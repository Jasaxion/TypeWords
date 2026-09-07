#!/usr/bin/env python3
"""
校验文章词库能被前端正确解析。

照抄 genArticleSectionData()（app/core/hooks/article.ts）的逻辑，
而不是"大概检查一下"—— 前端有一处不对称很容易踩：

    text.split('\\n\\n').filter(Boolean)        <- 原文过滤空段
    textTranslate.split('\\n\\n')[i]            <- 译文按下标直取，不过滤

所以原文里有空段会让两边的下标错开（原文被压缩、译文没有），
译文从那一段之后全部对错。这个脚本按前端的规则重放一遍，
逐句比对下标，报告实际会显示错位的位置。

用法：
    python3 scripts/check-article-dict.py /tmp/twout/en/article/*.json
"""

import json
import re
import sys


def frontend_sections(text):
    """原文：两级 split 都过滤空值（和前端一致）。"""
    out = []
    for sec in filter(None, text.strip().split('\n\n')):
        lines = [s.strip() for s in sec.strip().split('\n')]
        out.append([s for s in lines if s])
    return [s for s in out if s]


# 残留 LaTeX 的判据。不能简单地把 `{`、`}` 算进去 —— 正文里
# `output values: {yes, no, continue}` 是正常英文，花括号键盘上也有。
# 真正打不出来的是反斜杠命令、`$` 包裹、上下标这些。
LATEX_LEFTOVER_RE = re.compile(r'\$|\\[a-zA-Z]+|\\[{}]|[_^]\{|\\\\')


def check(path):
    d = json.load(open(path))
    problems = []
    blank_cn = 0

    for a in d:
        title = a['title'][:38]
        secs = frontend_sections(a['text'])

        # 打字素材本身的硬性要求
        for i, sec in enumerate(secs):
            for j, s in enumerate(sec):
                if not re.search(r'[A-Za-z]{2,}', s):
                    problems.append(f'{title} 第{i + 1}段第{j + 1}句没有单词: {s!r}')
                if re.search(LATEX_LEFTOVER_RE, s):
                    problems.append(f'{title} 第{i + 1}段第{j + 1}句有残留公式: {s[:60]!r}')

        # 原文里有空段/空行 -> 前端会过滤掉，译文下标随之错位
        raw_secs = a['text'].strip().split('\n\n')
        if len(raw_secs) != len(secs):
            problems.append(f'{title} 有空段落（{len(raw_secs)} -> 过滤后 {len(secs)}），'
                            f'译文会从那里开始错位')

        cn = a.get('textTranslate') or ''
        if not cn:
            continue

        # 译文：不过滤，严格按下标取（和前端一致）
        cn_secs = cn.split('\n\n')
        if len(cn_secs) != len(secs):
            problems.append(f'{title} 段数不符：原文 {len(secs)} 段 vs 译文 {len(cn_secs)} 段')
            continue
        for i, (sec, cn_sec) in enumerate(zip(secs, cn_secs)):
            cn_lines = cn_sec.split('\n')
            if len(cn_lines) != len(sec):
                problems.append(f'{title} 第{i + 1}段行数不符：'
                                f'原文 {len(sec)} 句 vs 译文 {len(cn_lines)} 行')
            # 翻译失败的句子留空行占位（下标不会错，页面上那句没有中文）。
            # 不算缺陷 —— 结构是对的，页面里点「翻译」能补上 —— 但要报数量。
            blank_cn += sum(1 for l in cn_lines if not l.strip())

    n_sent = sum(len(s) for a in d for s in frontend_sections(a['text']))
    n_cn = sum(1 for a in d if a.get('textTranslate'))
    tag = 'FAIL' if problems else ' ok '
    extra = f'（其中 {blank_cn} 句译文空缺）' if blank_cn else ''
    print(f'[{tag}] {path}：{len(d)} 篇 / {n_sent} 句 / {n_cn} 篇有译文{extra}')
    for p in problems[:15]:
        print('        ', p)
    if len(problems) > 15:
        print(f'         ...另有 {len(problems) - 15} 处')
    return not problems


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(0 if all(check(p) for p in sys.argv[1:]) else 1)
