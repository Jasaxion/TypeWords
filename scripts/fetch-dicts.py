#!/usr/bin/env python3
"""
下载官方词典数据，丰富自部署站点的词库。

官方站点把清单和词典数据分开托管：
  https://typewords.cc/         页面（Nuxt SSR）
  https://files.typewords.cc/   清单 + 词典数据（EdgeOne，Access-Control-Allow-Origin: *）

数据与官方完全一致（CET4_T.json 与仓库内自带的那份 md5 相同），不是转换/降级的版本。

为什么不把词典放进 git / 镜像：
  全量 245 个文件约 430MB。放进仓库会让 clone 和 docker build 都变得很难受，
  而这些是官方的静态数据、不是我们的代码。所以下到宿主机目录，运行时挂进容器，
  由 server/routes/dicts 提供访问 —— 与 ./data 的思路一致：代码和数据分开。

用法：
  python3 scripts/fetch-dicts.py                  # 下载全部（约 430MB）
  python3 scripts/fetch-dicts.py --category 中国考试 国际考试
  python3 scripts/fetch-dicts.py --max-size 3     # 只要小于 3MB 的，省空间
  python3 scripts/fetch-dicts.py --list           # 只看清单，不下载
  python3 scripts/fetch-dicts.py --dest /path/to/dicts  # 指定目录（默认 ./dicts）

可反复运行：已存在且大小与服务端一致的文件会跳过，中断后重跑即可续传。
"""

import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = 'https://files.typewords.cc'
UA = {'User-Agent': 'Mozilla/5.0 (TypeWords self-host dict fetcher)'}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 官方有两份清单，内容不同，取并集：
#   files.typewords.cc  站点运行时实际读取的（194 条）
#   typewords.cc        页面里那份，多了 47 个雅思听力词库（241 条）
# 两处的 47 个差集文件在 CDN 上都真实存在，所以合并后一起提供。
LIST_SOURCES = {
    'word': [f'{BASE}/list/word.json', 'https://typewords.cc/list/word.json'],
    'article': [f'{BASE}/list/article.json', 'https://typewords.cc/list/article.json'],
    'recommend_word': [f'{BASE}/list/recommend_word.json'],
    'recommend_article': [f'{BASE}/list/recommend_article.json'],
}


def get(url, timeout=60, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 —— 网络问题一律重试
            last = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f'下载失败 {url}: {last}')


def get_json(url):
    return json.loads(get(url).decode('utf-8'))


def remote_size(kind, url):
    """HEAD 拿 Content-Length，用于跳过已完整下载的文件"""
    try:
        req = urllib.request.Request(f'{BASE}/dicts/en/{kind}/{url}', headers=UA, method='HEAD')
        with urllib.request.urlopen(req, timeout=30) as r:
            return int(r.headers.get('Content-Length') or 0)
    except Exception:  # noqa: BLE001
        return 0


def load_manifests():
    """取两个来源的并集。同一个 url 以第一个来源（站点实际用的那份）为准。"""
    out = {}
    for kind, urls in LIST_SOURCES.items():
        merged, seen = [], set()
        for u in urls:
            try:
                data = get_json(u)
            except Exception as e:  # noqa: BLE001 —— 备用来源挂了不致命
                print(f'  ! 读取清单失败（已跳过）{u}: {e}', file=sys.stderr)
                continue
            for x in data:
                key = x.get('url')
                # 同一个词典可能被多个分类引用（如牛津 5000），按 url 去重
                if key and key not in seen:
                    seen.add(key)
                    merged.append(x)
        out[kind] = merged
    return out


def write_manifests(manifests, dest):
    """
    生成 public/list/*.json。

    为什么写进 public/ 而不是挂载目录：nitro 的静态资源索引是构建时生成的，
    里面记着每个文件的 size，运行时替换同名文件会被按旧 size 截断（实测确认）。
    所以清单只能作为构建输入。好在四份清单加起来才 130KB 左右，进 git 无妨。

    只保留「文件确实下载到了」的条目 —— 否则页面上列出一堆点开就 404 的词典。
    """
    list_dir = os.path.join(ROOT, 'public', 'list')
    os.makedirs(list_dir, exist_ok=True)

    def have(kind, x):
        return os.path.exists(os.path.join(dest, 'en', kind, x['url']))

    kept = {}
    for kind in ('word', 'article'):
        kept[kind] = [x for x in manifests[kind] if have(kind, x)]

    # recommend_* 是 word/article 的子集，同样按实际存在过滤
    kept['recommend_word'] = [x for x in manifests['recommend_word'] if have('word', x)]
    kept['recommend_article'] = [x for x in manifests['recommend_article'] if have('article', x)]

    for kind, data in kept.items():
        path = os.path.join(list_dir, f'{kind}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)

    print(f'\n已更新清单 {list_dir}：'
          f'单词 {len(kept["word"])} 个，文章 {len(kept["article"])} 个'
          f'（推荐 {len(kept["recommend_word"])}/{len(kept["recommend_article"])}）')
    print('注意：清单是构建输入，需要重新构建镜像才会生效。')


def main():
    ap = argparse.ArgumentParser(description='下载官方词典数据到本地')
    ap.add_argument('--dest', default=os.path.join(ROOT, 'dicts'),
                    help='存放目录，默认 ./dicts（对应容器内 /app/dicts）')
    ap.add_argument('--category', nargs='*', metavar='分类',
                    help='只下载指定分类，如：中国考试 国际考试 青少年英语 代码练习')
    ap.add_argument('--max-size', type=float, metavar='MB',
                    help='跳过大于该体积的词典（MB）')
    ap.add_argument('--jobs', type=int, default=8, help='并发数，默认 8')
    ap.add_argument('--list', action='store_true', help='只列出清单，不下载')
    args = ap.parse_args()

    print('读取官方清单 ...')
    manifests = load_manifests()
    words, articles = manifests['word'], manifests['article']
    if not words:
        print('清单为空，可能是网络问题，请重试', file=sys.stderr)
        return 1
    print(f'  单词词典 {len(words)} 个，文章 {len(articles)} 个')

    jobs = [('word', x) for x in words] + [('article', x) for x in articles]
    if args.category:
        want = set(args.category)
        jobs = [(k, x) for k, x in jobs if (x.get('category') or '') in want or k == 'article']

    print(f'查询文件体积（{len(jobs)} 个）...')
    with ThreadPoolExecutor(args.jobs) as ex:
        sizes = dict(zip(
            [(k, x['url']) for k, x in jobs],
            ex.map(lambda kx: remote_size(kx[0], kx[1]['url']), jobs),
        ))

    if args.max_size:
        cap = args.max_size * 1024 * 1024
        before = len(jobs)
        jobs = [(k, x) for k, x in jobs if 0 < sizes[(k, x['url'])] <= cap or k == 'article']
        print(f'  按体积过滤：{before} -> {len(jobs)}')

    todo, skipped, total = [], 0, 0
    for kind, x in jobs:
        want_size = sizes[(kind, x['url'])]
        path = os.path.join(args.dest, 'en', kind, x['url'])
        if os.path.exists(path) and want_size and os.path.getsize(path) == want_size:
            skipped += 1
            continue
        todo.append((kind, x, want_size))
        total += want_size

    print(f'\n待下载 {len(todo)} 个，已有 {skipped} 个，共约 {total / 1024 / 1024:.1f} MB')
    if args.list:
        for kind, x, s in sorted(todo, key=lambda t: -t[2]):
            print(f'  {s / 1024 / 1024:>7.1f}M  [{x.get("category") or kind}] {x.get("name")}  ({x["url"]})')
        return 0
    if not todo:
        print('已是最新')
        return 0

    free = shutil.disk_usage(args.dest).free
    if free < total * 1.1:
        print(f'磁盘空间不足：需要约 {total / 1024 ** 3:.1f}G，可用 {free / 1024 ** 3:.1f}G', file=sys.stderr)
        return 1

    def work(kind, x, want_size):
        url = f'{BASE}/dicts/en/{kind}/{x["url"]}'
        data = get(url, timeout=180)
        if want_size and len(data) != want_size:
            raise ValueError(f'大小不符：期望 {want_size} 实得 {len(data)}')
        # 必须能解析成 JSON —— 万一 CDN 返回了错误页，不能把它当词典存下来
        json.loads(data.decode('utf-8'))
        out = os.path.join(args.dest, 'en', kind, x['url'])
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + '.part'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, out)  # 先写 .part 再改名，中断不会留下半个文件
        return len(data)

    done_n = done_b = 0
    failed = []
    with ThreadPoolExecutor(args.jobs) as ex:
        futs = {ex.submit(work, k, x, s): (k, x) for k, x, s in todo}
        for fut in as_completed(futs):
            kind, x = futs[fut]
            done_n += 1
            try:
                done_b += fut.result()
            except Exception as e:  # noqa: BLE001
                failed.append((x['url'], str(e)))
                print(f'  [{done_n}/{len(todo)}] {x["url"]} 失败: {e}', flush=True)
            else:
                print(f'  [{done_n}/{len(todo)}] {x.get("name")} '
                      f'({done_b / 1024 / 1024:.0f}/{total / 1024 / 1024:.0f}MB)', flush=True)

    print(f'\n完成：成功 {done_n - len(failed)}，失败 {len(failed)}，目录 {args.dest}')
    if failed:
        print('失败的可以直接重跑本脚本（已下好的会跳过）：')
        for u, e in failed[:20]:
            print(f'  {u}: {e}')

    write_manifests(manifests, args.dest)
    if failed:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
