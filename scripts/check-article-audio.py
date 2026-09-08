#!/usr/bin/env python3
"""
自查合成出来的文章音频：时间轴条数、对齐、以及 Range 请求能不能用。

为什么要单独查：lrcPosition 对错句子的话页面**不报错**，只是每句念的是
别的句子 —— 跳句用的是 `audio.currentTime = start`，越界或错位都不会抛异常。
而 Range 不支持时，也只是"跳一句要等十几秒"，看不出是服务端的问题。

用法：
    # 只查本地文件
    python3 scripts/check-article-audio.py /tmp/twout/en/article/*.json --audio /tmp/twaudio
    # 顺带查线上（部署完之后）
    python3 scripts/check-article-audio.py ... --base http://127.0.0.1:3000
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


def split_sentences(text):
    """必须和 synth-article-audio.py / 前端 genArticleSectionData 完全一致"""
    out = []
    for section in filter(None, text.strip().split('\n\n')):
        for s in section.strip().split('\n'):
            s = s.strip()
            if s:
                out.append(s)
    return out


def duration(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return -1.0


def check_range(base, path):
    """
    确认服务端支持 Range。不支持的话前端每次跳句都要把整个文件下完 ——
    最大的一篇有一个多小时、十几 MB。
    """
    try:
        req = urllib.request.Request(base + path, headers={'Range': 'bytes=100-199'})
        with urllib.request.urlopen(req, timeout=30) as r:
            code = r.status
            got = len(r.read())
            cr = r.headers.get('Content-Range')
            ar = r.headers.get('Accept-Ranges')
        if code != 206:
            return f'不支持 Range（返回 {code}，应为 206）'
        if got != 100:
            return f'Range 返回了 {got} 字节，应为 100'
        if not cr:
            return '缺 Content-Range 头'
        if ar != 'bytes':
            return f'Accept-Ranges = {ar!r}，应为 bytes'
        return None
    except urllib.error.HTTPError as e:
        return f'HTTP {e.code}'
    except Exception as e:                      # noqa: BLE001
        return str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dicts', nargs='+', help='文章词库 json')
    ap.add_argument('--audio', help='本地音频目录（synth 脚本的 --out）')
    ap.add_argument('--base', help='线上根地址，给了就顺带查 Range')
    ap.add_argument('--tolerance', type=float, default=1.0,
                    help='时间轴末尾与音频实际时长的容差（秒）')
    args = ap.parse_args()

    bad = 0
    for path in args.dicts:
        en_name = os.path.splitext(os.path.basename(path))[0]
        articles = json.load(open(path))
        print(f'--- {en_name}: {len(articles)} 篇 ---')

        n_with, n_without = 0, 0
        for idx, a in enumerate(articles):
            sents = split_sentences(a['text'])
            src = a.get('audioSrc') or ''
            lrc = a.get('lrcPosition') or []

            if not src:
                n_without += 1
                continue
            n_with += 1

            # 1) 条数必须和句数一样，否则后面的句子全部错位
            if len(lrc) != len(sents):
                print(f'  [FAIL] [{idx}] 时间轴 {len(lrc)} 条 ≠ 句数 {len(sents)}')
                bad += 1
                continue

            # 2) 区间必须单调递增且非负，否则 currentTime 会往回跳
            prev_end = -1.0
            broken = None
            for i, pair in enumerate(lrc):
                if not (isinstance(pair, list) and len(pair) == 2):
                    broken = f'第 {i} 条不是 [start, end]：{pair!r}'
                    break
                s, e = pair
                if s < 0 or e < s:
                    broken = f'第 {i} 条区间非法：{pair!r}'
                    break
                if s < prev_end - 0.01:
                    broken = f'第 {i} 条起点 {s} 早于上一句终点 {prev_end}'
                    break
                prev_end = e
            if broken:
                print(f'  [FAIL] [{idx}] {broken}')
                bad += 1
                continue

            # 3) 音频文件在不在、时长和时间轴末尾对不对得上
            if args.audio:
                # audioSrc 形如 /audio/<enName>/<idx>.mp3
                local = os.path.join(args.audio, *src.strip('/').split('/')[1:])
                if not os.path.exists(local):
                    print(f'  [FAIL] [{idx}] 音频不存在: {local}')
                    bad += 1
                    continue
                real = duration(local)
                drift = abs(real - lrc[-1][1])
                if drift > args.tolerance:
                    print(f'  [FAIL] [{idx}] 音频 {real:.2f}s 与时间轴末尾 '
                          f'{lrc[-1][1]:.2f}s 差 {drift:.2f}s')
                    bad += 1
                    continue
                if lrc[-1][1] > real + args.tolerance:
                    print(f'  [FAIL] [{idx}] 时间轴超出音频末尾，最后几句播不出来')
                    bad += 1
                    continue

            # 4) 线上 Range
            if args.base:
                err = check_range(args.base.rstrip('/'), src)
                if err:
                    print(f'  [FAIL] [{idx}] {src}: {err}')
                    bad += 1
                    continue

        note = ''
        if n_without:
            note = f'，另有 {n_without} 篇没有音频（会回退到浏览器 TTS）'
        print(f'  {n_with} 篇有音频{note}')

    print('全部通过' if not bad else f'{bad} 处有问题')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
