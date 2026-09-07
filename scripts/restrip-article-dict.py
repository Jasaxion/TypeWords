#!/usr/bin/env python3
"""
用当前的过滤规则重新清理**已建好**的文章词库，不重新翻译。

为什么需要它：翻译走 Google 免费接口，一个库要跑半小时以上，而且按 IP 限流、
不能并发。所以每次改了过滤规则（BOILERPLATE_RE / is_token_spam / fix_c1）
都重跑一遍抓取+翻译，代价太大。这个脚本只删段落，译文原样保留。

**两边必须删同一批下标。** 段落是「英文段 + 译文段」按下标一一对应的
（前端 genArticleSectionData 就是这么配对的），只删英文那边会让后面每一段的
译文都错位 —— 而且段数还是相等的，check-article-dict.py 也看不出来。

只能删段落，改不了段落划分：新规则如果是**切句**层面的（split_sentences、
--max-sentence-chars），这里帮不上忙，得重新建库。

用法：
    python3 scripts/restrip-article-dict.py /tmp/twout/en/article/*.json
    python3 scripts/check-article-dict.py  /tmp/twout/en/article/*.json   # 再查一遍
"""

import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_rules():
    """
    把 make-article-dict.py 当模块导入，复用它的过滤规则。

    文件名带连字符，import 不了，只能走 spec_from_file_location。
    这样规则只有一份，不会和主脚本走偏 —— 复制一份过来迟早对不上。
    """
    path = os.path.join(ROOT, 'scripts', 'make-article-dict.py')
    spec = importlib.util.spec_from_file_location('mad', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def restrip(path, m):
    with open(path, encoding='utf-8') as f:
        data = json.load(f)

    total = dropped = cues = 0
    for a in data:
        ts = m.fix_c1(a.get('text') or '').split('\n\n')
        tt = m.fix_c1(a.get('textTranslate') or '').split('\n\n')
        if len(ts) != len(tt):
            # 本来就错位的库不动 —— 删段只会把问题埋得更深
            print(f'  跳过《{(a.get("title") or "")[:36]}》：段数本来就不等 '
                  f'({len(ts)} vs {len(tt)})，先查原因', file=sys.stderr)
            continue
        total += len(ts)
        keep = [i for i, p in enumerate(ts)
                if not m.BOILERPLATE_RE.match(p)
                and not m.is_token_spam(p)
                and not m.is_citation_block(p)]

        # 文末 Works Cited 区是**跨段**判据（要算占比），得在按段过滤之后、
        # 拿最终的段落列表来算，顺序和 drop_boilerplate 里保持一致。
        cut = m.works_cited_start([ts[i] for i in keep])
        if cut is not None:
            keep = keep[:cut]

        dropped += len(ts) - len(keep)
        ts, tt = [ts[i] for i in keep], [tt[i] for i in keep]

        # 行级：TED 的现场提示。段落级过滤器碰不到它们 —— 实测 22/24 处是和正文
        # 黏在一起的，整段删掉就把正文一起删了。
        for pi, (pe, pz) in enumerate(zip(ts, tt)):
            le, lz = pe.split('\n'), pz.split('\n')
            if len(le) != len(lz):
                # 段内行数不等：这一段的译文本来就对不上，别再动它
                continue
            if not any(m.AUDIENCE_CUE.search(x) for x in le):
                continue
            oe, oz = [], []
            for e, z in zip(le, lz):
                e2, z2 = m.strip_cue_line(e, z)
                if e2 is None:              # 整行都是提示，两边一起删
                    cues += 1
                    continue
                oe.append(e2)
                oz.append(z2)
            if oe:                          # 整段都是提示的话保持原样，交给人看
                ts[pi], tt[pi] = '\n'.join(oe), '\n'.join(oz)

        a['text'] = '\n\n'.join(ts)
        a['textTranslate'] = '\n\n'.join(tt)

    name = os.path.basename(path)
    if not dropped and not cues:
        print(f'{name}: {total} 段，没有要删的')
        return 0

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    note = f'，另剪掉 {cues} 行纯现场提示' if cues else ''
    print(f'{name}: {total} 段 -> 删掉 {dropped} 段{note}')
    return dropped + cues


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    m = load_rules()
    # 不要写 sum(restrip(p, m) for p in ...)：生成器里一个文件出错，
    # 后面的就不处理了，看起来像「只有一个文件有内容」。全部跑完再汇总。
    n = [restrip(p, m) for p in sys.argv[1:]]
    print(f'共 {len(n)} 个文件，删掉 {sum(n)} 段。'
          f'接着跑 check-article-dict.py 确认对齐没坏。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
