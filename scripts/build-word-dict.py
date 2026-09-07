#!/usr/bin/env python3
"""
用一份词表生成 TypeWords 词库。

官方那 241 个词库全是应试向的（四六级/考研/雅思托福/中小学教材），
想练专业词汇（AI、生命科学）或日常口语，只能自己造。这个脚本负责造，
但**不编造词条内容** —— 释义、音标、例句全部来自现成的词典数据：

  1. 已下载的官方词库（./dicts/en/word/*.json，241 个文件、33064 个去重单词）
     这是最好的来源：有 phonetic0/1、trans、sentences、phrases、synos、
     etymology、relWords，字段完整，和内置词库同一个模子。
  2. ECDICT（https://github.com/skywind3000/ECDICT，CC-BY-SA，370 万词条）
     官方词库里没有的词（多是专业术语，如 ribosome、hyperparameter）从这里补。
     只有 phonetic + translation，没有例句 —— 字段少，但都是真实数据。

命中不了的词直接丢掉并在末尾列出来，不去凑数。宁可词库小一点。

用法：
  # 词表：每行一个单词，# 开头是注释
  python3 scripts/build-word-dict.py --words wordlists/ai.txt \\
      --name "AI 与机器学习" --en-name ai-ml --category 专业领域 --tags 人工智能

  python3 scripts/build-word-dict.py --words - < /tmp/list.txt --name ... # 读 stdin
  python3 scripts/build-word-dict.py --words a.txt --name ... --dry-run   # 只看命中率

输出：
  {dest}/en/word/{en-name}.json    词库数据（运行时挂载，加词典只要重启）
  public/list/word.json            追加一条清单（构建输入，改了必须重新构建镜像）

清单为什么必须进 public/：nitro 的静态资源索引是构建时生成的，里面记着每个
文件的 size，运行时替换同名文件会被按旧 size 截断。详见 docker/README.md。
"""

import argparse
import csv
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 只收纯字母（含连字符和撇号）的词。词库是用来「打字练习」的，
# 带数字或符号的条目打起来没意义，ECDICT 里还有不少词组和乱码条目。
VALID_WORD = re.compile(r"^[a-zA-Z][a-zA-Z'\-]*[a-zA-Z]$")

# ECDICT 的 translation 形如 "n. 核糖体\n[化] 核蛋白体"，
# 前缀是词性。这里把常见词性抠出来放进 trans[].pos，与官方词库对齐。
POS_PREFIX = re.compile(r'^\s*((?:[a-z]+\.\s*)+|\[[^\]]+\]\s*)')


def norm(w):
    return (w or '').strip().lower()


def load_local_dicts(dicts_dir, verbose=True):
    """
    扫描已下载的官方词库，建立 单词 -> 最完整的那条 的索引。

    同一个词在多个词库里都有（cancel 在四级、六级、考研里都出现），
    内容不一定一样 —— 有的带例句有的不带。按「字段丰富程度」打分取最高的，
    这样生成出来的词库质量不输官方。
    """
    files = sorted(glob.glob(os.path.join(dicts_dir, 'en', 'word', '*.json')))
    if not files:
        return {}

    index = {}
    for path in files:
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001 —— 坏文件跳过就好，不该中断整个构建
            if verbose:
                print(f'  跳过 {os.path.basename(path)}: {e}', file=sys.stderr)
            continue
        if not isinstance(data, list):
            continue

        for w in data:
            if not isinstance(w, dict):
                continue
            key = norm(w.get('word'))
            if not key:
                continue
            # 例句最有价值，权重给最高；音标次之
            score = (
                len(w.get('sentences') or []) * 3
                + len(w.get('phrases') or []) * 2
                + len(w.get('synos') or [])
                + len(w.get('etymology') or [])
                + len(w.get('trans') or [])
                + (2 if w.get('phonetic0') else 0)
            )
            if key not in index or score > index[key][0]:
                index[key] = (score, w)

    if verbose:
        print(f'本地词库：{len(files)} 个文件，{len(index)} 个去重单词')
    return {k: v[1] for k, v in index.items()}


def load_ecdict(path, wanted, verbose=True):
    """
    从 ECDICT 里只取需要的词。

    整个 csv 66MB / 370 万行，全读进内存要 2GB+，所以流式扫、只留 wanted 里的。
    """
    if not path or not os.path.exists(path):
        return {}

    csv.field_size_limit(10 ** 7)  # detail 字段偶尔很长，默认上限会抛异常
    found = {}
    with open(path, encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            key = norm(row.get('word'))
            if key in wanted and key not in found:
                found[key] = row
    if verbose:
        print(f'ECDICT：补到 {len(found)} 个')
    return found


def parse_ecdict_translation(text):
    """
    "n. 核糖体\\n[化] 核蛋白体" -> [{'pos': 'n.', 'cn': '核糖体'}, ...]

    ECDICT 里的换行是字面的两个字符 \\n，不是真换行。
    """
    if not text:
        return []
    trans = []
    for line in text.replace('\\n', '\n').split('\n'):
        line = line.strip()
        if not line:
            continue
        m = POS_PREFIX.match(line)
        if m and not line.startswith('['):
            pos = m.group(1).strip()
            cn = line[m.end():].strip()
        else:
            pos, cn = '', line
        if cn:
            trans.append({'pos': pos, 'cn': cn})
    return trans


def word_from_ecdict(row):
    """
    ECDICT 行 -> Word。

    没有例句/词根/同义词，相应字段留空 —— 前端对空数组是容忍的（内置词库里
    也有 etymology 为空的条目）。宁可留空，也不要拿英文释义硬当中文翻译。
    """
    trans = parse_ecdict_translation(row.get('translation'))
    if not trans:
        return None  # 没有中文释义的条目对中文用户没用

    phonetic = (row.get('phonetic') or '').strip()
    return {
        'word': row['word'].strip(),
        'phonetic0': phonetic,
        'phonetic1': phonetic,  # ECDICT 只给一个音标，英/美填同一个
        'trans': trans,
        'sentences': [],
        'phrases': [],
        'synos': [],
        'etymology': [],
        'relWords': {'root': '', 'rels': []},
        'langType': 'en',
    }


def read_wordlist(path):
    """读词表。每行一个词，# 注释，允许 "word  # 说明" 这种行内注释。"""
    f = sys.stdin if path == '-' else open(path, encoding='utf-8')
    try:
        seen, out = set(), []
        for line in f:
            line = line.split('#')[0].strip()
            if not line:
                continue
            key = norm(line)
            if key in seen or not VALID_WORD.match(key):
                continue
            seen.add(key)
            out.append(key)
        return out
    finally:
        if f is not sys.stdin:
            f.close()


def upsert_manifest(entry, kind='word'):
    """
    往 public/list/{kind}.json 追加或更新一条。

    以 url 为准判断是不是同一个词库 —— 重跑脚本时覆盖而不是追加出重复项。
    """
    path = os.path.join(ROOT, 'public', 'list', f'{kind}.json')
    data = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)

    for i, x in enumerate(data):
        if x.get('url') == entry['url']:
            # 保留原 id，避免用户已有的学习进度对不上（进度按 id 存）
            entry['id'] = x.get('id', entry['id'])
            data[i] = entry
            break
    else:
        data.append(entry)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    return path, len(data)


def is_usable(w):
    """
    官方词库里混着一些空壳条目 —— 有 word 和 id，但 trans / phonetic /
    sentences 全是空的（实测 semantic、corpus、diffusion、verification、
    changed 都是这样）。这种条目在页面上表现为「有词无释义」，比缺词更糟。

    所以判定标准是「有没有中文释义」，而不是「本地词库里有没有这个词」。
    不可用的就当没命中，回落到 ECDICT 去补 —— 上面那几个词 ECDICT 都有。
    """
    return bool(w and w.get('trans'))


def main():
    ap = argparse.ArgumentParser(description='用词表生成 TypeWords 词库')
    ap.add_argument('--words', required=True, help='词表文件，- 表示 stdin')
    ap.add_argument('--name', required=True, help='词库显示名，如「AI 与机器学习」')
    ap.add_argument('--en-name', required=True,
                    help='英文标识，同时用作文件名，只能是字母数字和 - _')
    ap.add_argument('--description', default='', help='词库描述')
    ap.add_argument('--category', default='专业领域', help='分类，页面按它分组')
    ap.add_argument('--tags', nargs='*', default=[], help='标签，分类下的二级筛选')
    ap.add_argument('--dest', default=os.path.join(ROOT, 'dicts'),
                    help='词典数据目录，默认 ./dicts')
    ap.add_argument('--ecdict', default=os.path.join(ROOT, 'build-cache', 'ecdict.csv'),
                    help='ECDICT csv 路径，用于补官方词库里没有的词')
    ap.add_argument('--no-ecdict', action='store_true', help='只用本地词库，不补 ECDICT')
    ap.add_argument('--dry-run', action='store_true', help='只报告命中率，不写文件')
    ap.add_argument('--min-hit', type=float, default=0.5, metavar='RATIO',
                    help='命中率低于此值就报错退出，默认 0.5（词表大概写错了）')
    args = ap.parse_args()

    # 文件名会进 URL，被 server/routes/dicts 的 SAFE_NAME 白名单校验
    if not re.match(r'^[A-Za-z0-9_-]+$', args.en_name):
        print(f'--en-name 只能包含字母数字和 - _：{args.en_name}', file=sys.stderr)
        return 2

    wordlist = read_wordlist(args.words)
    if not wordlist:
        print('词表是空的', file=sys.stderr)
        return 2
    print(f'词表：{len(wordlist)} 个词')

    local = load_local_dicts(args.dest)
    words, missing = [], []
    for key in wordlist:
        hit = local.get(key)
        if is_usable(hit):
            words.append(hit)
        else:
            missing.append(key)

    print(f'本地命中 {len(words)}，还差 {len(missing)}')

    if missing and not args.no_ecdict:
        ec = load_ecdict(args.ecdict, set(missing))
        still = []
        for key in missing:
            row = ec.get(key)
            w = word_from_ecdict(row) if row else None
            if w:
                words.append(w)
            else:
                still.append(key)
        missing = still

    # 兜底：无论从哪条路径来的，没有中文释义的一律不要。
    # 上面每个分支都做了判断，这里是最后一道闸门，防止以后加来源时漏掉。
    before = len(words)
    words = [w for w in words if is_usable(w)]
    if len(words) < before:
        print(f'又剔除了 {before - len(words)} 个没有释义的条目')

    # 按词表原始顺序输出。词表通常是按频率排的，顺序有意义
    order = {w: i for i, w in enumerate(wordlist)}
    words.sort(key=lambda w: order.get(norm(w.get('word')), 10 ** 9))

    hit = len(words) / len(wordlist)
    print(f'\n最终 {len(words)}/{len(wordlist)} 个（命中率 {hit:.1%}）')
    if missing:
        preview = ' '.join(missing[:30])
        more = f' ...另有 {len(missing) - 30} 个' if len(missing) > 30 else ''
        print(f'未收录（已丢弃，不编造）：{preview}{more}')

    if hit < args.min_hit:
        print(f'\n命中率低于 {args.min_hit:.0%}，词表可能有问题，中止。'
              f'确认无误可用 --min-hit 0 强制继续。', file=sys.stderr)
        return 1

    if args.dry_run:
        print('\n--dry-run，未写入任何文件')
        return 0

    out_dir = os.path.join(args.dest, 'en', 'word')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f'{args.en_name}.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(words, f, ensure_ascii=False)
    print(f'\n已写入 {out_path}（{os.path.getsize(out_path):,} 字节）')

    # id 用 en_name：清单里 id 可以是字符串（官方自己就有 ieltsWang3 这种），
    # 前端 normalizeDictId 会 String() 一遍，用字符串不会和官方的数字 id 撞车
    entry = {
        'id': args.en_name,
        'enName': args.en_name,
        'name': args.name,
        'description': args.description or args.name,
        'categoryId': 0,
        'url': f'{args.en_name}.json',
        'length': len(words),
        'language': 'en',
        'translateLanguage': 'zh_CN',
        'version': 1,
        'type': 'word',
        'isDefault': False,
        'recommended': False,
        'userId': None,
        'cover': None,
        'hidden': False,
        'category': args.category,
        'tags': args.tags or [args.category],
    }
    path, total = upsert_manifest(entry, 'word')
    print(f'清单已更新 {path}（共 {total} 个词库）')
    print('清单是构建输入 —— 需要 docker compose up -d --build 才会生效。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
