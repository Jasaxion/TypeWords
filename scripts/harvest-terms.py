#!/usr/bin/env python3
"""
从真实语料里统计高频词，产出词表给 build-word-dict.py 用。

为什么不手写词表：手写只能凭印象列几十个词，覆盖面和排序都不可靠。
从领域语料里按词频统计出来的，才真的是「读这类文章会反复遇到的词」。

三个来源，都是公开 API，不需要 key：

  arxiv   arXiv API 的摘要。--query 指定分类，如 cat:cs.LG、cat:cs.CL
  pubmed  PubMed E-utilities 的摘要。--query 是检索词，如 "molecular biology"
  freq    OpenSubtitles 词频表（hermitdave/FrequencyWords，字幕语料）
          口语高频词就靠它 —— 字幕最接近日常对话

统计时会：
  - 扣掉停用词和「太常见的词」（用 --skip-top N 跳过通用高频榜前 N 名，
    不然统计出来全是 the/of/we/this，学不到东西）
  - 只保留出现在 --min-docs 篇以上的词，滤掉一次性的专有名词和笔误

用法：
  python3 scripts/harvest-terms.py arxiv --query cat:cs.LG cat:cs.CL cat:cs.AI \\
      --pages 12 --top 700 -o wordlists/ai.txt
  python3 scripts/harvest-terms.py pubmed --query "molecular biology" "cell biology" \\
      --pages 8 --top 600 -o wordlists/bio.txt
  python3 scripts/harvest-terms.py freq --skip-top 200 --top 800 -o wordlists/spoken.txt
"""

import argparse
import collections
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {'User-Agent': 'Mozilla/5.0 (TypeWords wordlist builder)'}

WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z'\-]*[a-zA-Z]")

# 缩写被截断后剩下的壳。字幕语料里 "couldn't" 会被切成 couldn，
# 这类残片不是单词，练它没意义。ECDICT 里恰好也收了一部分，
# 光靠「查不到就丢」拦不住，只能显式排除。
CONTRACTION_STEMS = set("""
couldn didn doesn isn wasn wouldn shouldn aren weren haven hasn hadn mustn needn
shan ain oughtn daren usedn gonna
""".split())

# 字幕语料的另一个问题：脏话频率很高（影视对白），
# 但不是「日常口语练习」想要的内容。列进来一并排除。
PROFANITY = set("""
fucking fuck shit damn bitch asshole bastard crap hell dick pussy fucked fucker
motherfucker goddamn ass whore slut cunt
""".split())

# arXiv 摘要里混着 LaTeX 命令和平台词。这些不是英语单词，
# 查词典也查不到，提前扔掉省得占词表名额。
LATEX_NOISE = set("""
textbf textit emph mathbf mathcal texttt textsc mathrm underline
href url cite ref eqref citep citet
arxiv github huggingface openreview
""".split())

# 功能词。领域词频里这些永远排前面，但没有学习价值。
# 不含实义词 —— 像 model、data 这种虽然常见，但确实是该领域要练的词，保留。
STOPWORDS = set("""
a an the and or but if then than that this these those there here of in on at to for from by with
without within into onto over under above below between among during before after while when where
which who whom whose what why how all any both each few more most other some such no nor not only
own same so too very can will just should now also as is are was were be been being being have has
had do does did doing would could may might must shall we our us you your they their them it its he
she his her i me my one two three first second new use used using show shown given via due
""".split())


def get(url, timeout=60, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception as e:  # noqa: BLE001 —— 网络问题一律重试
            last = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f'请求失败 {url}: {last}')


def fetch_arxiv(queries, pages, per_page=100):
    """arXiv API，返回摘要列表。Atom feed，用正则抠 <summary> 够了。"""
    docs = []
    for q in queries:
        for p in range(pages):
            url = ('https://export.arxiv.org/api/query?'
                   + urllib.parse.urlencode({
                       'search_query': q,
                       'start': p * per_page,
                       'max_results': per_page,
                       'sortBy': 'submittedDate',
                       'sortOrder': 'descending',
                   }))
            xml = get(url)
            found = re.findall(r'<summary>(.*?)</summary>', xml, re.S)
            if not found:
                break
            docs.extend(found)
            print(f'  {q} p{p + 1}: +{len(found)}（累计 {len(docs)}）')
            time.sleep(3)  # arXiv 明确要求间隔 3 秒
    return docs


def fetch_pubmed(queries, pages, per_page=100):
    """
    PubMed：先 esearch 拿 id，再 efetch 取摘要。

    必须用 retmode=xml 而不是 text：text 版把作者单位混在正文里，
    统计出来全是 university / department / pmid / conflict of interest 这类
    投稿样板话（实测过，词频榜前 40 名基本被它们占满）。
    xml 版能精确取到 ArticleTitle + Abstract/AbstractText。

    另外每篇摘要单独作为一个 doc 返回，count_terms 的 doc_freq 才有意义 ——
    整批塞成一个字符串的话，doc_freq 永远等于批次数，滤不掉一次性词。
    """
    import xml.etree.ElementTree as ET

    docs = []
    for q in queries:
        ids = []
        for p in range(pages):
            url = ('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?'
                   + urllib.parse.urlencode({
                       'db': 'pubmed', 'term': q, 'retmax': per_page,
                       'retstart': p * per_page, 'retmode': 'json',
                       'sort': 'date',
                   }))
            import json as _json
            got = _json.loads(get(url))['esearchresult'].get('idlist', [])
            if not got:
                break
            ids.extend(got)
            time.sleep(0.4)  # 无 api_key 时限 3 req/s

        added = 0
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            url = ('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?'
                   + urllib.parse.urlencode({
                       'db': 'pubmed', 'id': ','.join(chunk),
                       'rettype': 'abstract', 'retmode': 'xml',
                   }))
            try:
                root = ET.fromstring(get(url))
            except ET.ParseError as e:
                print(f'  {q}: 这批 XML 解析失败，跳过（{e}）', file=sys.stderr)
                time.sleep(0.4)
                continue
            for art in root.findall('.//PubmedArticle'):
                title = art.findtext('.//ArticleTitle') or ''
                body = ' '.join(
                    e.text or '' for e in art.findall('.//Abstract/AbstractText')
                )
                if body.strip():
                    docs.append(f'{title} {body}')
                    added += 1
            time.sleep(0.4)
        print(f'  {q}: {len(ids)} 个 id，取到 {added} 篇有摘要的（累计 {len(docs)}）')
    return docs


def fetch_freq(limit=50000):
    """OpenSubtitles 英文词频表，已按频率降序。返回 [(word, count)]。"""
    url = ('https://raw.githubusercontent.com/hermitdave/FrequencyWords/'
           'master/content/2018/en/en_50k.txt')
    out = []
    for line in get(url).splitlines():
        parts = line.split()
        if len(parts) == 2 and WORD_RE.fullmatch(parts[0]):
            out.append((parts[0].lower(), int(parts[1])))
        if len(out) >= limit:
            break
    print(f'  词频表：{len(out)} 条')
    return out


def load_skip_set(n):
    """
    取通用高频榜前 n 名作为「太简单，不用练」的排除集。

    复用同一份字幕词频表：排前面的就是 the/you/what 这类，
    对 AI/生物词表来说是噪声，对口语词表来说是「已经会了」。
    """
    if n <= 0:
        return set()
    return {w for w, _ in fetch_freq(n)}


def count_terms(docs, skip, min_docs, min_len=4):
    """
    统计词频 + 文档频率。

    doc_freq 用来滤掉「只在一篇里疯狂出现」的词（比如某篇论文自造的模型名），
    这类词占了原始词频榜相当一部分。
    """
    total = collections.Counter()
    doc_freq = collections.Counter()
    for d in docs:
        found = [w.lower() for w in WORD_RE.findall(d)]
        total.update(found)
        doc_freq.update(set(found))  # 每篇只算一次

    out = []
    for w, c in total.most_common():
        if len(w) < min_len or w in STOPWORDS or w in skip:
            continue
        if w in CONTRACTION_STEMS or w in PROFANITY or w in LATEX_NOISE:
            continue
        if doc_freq[w] < min_docs:
            continue
        out.append((w, c, doc_freq[w]))
    return out


def main():
    ap = argparse.ArgumentParser(description='从真实语料统计高频词，生成词表')
    ap.add_argument('source', choices=['arxiv', 'pubmed', 'freq'])
    ap.add_argument('--query', nargs='*', default=[],
                    help='arxiv: cat:cs.LG 等；pubmed: 检索词')
    ap.add_argument('--pages', type=int, default=5, help='每个 query 翻几页（每页 100）')
    ap.add_argument('--top', type=int, default=600, help='取前多少个词')
    ap.add_argument('--skip-top', type=int, default=0,
                    help='排除通用高频榜前 N 名（专业词表建议 300~800）')
    ap.add_argument('--min-docs', type=int, default=3,
                    help='至少出现在几篇文档里，滤掉一次性词')
    ap.add_argument('--min-len', type=int, default=4, help='最短词长')
    ap.add_argument('-o', '--output', required=True, help='输出词表路径')
    args = ap.parse_args()

    if args.source in ('arxiv', 'pubmed') and not args.query:
        print(f'{args.source} 需要 --query', file=sys.stderr)
        return 2

    print(f'排除通用高频前 {args.skip_top} 名' if args.skip_top else '不排除通用高频词')
    skip = load_skip_set(args.skip_top)

    if args.source == 'arxiv':
        docs = fetch_arxiv(args.query, args.pages)
        ranked = count_terms(docs, skip, args.min_docs, args.min_len)
        print(f'\n{len(docs)} 篇摘要，{len(ranked)} 个候选词')
    elif args.source == 'pubmed':
        docs = fetch_pubmed(args.query, args.pages)
        ranked = count_terms(docs, skip, args.min_docs, args.min_len)
        print(f'\n{len(docs)} 批摘要，{len(ranked)} 个候选词')
    else:
        # 词频表本身就是排好的，只需过滤。注意这条路径不经过 count_terms，
        # 所以排除规则要在这里重复一遍
        ranked = [(w, c, 1) for w, c in fetch_freq(args.top + args.skip_top + 5000)
                  if w not in skip and len(w) >= args.min_len
                  and w not in STOPWORDS
                  and w not in CONTRACTION_STEMS and w not in PROFANITY
                  and w not in LATEX_NOISE]
        print(f'\n{len(ranked)} 个候选词')

    picked = ranked[:args.top]
    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or '.', exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(f'# {args.source} {" ".join(args.query)}\n')
        f.write(f'# 按词频降序，取前 {len(picked)} 个；'
                f'排除通用高频前 {args.skip_top} 名，最少出现在 {args.min_docs} 篇文档\n')
        for w, c, df in picked:
            f.write(f'{w}\n')

    print(f'已写入 {args.output}（{len(picked)} 个词）')
    print('前 40：', ' '.join(w for w, _, _ in picked[:40]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
