#!/usr/bin/env python3
"""
自查部署结果：清单里声明的每个词库，实际都能取到、篇数/词数对得上。

为什么要比「声明 vs 实际」而不只是看 HTTP 200：上游在 public/dicts/ 下
带了两个样例词典，被烧进构建产物，而 nitro 的静态中间件排在自定义
/dicts/ 路由**之前** —— 同名的挂载文件永远取不到。表现就是清单说
nce1 有 72 篇、页面上只有 5 篇，而两个请求都是 200。

单看状态码查不出这类问题，必须比数量。

用法（在部署机上跑最快 —— 几百个词库要逐个下载，走局域网可能几分钟都跑不完，
走 127.0.0.1 一分钟出结果）：
    python3 scripts/check-live-dicts.py                      # 默认 127.0.0.1:3000
    python3 scripts/check-live-dicts.py --base http://192.168.1.10:8080
    TW_BASE=http://127.0.0.1:8080 python3 scripts/check-live-dicts.py
"""

import argparse
import json
import os
import sys
import urllib.request


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=60) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default=os.environ.get('TW_BASE', 'http://127.0.0.1:3000'),
                    help='站点根地址，也可用 TW_BASE 环境变量')
    ap.add_argument('--mine', nargs='*',
                    default=['ai-ml', 'life-science', 'spoken-daily',
                             'ted-spoken', 'ai-reading', 'life-science-read'],
                    help='自建词库的 enName。这些的数量差异算 FAIL，'
                         '上游词库的只作参考（上游自己就有 55 个对不上）')
    args = ap.parse_args()

    mine = set(args.mine)
    bad, diff, mine_seen = 0, [], []
    for kind in ('word', 'article'):
        try:
            manifest = get(args.base, f'/list/{kind}.json')
        except Exception as e:                       # noqa: BLE001
            print(f'[FAIL] 取不到 /list/{kind}.json: {e}')
            bad += 1
            continue
        print(f'--- {kind}: 清单 {len(manifest)} 条 ---')
        for e in manifest:
            # 官方清单里有的条目没有 enName，用 url 兜底
            label = e.get('enName') or e.get('name') or e['url']
            url = f'/dicts/{e["language"]}/{kind}/{e["url"]}'
            try:
                n = len(get(args.base, url))
            except Exception as err:                # noqa: BLE001
                print(f'  [FAIL] {label}: 取不到 {url} —— {err}')
                bad += 1
                continue
            ok = n == e.get('length')
            if str(label) in mine:
                mine_seen.append(str(label))
                print(f'  {"ok " if ok else "BAD"} {label}: 清单 {e.get("length")} 实际 {n}')
                bad += not ok
            elif not ok:
                diff.append((kind, label, e.get('length'), n))

    # 上游词库篇数对不上**不算部署失败**：官方数据自己就有一批对不上的
    # （NCE_4 清单写 48、文件里 47；Cambridge_JOIN_IN 写 1232、实际 1350）。
    # 自建词库不一样 —— 那是我自己生成的，对不上就是我的问题，算 FAIL。
    if diff:
        print(f'--- 上游词库与清单不一致 {len(diff)} 个（上游数据问题，仅供参考）---')
        for kind, label, want, got in diff[:5]:
            print(f'  {kind} {label}: 清单 {want} 实际 {got}')
        if len(diff) > 5:
            print(f'  ...另有 {len(diff) - 5} 个')

    missing = mine - set(mine_seen)
    if missing:
        print(f'[FAIL] 自建词库不在清单里: {", ".join(sorted(missing))}')
        bad += len(missing)

    print('所有词库都能取到，自建词库数量全对' if not bad else f'{bad} 处有问题')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
