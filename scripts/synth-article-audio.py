#!/usr/bin/env python3
"""
给文章词库合成朗读音频，让句子发音从浏览器 TTS 换成云端 TTS。

## 为什么需要这个

前端播某一句时（core/hooks/article.ts:playSentenceAudio）只有在
`sentence.audioPosition` 和 `<audio>` 的 src 都有值时才播真实音频，
否则把**整句**丢进有道的**单词**发音接口 —— 那个接口不接整句，
一个 74 字符的句子返回 120 字节空响应，于是 onerror 回退到浏览器
speechSynthesis，就是那个机械嗓子。

官方的新概念英语带真人配音 MP3+ 逐句 lrcPosition 时间轴，所以听起来正常。
自己抓的网页文章没有这份数据，只能自己合成。

## 产出

每篇文章一个 MP3（整篇拼在一起，不是一句一个文件 —— 前端就是按
`audioSrc` + `[start, end]` 的模式跳着播的），外加写回词库 json：

    article['audioSrc']    = '/audio/<en_name>/<idx>.mp3'
    article['lrcPosition'] = [[start, end], ...]   # 按句展平，秒

lrcPosition 的顺序必须和前端切句的顺序**完全一致**：text 按 \\n\\n 分段、
段内按 \\n 分句，展平后逐个对应（article.ts:genArticleSectionData）。
所以这里的切分逻辑是照抄前端的，不要"优化"。

## 用法

    export TW_TTS_KEY=sk-...
    # 用自建/私有网关的话再给这两个（默认走 dashscope 公网地址）
    export TW_TTS_URL=https://<你的网关>/api/v1/services/aigc/multimodal-generation/generation
    export TW_TTS_MODEL=qwen3-tts-flash

    # 先看有哪些音色能用（换网关/换模型后一定要先试）
    python3 scripts/synth-article-audio.py --dict ... --list-voices

    # 只跑一个库看效果（生命科学科普最小，649 句约 15 分钟）
    python3 scripts/synth-article-audio.py \\
        --dict /tmp/twout/en/article/life-science-read.json \\
        --voice Ethan --out /tmp/twaudio

    # 先看要花多少时间/多少流量，不实际请求
    python3 scripts/synth-article-audio.py --dict ... --dry-run

可反复运行：每句的音频按 (音色, 文本) 的哈希缓存在 --cache 目录下，
中断后重跑只补没合成的那些。

## 坑

- **网关限流很紧。** 实测 4 路并发有一半直接 Throttling.RateQuota，2 路稳定。
  默认 --jobs 2，不要往上调。
- **MP3 必须恒定码率（CBR）。** 前端跳句是 `audio.currentTime = start`，
  VBR 的 MP3 按字节偏移估算时间会跳错位置。这里用 `-b:a` 而不是 `-q:a`。
- **服务端必须支持 Range。** 见 server/routes/audio/[...path].get.ts。
- 音频**不能放 public/sound/**：那是构建输入，进了镜像每次加文章都要重新构建。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = os.environ.get(
    'TW_TTS_URL',
    'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation')
MODEL = os.environ.get('TW_TTS_MODEL', 'qwen3-tts-flash')

# 实测可用的音色。踩过的坑：
#   - OpenAI 兼容的 /audio/speech 在私有网关上 404，走不通。
#   - websocket（dashscope.audio.tts_v2.SpeechSynthesizer）连上后如果模型名
#     不在网关的模型列表里，会落到 cosyvoice 引擎，所有音色名都报
#     Engine error [411] —— 看着像音色名写错了，其实是模型名不对。
#   - 能用的是 DashScope 原生 HTTP 的 multimodal-generation 路径，
#     返回一个装着 24kHz 单声道 WAV 的临时 URL。
# 换网关/换模型时先用 --list-voices 试一遍，不要假设这张表还对。
VOICES = ['Cherry', 'Ethan', 'Serena', 'Chelsie', 'Dylan', 'Jada', 'Sunny']


def split_sentences(text):
    """
    照抄前端 genArticleSectionData：\\n\\n 分段，段内 \\n 分句，展平。

    顺序必须和前端一致，否则 lrcPosition 对错句子 —— 页面不报错，
    只是每句都念的是别的句子。
    """
    out = []
    for section in filter(None, text.strip().split('\n\n')):
        for s in section.strip().split('\n'):
            s = s.strip()
            if s:
                out.append(s)
    return out


def synth(key, text, voice, timeout=90, retries=6):
    """合成一句，返回 wav 字节。限流就退避重试。"""
    payload = json.dumps({
        'model': MODEL,
        'input': {'text': text, 'voice': voice, 'language_type': 'English'},
    }).encode()
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                API, data=payload,
                headers={'Authorization': f'Bearer {key}',
                         'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.load(r)
            url = (body.get('output') or {}).get('audio', {}).get('url')
            if not url:
                # 限流是可重试的，其他错误（比如文本非法）重试也没用，但这里
                # 一律重试几次再放弃 —— 网关偶发 5xx 也会走到这里
                raise RuntimeError(body.get('code') or json.dumps(body)[:200])
            with urllib.request.urlopen(url, timeout=timeout) as f:
                return f.read()
        except Exception as e:  # noqa: BLE001 —— 网络/限流一律退避重试
            last = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f'合成失败: {last}')


def cache_path(cache_dir, voice, text):
    h = hashlib.sha256(f'{MODEL}\x00{voice}\x00{text}'.encode()).hexdigest()[:20]
    return os.path.join(cache_dir, voice, f'{h}.wav')


def duration(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', path], capture_output=True, text=True)
    return float(r.stdout.strip())


def main():
    ap = argparse.ArgumentParser(description='给文章词库合成朗读音频')
    ap.add_argument('--dict', required=True, help='文章词库 json（会被就地改写）')
    ap.add_argument('--voice', default='Ethan', choices=VOICES,
                    help='音色。语速差别很大（Serena ~118wpm 到 Jada ~195wpm）')
    ap.add_argument('--list-voices', action='store_true',
                    help='逐个试 VOICES 里的音色，报告哪些在当前网关/模型下真能用')
    ap.add_argument('--out', default='/tmp/twaudio',
                    help='音频输出目录，产出 <out>/<enName>/<idx>.mp3')
    ap.add_argument('--cache', default='/tmp/twaudio-cache',
                    help='按句缓存 wav，断点续传靠它')
    ap.add_argument('--bitrate', default='32k',
                    help='MP3 码率，默认 32k 单声道（人声朗读够用）。必须是 CBR')
    ap.add_argument('--gap', type=float, default=0.25,
                    help='句间静音秒数，默认 0.25。太小听着连成一片，太大拖沓')
    ap.add_argument('--jobs', type=int, default=2,
                    help='并发数，默认 2。实测 4 路一半被限流，别往上调')
    ap.add_argument('--limit', type=int, help='只处理前 N 篇，用来试效果')
    ap.add_argument('--dry-run', action='store_true', help='只算规模，不请求')
    args = ap.parse_args()

    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        sys.exit('需要 ffmpeg / ffprobe')

    if args.list_voices:
        key = os.environ.get('TW_TTS_KEY')
        if not key:
            sys.exit('请设置 TW_TTS_KEY')
        print(f'网关 {API}\n模型 {MODEL}\n')
        ok = []
        for v in VOICES:
            try:
                data = synth(key, 'This is a test.', v, retries=2)
                print(f'  可用   {v:10s} {len(data)} 字节')
                ok.append(v)
            except Exception as e:                       # noqa: BLE001
                print(f'  不可用 {v:10s} {str(e)[:90]}')
        print(f'\n{len(ok)}/{len(VOICES)} 可用: {", ".join(ok)}')
        return 0 if ok else 1

    en_name = os.path.splitext(os.path.basename(args.dict))[0]
    articles = json.load(open(args.dict))
    if args.limit:
        articles = articles[:args.limit]

    plan = [(i, a, split_sentences(a['text'])) for i, a in enumerate(articles)]
    n_sent = sum(len(s) for _, _, s in plan)
    n_char = sum(len(x) for _, _, s in plan for x in s)

    print(f'{en_name}: {len(plan)} 篇 / {n_sent} 句 / {n_char} 字符，音色 {args.voice}')
    # 15.1 字符/秒 是七个音色在真实句子上的实测均值
    est_audio = n_char / 15.1
    print(f'  预计音频 {est_audio / 60:.0f} 分钟，'
          f'{est_audio * int(args.bitrate.rstrip("k")) * 1000 / 8 / 1024 / 1024:.0f} MB @{args.bitrate}')
    print(f'  预计耗时 {n_sent * 1.7 / args.jobs / 60:.0f} 分钟（{args.jobs} 并发，单句实测 ~1.7s）')
    if args.dry_run:
        return 0

    key = os.environ.get('TW_TTS_KEY')
    if not key:
        sys.exit('请设置 TW_TTS_KEY')

    os.makedirs(os.path.join(args.cache, args.voice), exist_ok=True)
    out_dir = os.path.join(args.out, en_name)
    os.makedirs(out_dir, exist_ok=True)

    # ---- 1. 合成所有缺的句子（按句缓存，可续传）----
    todo = []
    for _, _, sents in plan:
        for s in sents:
            p = cache_path(args.cache, args.voice, s)
            if not os.path.exists(p) or os.path.getsize(p) == 0:
                todo.append((s, p))
    # 同一句在不同文章里重复出现时只合成一次
    todo = list({p: (s, p) for s, p in todo}.values())
    print(f'\n需要合成 {len(todo)} 句，已缓存 {n_sent - len(todo)} 句')

    done = [0]
    failed = []

    def work(item):
        s, p = item
        try:
            data = synth(key, s, args.voice)
            tmp = p + '.part'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, p)   # 先写 .part 再改名，中断不留半个文件
        except Exception as e:   # noqa: BLE001
            failed.append((s[:60], str(e)))
        done[0] += 1
        if done[0] % 25 == 0 or done[0] == len(todo):
            print(f'  [{done[0]}/{len(todo)}] 失败 {len(failed)}', flush=True)

    if todo:
        with ThreadPoolExecutor(args.jobs) as ex:
            list(ex.map(work, todo))

    if failed:
        print(f'\n{len(failed)} 句合成失败，前几条：')
        for t, e in failed[:5]:
            print(f'  {t!r}: {e}')
        print('直接重跑本脚本即可（已合成的会跳过）')
        return 1

    # ---- 2. 每篇拼成一个 MP3，同时记下每句的 [start, end] ----
    print('\n拼接音频 ...')
    total_bytes = 0
    for idx, article, sents in plan:
        mp3 = os.path.join(out_dir, f'{idx}.mp3')
        parts = [cache_path(args.cache, args.voice, s) for s in sents]

        # 用 concat demuxer 而不是 filter：不重新编码 wav，快得多。
        # 句间静音靠 apad 之类不好控，改成拼完后按各句真实时长算偏移，
        # 静音用一个专门生成的 wav 插进去。
        silence = os.path.join(args.cache, f'_gap_{args.gap}.wav')
        if not os.path.exists(silence):
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                            f'anullsrc=r=24000:cl=mono', '-t', str(args.gap),
                            silence], check=True)

        listfile = os.path.join(args.cache, f'_list_{en_name}_{idx}.txt')
        with open(listfile, 'w') as f:
            for i, p in enumerate(parts):
                f.write(f"file '{p}'\n")
                if i < len(parts) - 1:
                    f.write(f"file '{silence}'\n")

        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0',
                        '-i', listfile, '-c:a', 'libmp3lame',
                        '-b:a', args.bitrate, '-ac', '1', '-ar', '24000',
                        mp3], check=True)
        os.remove(listfile)

        # lrcPosition 按各句真实时长累加。注意用缓存 wav 的时长，
        # 不是去解析拼好的 mp3 —— mp3 编码会有几毫秒的帧对齐误差，
        # 但累加误差在一篇里可以忽略，而且前端播完就 pause，不影响。
        lrc, t = [], 0.0
        for p in parts:
            d = duration(p)
            lrc.append([round(t, 2), round(t + d, 2)])
            t += d + args.gap

        article['audioSrc'] = f'/audio/{en_name}/{idx}.mp3'
        article['lrcPosition'] = lrc
        size = os.path.getsize(mp3)
        total_bytes += size
        real = duration(mp3)
        # 拼出来的时长应该和累加的差不多，差太多说明有句子没拼进去
        drift = abs(real - (t - args.gap))
        flag = '  ⚠ 时长不符' if drift > 1.0 else ''
        print(f'  [{idx}] {len(sents):4d} 句  {real / 60:5.1f} 分钟  '
              f'{size / 1024 / 1024:5.1f} MB  {article["title"][:34]}{flag}')

    json.dump(articles, open(args.dict, 'w'), ensure_ascii=False)
    print(f'\n已写回 {args.dict}（audioSrc + lrcPosition）')
    print(f'音频 {out_dir}，共 {total_bytes / 1024 / 1024:.0f} MB')
    print('\n接下来：')
    print(f'  1. 把 {out_dir} 放到部署机的音频挂载目录 <AUDIO_PATH>/{en_name}/')
    print(f'  2. 用 deploy-article-dicts.py 传改过的 json')
    print(f'  3. 自查：python3 scripts/check-article-audio.py {args.dict} --audio {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
