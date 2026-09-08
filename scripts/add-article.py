#!/usr/bin/env python3
"""
一条命令把一篇文章加进已有的文章词库：抽正文 -> 翻译 -> 合成朗读 -> 追加 -> 自查。

和 make-article-dict.py 的分工：
    make-article-dict.py   建**新**库，一次转一批，会**覆盖**整个 json
    add-article.py（本脚本）往**已有**库里**追加**一篇，不动已有文章

为什么必须是追加而不是重建：`lastLearnIndex` 是**位置下标**，
`audioSrc` 也是按数组下标命名的（/audio/<enName>/<i>.ogg）。重建整库会重排
文章顺序 —— 页面不报错，只是「接着学」指到别的文章、每篇念的是别人的音频。
所以本脚本只往末尾加，已有条目的下标一个都不动。

--- 用法 ---

    export TW_TTS_KEY=sk-...
    export TW_TTS_URL=https://<网关>/api/v1/services/aigc/multimodal-generation/generation
    export TW_LLM_URL=https://<网关>/compatible-mode/v1/chat/completions   # 翻译用

    # 网页
    python3 scripts/add-article.py --url https://example.com/post --into my-reading

    # RSS 最近 3 篇 / PDF / 纯文本（首行当标题）
    python3 scripts/add-article.py --rss https://example.com/feed.xml --limit 3 --into my-reading
    python3 scripts/add-article.py --pdf paper.pdf --into my-reading --max-sentence-chars 200
    python3 scripts/add-article.py --txt a.txt --into my-reading

    # 库还不存在就自己建一个（第一篇）
    python3 scripts/add-article.py --url ... --into my-reading --create --name "我的阅读"

    # 先看看抽出来什么、多少句、要多久，不请求不写文件
    python3 scripts/add-article.py --url ... --into my-reading --dry-run

--- 词库 json 从哪来 ---

`--into` 指的是 `--dicts-dir`（默认 ./dicts）下的 en/article/<name>.json。
部署机上的那份才是**真的**，所以改之前要先把它拉下来：

    python3 scripts/pull-article-dict.py my-reading      # 没这个脚本就手动 scp/tar

不先拉就在本地旧副本上追加，会把部署机上后来加的文章覆盖掉。脚本会核对
文章数并在数量对不上时警告，但它没法替你判断哪份是新的。

--- 音频 ---

音频只给**新加的这几篇**合成（已有文章的 audioSrc 不动），产出在
`<out>/<enName>/<新下标>.ogg`。上传是增量的：那个目录里只有新文件，
tar 上去不会动已有的。

--- 边界 ---

- 不碰部署机。最后只**打印**上传命令，你自己看过再跑。
- 清单（public/list/article.json）里的 length 会过期，但前端在
  normalizeStoredDict() 里按 articles.length 重算，所以**不用重新构建镜像**。
  只有新建库（--create）才需要加清单条目 + rebuild。
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, filename):
    """把同目录的脚本当模块加载（文件名带横线，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(os.path.dirname(os.path.abspath(__file__)), filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 抽正文/断句/免费翻译都复用 make-article-dict.py，音频复用 synth-article-audio.py。
# 断句逻辑**必须**只有一份实现：它和前端 genArticleSectionData 是死死绑定的，
# 抄一份出来早晚会分叉，而分叉的表现是「每句念的是别的句子」且页面不报错。
mad = _load('mad', 'make-article-dict.py')
synth = _load('synth', 'synth-article-audio.py')

LLM_URL = os.environ.get(
    'TW_LLM_URL',
    'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions')
LLM_MODEL = os.environ.get('TW_LLM_MODEL', 'qwen-plus')

# 逐行翻译的提示词。关键是**行数必须守住** —— 译文按下标和原文对应，
# 多一行少一行会让那一段之后全部错位（页面不报错，只是中英对不上）。
# 编号是给模型的锚，也是给我们的校验依据：回来的行必须还带着同样的编号。
LLM_SYS = ('你是英译中译者。逐行翻译，严格保持行数一致：输入 N 行编号句子，'
           '输出恰好 N 行，每行以相同编号开头。不合并、不拆分、不加注释、'
           '不输出原文。只输出译文行。')

# 一批多少行。太大容易掉行/超时，太小则请求数多、限流风险高。
# 实测 40 行稳定（~27s / 2400 tokens），80 行也能守住行数但耗时翻倍。
LLM_BATCH = 40


# ---------------------------------------------------------------- 翻译（LLM）

def _llm_once(key, lines, timeout=180):
    """发一批带编号的行，返回按编号还原的译文列表。行数不符就抛。"""
    numbered = '\n'.join(f'{i + 1}. {s}' for i, s in enumerate(lines))
    payload = json.dumps({
        'model': LLM_MODEL,
        'messages': [{'role': 'system', 'content': LLM_SYS},
                     {'role': 'user', 'content': numbered}],
        # 翻译要的是稳定复现，不是创造性
        'temperature': 0,
    }).encode()
    req = urllib.request.Request(
        LLM_URL, data=payload,
        headers={'Authorization': f'Bearer {key}',
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.load(r)
    text = body['choices'][0]['message']['content']

    # 按编号取回，而不是简单 splitlines()：模型偶尔会在前面加一句
    # "以下是翻译："，或者让长句里带上换行。按编号锚定能容忍这些。
    got = {}
    for line in text.splitlines():
        m = re.match(r'^\s*(\d+)\s*[.、:：]\s*(.*)$', line)
        if m:
            got[int(m.group(1))] = m.group(2).strip()
    missing = [i for i in range(1, len(lines) + 1) if not got.get(i)]
    if missing:
        raise RuntimeError(f'{len(missing)} 行没对上编号（如第 {missing[:5]} 行）')
    return [got[i] for i in range(1, len(lines) + 1)]


def llm_translate(key, lines, batch=LLM_BATCH, retries=3):
    """
    分批逐行翻译，返回 (译文列表, 失败行数)。

    行数守不住的批次**整批重试**，重试仍失败就整批留空占位 —— 宁可少几行
    中文，也不能让下标错位。空占位由调用方换成 ' '（不能是空串，
    '\\n'.join(['','']) 是 '\\n\\n'，正好是段落分隔符，会凭空多出一个段边界）。
    """
    out, failed = [], 0
    for start in range(0, len(lines), batch):
        chunk = lines[start:start + batch]
        last = None
        for attempt in range(retries):
            try:
                out.extend(_llm_once(key, chunk))
                break
            except Exception as e:                      # noqa: BLE001
                last = e
                if attempt < retries - 1:
                    time.sleep(3 * (attempt + 1))
        else:
            print(f'    第 {start + 1}-{start + len(chunk)} 行翻译失败，留空：{last}',
                  file=sys.stderr)
            out.extend([''] * len(chunk))
            failed += len(chunk)
        print(f'    翻译 {min(start + batch, len(lines))}/{len(lines)}', flush=True)
    return out, failed


def translate_sections(key, sections, title, free=False):
    """
    翻译一篇，返回 (titleTranslate, textTranslate, 失败句数)。

    textTranslate 严格按 sections 重建：同样的段数、同样的每段行数。
    这一步和 make-article-dict.translate_article 是同一个契约，
    只是把翻译引擎换成 LLM。
    """
    flat = [s for sec in sections for s in sec]
    if free:
        return mad.translate_article(sections, title)

    translated, failed = llm_translate(key, [title] + flat)
    title_cn, body = translated[0], translated[1:]
    # 失败的句子用空格占位，不能用空串（见 llm_translate 的说明）
    body = [t if t.strip() else ' ' for t in body]

    out, pos = [], 0
    for sec in sections:
        out.append('\n'.join(body[pos:pos + len(sec)]))
        pos += len(sec)
    return title_cn, '\n\n'.join(out), failed


# ---------------------------------------------------------------- 词库读写

def dict_path(dicts_dir, en_name):
    return os.path.join(dicts_dir, 'en', 'article', f'{en_name}.json')


def load_dict(path, create):
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            sys.exit(f'{path} 不是文章数组')
        return data
    if not create:
        sys.exit(f'词库不存在：{path}\n'
                 f'  先从部署机把它拉下来（本地旧副本会覆盖线上新加的文章），\n'
                 f'  或者确实要新建就加 --create --name "显示名"')
    return []


def structure_ok(text, text_cn):
    """段数 + 每段行数必须完全一致，否则前端按下标取译文会错位。"""
    a = [len(s.split('\n')) for s in text.split('\n\n')]
    b = [len(s.split('\n')) for s in text_cn.split('\n\n')]
    return a == b, a, b


# ---------------------------------------------------------------- 主流程

def gather(args):
    """复用 make-article-dict 的抽取器，统一成 [{'title','paragraphs'}]。"""
    # gather_sources 读的是 argparse 的属性名，这里造一个同形状的壳
    shim = argparse.Namespace(
        url=args.url, ted=args.ted, pdf=args.pdf, rss=args.rss,
        json=args.json, txt_dir=args.txt_dir,
        limit=args.limit, fetch_full=args.fetch_full)
    docs = mad.gather_sources(shim)

    # --txt 是单个文件（make-article-dict 只有 --txt-dir）。约定同它：
    # 首行标题，其余正文。
    for path in args.txt or []:
        with open(path, encoding='utf-8') as f:
            content = f.read().strip()
        if not content:
            continue
        lines = content.split('\n')
        docs.append({
            'title': lines[0].strip(),
            # 自备的 txt 不过 drop_boilerplate：想留什么是自己的事
            'paragraphs': [p for p in re.split(r'\n\s*\n', '\n'.join(lines[1:]).strip()
                                               or content) if p.strip()],
        })
        print(f'  + {lines[0].strip()[:50]}')
    return docs


def main():
    ap = argparse.ArgumentParser(
        description='把一篇文章追加进已有的文章词库（抽正文+翻译+合成朗读+自查）',
        formatter_class=argparse.RawDescriptionHelpFormatter)

    ap.add_argument('--into', required=True, metavar='EN_NAME',
                    help='目标词库的 enName（= dicts/en/article/<EN_NAME>.json）')
    ap.add_argument('--dicts-dir', default=os.path.join(ROOT, 'dicts'),
                    help='词库根目录，默认 ./dicts')
    ap.add_argument('--create', action='store_true',
                    help='词库不存在时新建（需要 --name）。新建的库要加清单 + rebuild')
    ap.add_argument('--name', help='新建词库时页面上显示的名字')

    src = ap.add_argument_group('来源（至少一个）')
    src.add_argument('--url', nargs='*', help='网页地址')
    src.add_argument('--ted', nargs='*', metavar='URL', help='TED 演讲，取英文文稿')
    src.add_argument('--pdf', nargs='*', help='PDF 路径或地址')
    src.add_argument('--rss', nargs='*', help='RSS/Atom 订阅')
    src.add_argument('--json', nargs='*', help='JSON，见 make-article-dict.py 头部')
    src.add_argument('--txt', nargs='*', help='txt 文件，首行当标题')
    src.add_argument('--txt-dir', nargs='*', help='txt 目录，每文件一篇')
    src.add_argument('--limit', type=int, default=3,
                     help='每个订阅取几篇，默认 3（追加场景一般不要一次几十篇）')
    src.add_argument('--fetch-full', action='store_true',
                     help='订阅只给摘要时，回原文页抓全文')

    t = ap.add_argument_group('翻译')
    t.add_argument('--translate-free', action='store_true',
                   help='用 Google 免费接口而不是 LLM（不要 key，但质量差些、限流紧）')
    t.add_argument('--no-translate', action='store_true', help='不翻译，译文留空')

    a = ap.add_argument_group('朗读音频')
    a.add_argument('--voice', default='Chelsie', choices=synth.VOICES,
                   help='音色，默认 Chelsie')
    a.add_argument('--no-audio', action='store_true',
                   help='不合成音频（页面会回退到浏览器 TTS 机械音）')
    a.add_argument('--out', default='/tmp/twaudio', help='音频输出目录')
    a.add_argument('--cache', default='/tmp/twaudio-cache', help='按句 wav 缓存')
    a.add_argument('--codec', default='opus', choices=sorted(synth.CODECS))
    a.add_argument('--bitrate', default=None, help='默认按 codec 取')
    a.add_argument('--gap', type=float, default=0.25, help='句间静音秒数')
    a.add_argument('--jobs', type=int, default=2, help='合成并发，默认 2')

    ap.add_argument('--max-sentence-chars', type=int, default=0, metavar='N',
                    help='拆分超过 N 字符的长句，0 不拆。PDF 建议 200')
    ap.add_argument('--strip-math', action='store_true',
                    help='LaTeX 转纯文本，整句是公式的丢掉。抓论文/AI 博客时加')
    ap.add_argument('--dry-run', action='store_true',
                    help='只抽正文并报告规模，不翻译、不合成、不写文件')
    args = ap.parse_args()

    if not any([args.url, args.ted, args.pdf, args.rss, args.json,
                args.txt, args.txt_dir]):
        sys.exit('至少要给一个来源：--url / --ted / --pdf / --rss / --json / --txt / --txt-dir')
    if not re.match(r'^[A-Za-z0-9_-]+$', args.into):
        sys.exit(f'--into 只能是字母数字和 - _（要进 URL）：{args.into}')
    if args.create and not args.name:
        sys.exit('--create 要配 --name')
    if args.bitrate is None:
        args.bitrate = synth.CODECS[args.codec]['default_bitrate']
    if not args.no_audio and not args.dry_run:
        synth.check_ffmpeg(args.codec)

    key_tts = os.environ.get('TW_TTS_KEY')
    key_llm = os.environ.get('TW_LLM_KEY') or key_tts
    do_translate = not args.no_translate
    if not args.dry_run:
        if not args.no_audio and not key_tts:
            sys.exit('请设置 TW_TTS_KEY（或加 --no-audio）')
        if do_translate and not args.translate_free and not key_llm:
            sys.exit('请设置 TW_LLM_KEY 或 TW_TTS_KEY（或加 --translate-free / --no-translate）')

    path = dict_path(args.dicts_dir, args.into)
    existing = load_dict(path, args.create or args.dry_run)
    base = len(existing)
    print(f'目标词库 {args.into}：已有 {base} 篇')
    if base and not args.dry_run:
        print('  （确认这份是从部署机拉下来的最新副本，否则会覆盖线上后加的文章）')

    # ---- 1. 抽正文 ----
    print('\n[1/5] 抽正文')
    docs = gather(args)
    if not docs:
        sys.exit('没有取到任何内容')

    prepared = []
    for d in docs:
        text, sections, math_dropped = mad.build_text(
            d['paragraphs'], args.max_sentence_chars, args.strip_math)
        if not text.strip():
            print(f'  跳过《{d["title"][:40]}》：没有正文', file=sys.stderr)
            continue
        title = re.sub(r'\s+', ' ', d.get('title') or '').strip() or 'Untitled'
        # 同名的已经在库里就跳过，重跑同一条命令不会加出重复文章
        if any(x.get('title') == title for x in existing):
            print(f'  跳过《{title[:40]}》：库里已有同名文章')
            continue
        if math_dropped:
            print(f'    丢弃 {math_dropped} 句纯公式')
        prepared.append({'title': title, 'text': text, 'sections': sections,
                         'titleTranslate': d.get('titleTranslate', ''),
                         'textTranslate': d.get('textTranslate', '')})
        n = sum(len(s) for s in sections)
        print(f'  + 《{title[:44]}》{len(sections)} 段 / {n} 句')

    if not prepared:
        print('没有新文章可加')
        return 0

    n_sent = sum(sum(len(s) for s in p['sections']) for p in prepared)
    n_char = sum(len(x) for p in prepared for s in p['sections'] for x in s)
    est = n_char / 15.1          # 15.1 字符/秒，七个音色的实测均值
    print(f'\n共 {len(prepared)} 篇 / {n_sent} 句 / {n_char} 字符')
    if not args.no_audio:
        mb = est * int(args.bitrate.rstrip('k')) * 1000 / 8 / 1024 / 1024
        print(f'  预计音频 {est / 60:.1f} 分钟 / {mb:.1f} MB @{args.codec} {args.bitrate}'
              f'，合成约 {n_sent * 1.7 / args.jobs / 60:.0f} 分钟')

    if args.dry_run:
        print('\n--dry-run，未翻译、未合成、未写文件')
        return 0

    # ---- 2. 翻译 ----
    print('\n[2/5] 翻译')
    for p in prepared:
        if p['textTranslate']:                      # JSON 输入自带译文
            print(f'  《{p["title"][:40]}》自带译文，跳过')
        elif not do_translate:
            print('  --no-translate，译文留空（页面里可以点「翻译」）')
            break
        else:
            n = sum(len(s) for s in p['sections'])
            engine = 'Google 免费接口' if args.translate_free else LLM_MODEL
            print(f'  《{p["title"][:40]}》{n} 句，用 {engine}')
            p['titleTranslate'], p['textTranslate'], failed = translate_sections(
                key_llm, p['sections'], p['title'], free=args.translate_free)
            if failed >= n:
                print('    整篇都没翻出来，丢弃译文（页面里可以点「翻译」）',
                      file=sys.stderr)
                p['titleTranslate'] = p['textTranslate'] = ''
            elif failed:
                print(f'    {failed}/{n} 句失败，留空白占位')

        # 结构校验：这是整个流程最容易静默出错的地方，对不上就丢译文保原文
        if p['textTranslate']:
            ok, a, b = structure_ok(p['text'], p['textTranslate'])
            if not ok:
                print(f'    译文结构不符（原文 {len(a)} 段 vs 译文 {len(b)} 段），'
                      f'丢弃译文保住原文', file=sys.stderr)
                p['titleTranslate'] = p['textTranslate'] = ''
            else:
                print(f'    {sum(a)}/{sum(b)} 行对齐 ✓')

    # ---- 3. 合成音频 ----
    print('\n[3/5] 合成音频')
    new_articles = []
    for i, p in enumerate(prepared):
        art = {
            'id': base + i + 1,
            'title': p['title'],
            'titleTranslate': p['titleTranslate'],
            'text': p['text'],
            'textTranslate': p['textTranslate'],
            'audioSrc': '',
            'lrcPosition': None,
            'question': None,
            'nameList': None,
            'quote': None,
            'userId': None,
        }
        new_articles.append(art)

    audio_dir = os.path.join(args.out, args.into)
    if args.no_audio:
        print('  --no-audio，跳过（句子发音会回退到浏览器 TTS）')
    else:
        # 用同一个 split_sentences：顺序必须和前端一致，否则 lrcPosition
        # 对错句子 —— 页面不报错，只是每句念的是别的句子
        plans = [(base + i, art, synth.split_sentences(art['text']))
                 for i, art in enumerate(new_articles)]
        failed = synth.synth_missing(
            key_tts, [s for _, _, ss in plans for s in ss],
            args.voice, args.cache, args.jobs)
        if failed:
            print(f'\n{len(failed)} 句合成失败，前几条：')
            for txt, err in failed[:5]:
                print(f'  {txt!r}: {err}')
            print('直接重跑本命令即可（已合成的会跳过，也不会重复加文章）')
            return 1

        ext = synth.CODECS[args.codec]['ext']
        total = 0
        for idx, art, sents in plans:
            out_file = os.path.join(audio_dir, f'{idx}.{ext}')
            lrc, expect = synth.build_audio(sents, out_file, args.cache, args.voice,
                                            args.codec, args.bitrate, args.gap)
            art['audioSrc'] = f'/audio/{args.into}/{idx}.{ext}'
            art['lrcPosition'] = lrc
            size = os.path.getsize(out_file)
            total += size
            real = synth.duration(out_file)
            flag = '  ⚠ 时长不符' if abs(real - expect) > 1.0 else ''
            print(f'  [{idx}] {len(sents):4d} 句  {real / 60:5.1f} 分钟  '
                  f'{size / 1024 / 1024:5.1f} MB  {art["title"][:34]}{flag}')
        print(f'  音频 {audio_dir}，{total / 1024 / 1024:.1f} MB')

    # ---- 4. 追加写回 ----
    print('\n[4/5] 写入词库')
    merged = existing + new_articles
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 先写临时文件再改名：写一半断电不会留下坏 json（这可是全部学习素材）
    tmp = path + '.part'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False)
    os.replace(tmp, path)
    print(f'  {path}（+{len(new_articles)} 篇，共 {len(merged)} 篇，'
          f'{os.path.getsize(path):,} 字节）')

    # ---- 5. 自查 ----
    print('\n[5/5] 自查')
    checks = [[sys.executable, os.path.join(ROOT, 'scripts', 'check-article-dict.py'),
               path]]
    if not args.no_audio:
        checks.append([sys.executable,
                       os.path.join(ROOT, 'scripts', 'check-article-audio.py'),
                       path, '--audio', args.out])
    bad = 0
    for cmd in checks:
        r = subprocess.run(cmd)
        bad += r.returncode != 0
    if bad:
        print('\n自查没过，先修好再上传', file=sys.stderr)
        return 1

    # ---- 上传命令：只打印，不执行 ----
    print('\n全部通过。上传命令（自己看过再跑）：')
    if not args.no_audio:
        print(f'  python3 scripts/deploy-article-dicts.py --audio-dir {audio_dir}')
    name = args.name or args.into
    print(f'  python3 scripts/deploy-article-dicts.py --file {path} --name "{name}"')
    if args.create:
        print('\n新建的库要让页面看见它，必须再跑一次构建（清单是构建输入）：')
        print('  python3 scripts/deploy-article-dicts.py --rebuild')
    else:
        print('\n往已有库追加：不用 --rebuild。清单里的 length 会过期，但前端按'
              ' articles.length 重算。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
