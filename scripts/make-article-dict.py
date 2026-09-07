#!/usr/bin/env python3
"""
把网页 / PDF / RSS / JSON / 纯文本转成 TypeWords 文章词库。

自部署站点只有 4 篇官方文章（新概念 1-4），想读别的只能自己转。
这个脚本负责转，配套的 skill 见 ~/.claude/skills/typewords-article/。

--- 关键：断句格式必须和前端对齐 ---

前端 genArticleSectionData()（app/core/hooks/article.ts）这样解析：

    text.split('\\n\\n')  ->  段落（section）
        .split('\\n')     ->  句子（sentence）

textTranslate 用同样的规则拆，然后**按下标一一对应**：
第 i 段第 j 句的翻译 = textTranslate 第 i 段第 j 行。

所以译文的段数、每段行数必须和原文完全一致。多一行少一行都会让后面
全部错位 —— 页面上表现为「译文跟原文对不上」。本脚本每篇都校验这一点，
对不上就丢弃这一篇的译文、保住原文（不中断整批）。

--- 用法 ---

  # 网页（自动抽正文）
  python3 scripts/make-article-dict.py --name "Paul Graham 文集" --en-name pg-essays \\
      --url https://paulgraham.com/greatwork.html

  # TED 演讲文稿（真人口语素材；TED 的文稿是前端渲染的，普通 --url 抓不到）
  python3 scripts/make-article-dict.py --name "TED 精选" --en-name ted-talks \\
      --ted https://www.ted.com/talks/xxx https://www.ted.com/talks/yyy

  # PDF（本地文件或 URL；需要 pdftotext 或 pypdf）
  python3 scripts/make-article-dict.py --name "某论文" --en-name my-paper --pdf paper.pdf

  # RSS/Atom 订阅，取最近 10 篇
  python3 scripts/make-article-dict.py --name "Hacker News Blogs" --en-name hn-blogs \\
      --rss https://example.com/feed.xml --limit 10

  # JSON：[{"title":..,"text":..,"titleTranslate":..,"textTranslate":..}, ...]
  python3 scripts/make-article-dict.py --name "我的材料" --en-name mine --json in.json

  # 纯文本目录，每个 .txt 一篇，首行当标题
  python3 scripts/make-article-dict.py --name "口语对话" --en-name talks --txt-dir ./talks

  # 加机器翻译（默认不翻译，留空让你在页面里点「翻译」）
  ... --translate

  # 论文 / AI 博客里有 LaTeX 时加上，把 $c$ 变成 c、整句是公式的丢掉
  ... --strip-math

--- 输出 ---

  {dest}/en/article/{en-name}.json    文章数据（运行时挂载，加文章只要重启容器）
  public/list/article.json            追加一条清单（构建输入，必须重新构建镜像）
"""

import argparse
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'}

# 与前端 isSentenceEnd() 的缩写表保持一致，避免 "Dr." 被当成句子结束
ABBREVIATIONS = ['Mr', 'Mrs', 'Ms', 'Dr', 'Prof', 'Sr', 'Jr', 'St', 'Co', 'Ltd',
                 'Inc', 'e.g', 'i.e', 'U.S.A', 'U.S', 'U.K', 'etc', 'vs', 'Fig',
                 'al', 'approx', 'cf', 'Eq']
ABBREV_RE = re.compile(r'\b(' + '|'.join(re.escape(a) for a in ABBREVIATIONS) + r')\.$', re.I)


def fetch(url, timeout=60, retries=3, binary=False):
    """
    抓一个 URL。文本按响应头声明的编码解码 —— 不能一律当 utf-8：
    ScienceDaily 发 windows-1252，硬解成 utf-8 会把 em-dash 变成 \\x97，
    页面上就是一个打不出来的乱码字符。
    """
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if binary:
                    return raw
                enc = r.headers.get_content_charset()
                if not enc:
                    m = re.search(br'charset=["\']?([\w-]+)', raw[:4096], re.I)
                    enc = m.group(1).decode('ascii', 'replace') if m else 'utf-8'
                try:
                    return raw.decode(enc, 'replace')
                except LookupError:             # 编码名瞎写的
                    return raw.decode('utf-8', 'replace')
        except Exception as e:  # noqa: BLE001 —— 网络问题一律重试
            last = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f'请求失败 {url}: {last}')


# cp1252 被当成 latin-1 塞进 utf-8 留下的残渣。U+0080~U+009F 是 C1 控制字符，
# 正常文本里根本不该出现，出现就一定是这种错误编码 —— 按 cp1252 还原回去。
#
# 实测 ScienceDaily：页面本身是**合法 utf-8**（0xC2 0x97），但里面那个码位是
# U+0097，也就是 cp1252 的 em dash 被当字符存了进去。所以按声明的 utf-8 解码
# 完全正确，错在源站，解码层查不出来。
#
# 这不只是「显示成乱码」那么无害：ScienceDaily 页尾"相关报道"每条形如
# "Mar. 3, 2022 — 摘要..."，BOILERPLATE_RE 靠那个破折号认它。破折号变成
# U+0097 就匹配不上，于是每篇文章尾部混进 6 段截断的摘要（实测占全库 16%），
# 而且都以 "..." 结尾 —— 不是正文，练打字没意义。
C1_MOJIBAKE = re.compile(r'[-]')


def fix_c1(text):
    if not C1_MOJIBAKE.search(text):
        return text
    return C1_MOJIBAKE.sub(
        lambda m: bytes([ord(m.group())]).decode('cp1252', 'replace'), text)


# ---------------------------------------------------------------- 正文抽取

def strip_html(raw):
    """
    从 HTML 里抽正文。

    没有 bs4 也能用（NAS 上就没装），所以全部用正则。思路是先砍掉
    script/style/nav/footer 这些整块，再按 <p>/<h*>/<br> 切段，最后去标签。
    比不上 readability，但对博客/新闻这类「正文在 <p> 里」的页面够用。
    """
    # 有些页面把正文包在 <article> 或 <main> 里，先缩小范围能少很多噪声
    for tag in ('article', 'main'):
        m = re.search(rf'<{tag}\b[^>]*>(.*?)</{tag}>', raw, re.S | re.I)
        if m and len(m.group(1)) > 500:
            raw = m.group(1)
            break

    # 整块删掉：这些标签里的文字不是正文
    for tag in ('script', 'style', 'nav', 'footer', 'header', 'aside', 'form',
                'noscript', 'svg', 'button', 'figure', 'iframe'):
        raw = re.sub(rf'<{tag}\b[^>]*>.*?</{tag}>', ' ', raw, flags=re.S | re.I)
    raw = re.sub(r'<!--.*?-->', ' ', raw, flags=re.S)

    # 块级标签转成段落分隔符，行内标签直接去掉
    raw = re.sub(r'<(br|/p|/div|/h[1-6]|/li|/tr|/blockquote)\s*/?>', '\n\n', raw, flags=re.I)
    raw = re.sub(r'<[^>]+>', ' ', raw)
    text = fix_c1(html.unescape(raw))

    # 逐段清理：合并空白，丢掉过短的（导航残留、版权行之类）
    paras = []
    for p in re.split(r'\n\s*\n', text):
        # 必须连 \n 一起合并：网页源码里的软换行（paulgraham.com 就是每 70 列
        # 硬折一次）如果留在段落里，会被前端当成句子分隔符 ——
        # 一句被切成好几句，译文按下标一一对应就全错位了。
        # \s 覆盖 \n、\t 和 nbsp，一次搞定。
        p = re.sub(r'\s+', ' ', p).strip()
        # 至少要有 40 个字符且包含句末标点，才像一段正文
        if len(p) >= 40 and re.search(r'[.!?]', p):
            paras.append(p)
    return '\n\n'.join(paras)


# 正文里混进来的模板残留。都是实测撞见的：
#   ScienceDaily 每篇开头有一行 "Date: ... Source: ... Summary: ..."
#   BBC Learning English 的导航条会被当成一段（"Home Level Topics ... Search"）
#   分享/订阅按钮的文字、cookie 提示
# 这些句子语法不完整，练打字没意义，而且会占掉第一段的位置。
BOILERPLATE_RE = re.compile(
    r'^(date:\s*\w+\s+\d|source:\s|summary:\s|home\s+level\s+topics|'
    r'the code has been copied|share on|follow us|sign up for|'
    r'subscribe to|cookies? (policy|settings)|all rights reserved|'
    r'copyright\s*©|advertisement$|related stories|read more:|'
    r'cite this page|journal reference|story source|'
    r'materials provided by|note:\s*content may be edited|'
    # ScienceDaily 页尾的"相关报道"列表：每条都是 "Mar. 3, 2022 — 正文摘要 ..."
    r'(jan|feb|mar|apr|may|june?|july?|aug|sept?|oct|nov|dec)\w*\.?\s+\d{1,2},'
    r'\s+\d{4}\s*[—–-])',
    re.I)


# 参考文献条目。技术博客和论文末尾动辄几十条，形如
#   [12] Jason Wei, et al. "Chain of Thought Prompting..." NeurIPS 2022.
# 它们不是句子（作者名 + 引号标题 + 会议名），练打字没价值，
# 却能占掉一篇文章三分之一的篇幅（实测 Lilian Weng 某篇 148 段里有 54 段是文献）。
REFERENCE_RE = re.compile(
    r'^(\[\d+\]|\(\d{4}\)|\d+\.\s+[A-Z][a-z]+,\s)'      # [12] / (2024) / 12. Wei,
    r'|^[A-Z][a-zA-Z\'`-]+,\s+[A-Z]\.'                  # Wei, J.
    r'|arxiv\s*preprint\s*arxiv:'                        # 正文极少这样写
    r'|^@\w+\{'                                          # BibTeX 块（博客常放「如何引用本文」）
    r'|^\s*\w+\s*=\s*\{'                                 # BibTeX 的字段行：title = {...}
    r'|\\url\{'                                          # 同上，howpublished = {\url{...}}
    r'|^\w+,\s+\w+\.\s+"[^"]+"\.\s',                     # Weng, Lilian. "Why We Think".
    re.I)


# PDF 首页/页眉页脚的元信息。论文 PDF 抽出来的头几段是
#   arXiv:1706.03762v7 [cs.CL] 2 Aug 2023
#   Ashish Vaswani∗ Google Brain avaswani@google.com
# 邮箱和 arXiv 编号里的 `.` 还会把断句切成 'com noam@google.' 这种碎片。
# 只认邮箱和 arXiv 号这两个明确标记，不去猜作者名 —— 正文里的人名不该被牵连。
#
# `<EOS>`、`<pad>` 这类特殊 token 来自注意力可视化图。图里的词有时是
# 打散交错的（'process <EOS> have making <pad> <pad> governments voting'），
# 连续重复判不出来，但这几个 token 正常英文里不会出现，可以直接认。
# 故意不收 `<s>` / `</s>`：NLP 论文正文里会正常讨论 "a <s> token marks the
# start of a sequence"，那是好句子，不该连坐。
PDF_META_RE = re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.]+|arxiv:\d{4}\.\d{4,}'
                         r'|<(eos|bos|pad|unk|mask)>', re.I)


# TED 文稿里的现场提示：`(Laughter)`、`(Applause)`、`(Music)`。
# 它们不是讲者说的话，是转写员标的现场反应，打出来没意义。
#
# 为什么必须在 ted_transcript 里删、不能交给 drop_boilerplate：实测 2586 句里
# 24 句含提示，其中**只有 2 句是整行只有提示**，另外 22 句和正文黏在一起
# （`(Laughter) I see you.`、`...you know — (Laughter) — an awkward interaction`）。
# 按段落取舍的过滤器要么放过它们、要么连正文一起删掉，只有在文本层剪掉才对。
#
# 只列**已知的**提示词，不写成 `^\([A-Z]\w+\)`：讲者说的话里也有整句括号
# （实测有 `(Higher pitch) Where did you leave...` 这种模仿语气的，属于正文；
# 也有正常的插入语括号）。宁可漏掉冷门提示词，不要吃掉正文。
AUDIENCE_CUE = re.compile(
    r'\(\s*(?:laughter|applause|cheers?|cheering|laughs?|laughing|sighs?|'
    r'music|singing|video|audio|recording|beatboxing|silence|pause|'
    r'clapping|booing|whistling|gasps?)\s*\)', re.I)

# 译文侧的同一批提示。**只有 restrip-article-dict.py 用它** —— 建库时是先删
# 提示再翻译，译文里根本不会出现。但已经建好的库译文里有 `（笑声）`，要跟
# 英文侧删同一处，否则一边有一边没有。规则放这里而不是放 restrip 里，
# 是为了让两份规则不会走偏（restrip 直接 import 本文件）。
AUDIENCE_CUE_ZH = re.compile(r'[（(]\s*(?:笑声|掌声|欢呼声?|鼓掌|音乐|歌声|'
                             r'视频|录音|音频|叹气|沉默|停顿|嘘声|口哨声|'
                             r'喘息声?|倒吸一口气)\s*[)）]')


def strip_cue_line(en, zh):
    """
    从一对「英文行 / 译文行」里剪掉现场提示。返回 (en, zh)，整行只有提示时返回
    (None, None)，调用方要把这一行从**两边**一起删掉。

    为什么不能只删英文那一行：前端 genArticleSectionData 对原文的行做
    filter(Boolean)、对译文按下标直取。英文删空了、译文还在，从这行往后
    全部错位，而段数、篇数都看不出异常。

    提示夹在句子中间时，两侧的破折号会剩下来 ——
    `you know — (Laughter) — an awkward` 剪完是 `you know — — an awkward`，
    译文那边更明显（`你知道————尴尬的互动`）。所以剪完还要合并破折号。

    **这一步必须只在真的剪掉了提示时做**，否则会误伤本来就有双破折号的正文：
    `A range of 10-20 percent — maybe more — was seen.` 里那两个破折号是
    一对插入语标记，中间隔着字，不该动 —— 但译文 `——也许更多——` 里两个
    中文破折号一旦被当成「连着的」就会被合掉。所以没匹配到提示就原样返回。

    中英文破折号规则不一样：英文 `— —` 合成一个 `—`；中文的 `——` 本身就是
    **一个**标点（占两个字符），所以要合成 `——` 而不是 `—`。
    """
    if not AUDIENCE_CUE.search(en):
        return en, zh
    e = AUDIENCE_CUE.sub(' ', en)
    e = re.sub(r'([—–-])\s*\1', r'\1', e)          # `— —` -> `—`
    e = re.sub(r'\s{2,}', ' ', e).strip()
    # 提示在句首时后面常跟着破折号/逗号
    e = re.sub(r'^[\s—–\-,;:]+', '', e)
    if not re.search(r'[A-Za-z]{2,}', e):      # 剪完没词了 = 整行都是提示
        return None, None
    z = AUDIENCE_CUE_ZH.sub('', zh)
    z = re.sub(r'—{3,}', '——', z)                  # `————` -> `——`（中文破折号是双字符）
    z = re.sub(r'\s{2,}', ' ', z).strip()
    z = re.sub(r'^[\s—–\-,;:，；：]+', '', z)
    # 译文空了不能留 ''，前端会当成"没有译文"；给一个空格占位
    return e, z or ' '


def is_token_spam(s):
    """
    重复词刷屏。PDF 里的注意力可视化图、对齐矩阵会被抽成
        we we we we are are are are missing missing missing missing
        <EOS> <EOS> <EOS> <pad> <pad> <pad> at layer 5 of 6
    语法上不成句，打起来也纯属折磨手指。

    判据是「参与连续重复的词占全段的比例」，两个更直觉的判据都试过、都错：

    1. 「最高频词占比 > 0.3」——- 误删口语。排比和强调恰恰是口语的特征：
           "I'm not thin enough, rich enough, beautiful enough, smart enough."
       这些词频高但不连着出现。

    2. 「有连续 3 次以上同词」—— 也误删口语，而且更隐蔽。实测 TED 那篇
       《How to speak so that people want to listen》里的发声练习段
           "we're going to go Ba, Ba, Ba, Ba, Ba, Ba, Ba, Ba."
           "with exaggerated la, la, la, la, la, la, la, la, la."
       连跑长度 8，比真正的可视化图（实测最长只有 4）还长 —— 一行练习
       就判掉整段 22 句正经演讲。**最长连跑长度根本不区分两者。**

    真正的区别是**密度**：可视化图整段都在刷同一个 token，正文只是偶尔
    有一行。实测 1706.03762（Transformer）的 7 段图注重复占比 57%~80%，
    上面那段 TED 是 8.2% —— 中间隔了一大截，取 0.4 作阈值。

    注意这个判断是**段落级**的（只在 drop_boilerplate 里按段调用）。
    单独一行 "Ba, Ba, Ba, Ba, Ba, Ba" 拿来判还是会算垃圾，实测三个词库
    里没有这样的独立段落，所以不额外处理；真遇到了就是漏了一段练习音节，
    不会牵连正文。也别想用标点区分（口语的重复带逗号）—— 图注里
    的重复 token 同样有独立的 ',' 和 '.'，实测 7 段里 3 段如此。
    """
    w = s.split()
    if len(w) < 6:
        return False
    dup = run = 1
    for i in range(1, len(w)):
        if w[i] == w[i - 1]:
            run += 1
            # 连到第 3 个才算重复，此时把前两个一起记上
            dup += 3 if run == 3 else (1 if run > 3 else 0)
        else:
            run = 1
    return dup / len(w) > 0.4


# "Cite This Page" 里的 MLA / APA / Chicago 三条引用格式。
# ScienceDaily 每篇末尾都有，实测占 life-science-read 全库 376 段里的 36 段（10%）。
#
# 为什么不能靠 BOILERPLATE_RE 的 "cite this page" 那条：那三段各自以**机构名**开头
# （"University of California - San Diego. (2026, September 5). ..."），
# 标题行才是 "Cite This Page"，而标题行短、早就被 strip_html 的长度过滤扔了。
#
# 判据分两半，都是实测挑出来的：
#   1. 三种格式各自的检索短语 —— APA 的 "Retrieved <Month> <D>, <YYYY> from"、
#      Chicago 的 "(accessed <Month> <D>, <YYYY>)"、MLA 把网址包在尖括号里
#      （'<www.' / '<https'）。正文不会这么写。
#   2. 网址被断句切碎后留下的**孤立域名片段行**占比 > 25%：
#      'www.' / 'sciencedaily.' / 'com / releases / 2026 / 09 / xxx.htm'。
#
# 两个更宽的判据试过、都误伤：
#   - 「段落里含 www. 或 .com/」：命中 ai-reading 5 段正经论述（正文引用网址很正常）。
#   - 「任意单词+句点单独成行」算片段：把 TED 的 'Yeah.' 'Fantastic.' 和
#     ai-reading 的小标题（'III. Inverting Consequentialist Reflection'）一起判掉。
#     所以片段只认 **www. 和已知 TLD**，不认任意单词。
# 占比而不是「有就删」也是必需的：ai-reading 有一段致谢正文末尾提了 'tagds.com'，
# 整段删掉就冤了（实测该段片段占比 1/5，低于阈值，保住了）。
CITE_MARK = re.compile(
    r'retrieved\s+\w+\s+\d{1,2},\s+\d{4}\s+from'
    r'|\(accessed\s+\w+\s+\d{1,2},\s+\d{4}\)'
    r'|<\s*(?:www\s*\.|https?)', re.I)

URL_FRAGMENT = re.compile(
    r'^\s*www\s*\.\s*$'
    r'|^\s*(?:com|org|net|edu|gov|io|ai)\s*(?:\.\s*$|/)'
    r'|^\s*[\w-]+\s*\.\s*(?:com|org|net|edu|gov|io|ai)\b', re.I)

# "Journal Reference" 块：ScienceDaily 每篇正文后面挂的原始论文出处，形如
#   Qingrong Li, Peini Hou, Michiko Kimoto, ... Science, 2026; 393 (6808)
#   DOI: 10.1126/science.aea3075
# 和 "Cite This Page" 一样躲过了 BOILERPLATE_RE（标题行 "Journal Reference"
# 太短、被长度过滤扔了，正文行从**作者名**起头），也躲过 CITE_MARK
# （里面没有 "retrieved from" 也没有裸 URL）。实测 life-science-read 340 段里
# 有 13 段、ai-reading 1147 段里有 1 段。这些是作者列表加期刊卷号，练打字没意义，
# DOI 尾巴还会被断句切出 'aea3075' 这种孤立行。
#
# 判据只用 DOI 标记，不去猜「像不像作者列表」：`\bDOI:\s*10.` 和 `doi.org/10.`
# 在正文里根本不会出现（拿官方 NCE_3 / NCE_4 共 107 篇做回归，命中 0 段）。
# 「逗号分隔的人名占多数」这类形状判据试过，会误伤正文里的致谢和列举。
JOURNAL_REF = re.compile(r'\bdoi:\s*10\.\d{4,}|\bdoi\.org/10\.', re.I)


def is_citation_block(p):
    if CITE_MARK.search(p) or JOURNAL_REF.search(p):
        return True
    lines = [l for l in p.split('\n') if l.strip()]
    if len(lines) < 2:
        return False
    frag = sum(1 for l in lines if URL_FRAGMENT.match(l))
    return frag / len(lines) > 0.25


def drop_boilerplate(paras):
    """
    去掉模板残留和参考文献段落。

    只按开头匹配（文献那条例外，arxiv preprint 出现在哪都是文献），
    不做全文关键词过滤 —— 正文里正常提到 "source" 的句子不该被牵连
    （科普文章里 "the source of the signal" 很常见）。
    """
    out = []
    for p in paras:
        s = p.strip()
        if BOILERPLATE_RE.match(s) or REFERENCE_RE.search(s):
            continue
        if PDF_META_RE.search(s) or is_token_spam(s):
            continue
        if is_citation_block(s):
            continue
        out.append(p)
    return out


def ted_transcript(url):
    """
    取 TED 演讲的英文字幕。

    为什么单独写：TED 的文稿是前端渲染的，页面 HTML 里没有正文，
    strip_html 抽出来是空的。真正的数据在 __NEXT_DATA__ 这个
    内联 JSON 里（paragraphs -> cues -> text）。

    TED 是练日常口语最好的素材之一 —— 是真人讲出来的话，
    句式和书面文章不一样。字幕里的软换行要去掉（cue 是按显示宽度切的，
    不是按句子），一个 paragraph 的所有 cue 拼成一段，再交给正常断句。
    """
    if '/transcript' not in url:
        url = url.rstrip('/') + '/transcript'
    raw = fetch(url)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S)
    if not m:
        raise RuntimeError('页面里没有 __NEXT_DATA__，TED 可能改版了')

    data = json.loads(m.group(1))

    def find_paragraphs(node, depth=0):
        """字幕节点埋得比较深，而且路径会变，递归找 cues 结构最稳。"""
        if depth > 10:
            return None
        if isinstance(node, dict):
            if isinstance(node.get('paragraphs'), list) and node['paragraphs']:
                if isinstance(node['paragraphs'][0], dict) and 'cues' in node['paragraphs'][0]:
                    return node['paragraphs']
            for v in node.values():
                r = find_paragraphs(v, depth + 1)
                if r:
                    return r
        elif isinstance(node, list):
            for v in node[:60]:
                r = find_paragraphs(v, depth + 1)
                if r:
                    return r
        return None

    paras_raw = find_paragraphs(data)
    if not paras_raw:
        raise RuntimeError('这个演讲没有英文文稿')

    paras = []
    for p in paras_raw:
        # cue 内部也有换行（字幕按两行显示切的），一起压平
        joined = ' '.join((c.get('text') or '') for c in p.get('cues') or [])
        joined = AUDIENCE_CUE.sub(' ', joined)
        joined = re.sub(r'\s+', ' ', joined).strip()
        # 去掉提示后可能开头就剩标点（'— an awkward interaction' 那种）
        joined = re.sub(r'^[\s—–\-,;:]+', '', joined)
        if len(joined) >= 40:
            paras.append(joined)

    title = ''
    tm = re.search(r'<meta property="og:title" content="([^"]+)"', raw)
    if tm:
        title = html.unescape(tm.group(1)).replace(' | TED Talk', '').strip()
    return title or html_title(raw), paras


def html_title(raw):
    m = re.search(r'<h1\b[^>]*>(.*?)</h1>', raw, re.S | re.I) or \
        re.search(r'<title\b[^>]*>(.*?)</title>', raw, re.S | re.I)
    if not m:
        return ''
    t = html.unescape(re.sub(r'<[^>]+>', ' ', m.group(1)))
    return re.sub(r'\s+', ' ', t).strip()


def pdf_to_text(path):
    """
    PDF 取文字。按可用性依次尝试，都没有就报错让用户装一个。

    pdftotext -layout 效果最好（poppler），pypdf 是纯 Python 的兜底。
    注意：扫描版 PDF 里没有文字层，两种都取不出东西 —— 那种需要 OCR，
    不在这个脚本的范围内，会在下面明确报错而不是产出一篇空文章。
    """
    if shutil.which('pdftotext'):
        out = subprocess.run(['pdftotext', '-layout', '-enc', 'UTF-8', path, '-'],
                             capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout

    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        return '\n\n'.join((pg.extract_text() or '') for pg in reader.pages)
    except ImportError:
        pass

    raise RuntimeError(
        'PDF 解析需要 pdftotext（brew install poppler / apt install poppler-utils）'
        '或 pypdf（pip install pypdf），两个都没找到')


def clean_pdf_text(text):
    """
    PDF 文本的典型毛病：整段被硬换行切碎、页码单独一行、跨行连字符断词。

    处理策略：把「不是以句末标点结尾的换行」当成假换行接回去。
    论文的公式、表格没法完美处理，但正文段落基本能还原。
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'-\n([a-z])', r'\1', text)        # 跨行连字符：inter-\nnational -> international
    text = re.sub(r'\n\s*\d+\s*\n', '\n\n', text)     # 单独一行的页码

    paras = []
    for block in re.split(r'\n\s*\n', text):
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        if not lines:
            continue
        merged = ''
        for line in lines:
            if not merged:
                merged = line
            elif re.search(r'[.!?:;]$', merged):
                merged += '\n' + line   # 真换行（上一行是完整句子），先留着
            else:
                merged += ' ' + line    # 假换行，接回去
        for p in merged.split('\n'):
            p = re.sub(r'\s+', ' ', p).strip()
            if len(p) >= 40 and re.search(r'[.!?]', p):
                paras.append(p)
    return '\n\n'.join(paras)


def parse_feed(xml, limit):
    """
    RSS 2.0 和 Atom 都支持。优先取全文字段
    （content:encoded / content），没有才退回 description / summary。
    """
    import xml.etree.ElementTree as ET
    NS = {
        'atom': 'http://www.w3.org/2005/Atom',
        'content': 'http://purl.org/rss/1.0/modules/content/',
    }
    root = ET.fromstring(xml.encode('utf-8') if isinstance(xml, str) else xml)

    items = root.findall('.//item') or root.findall('.//atom:entry', NS)
    out = []
    for it in items[:limit]:
        title = (it.findtext('title') or it.findtext('atom:title', '', NS) or '').strip()
        body = (it.findtext('content:encoded', '', NS)
                or it.findtext('atom:content', '', NS)
                or it.findtext('description')
                or it.findtext('atom:summary', '', NS)
                or '')
        link = it.findtext('link') or ''
        if not link:
            le = it.find('atom:link', NS)
            if le is not None:
                link = le.get('href') or ''
        out.append({'title': title, 'html': body, 'link': link.strip()})
    return out


# ---------------------------------------------------------------- 数学公式

GREEK = ('alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu '
         'nu xi pi rho sigma tau upsilon phi chi psi omega').split()

# \mathcal{H} 这类"只是换个字体"的包装，内容本身是可读的，剥掉外壳即可
FONT_CMD_RE = re.compile(
    r'\\(?:mathcal|mathbb|mathbf|mathrm|mathit|mathsf|text|textit|textbf|'
    r'operatorname|emph)\s*\{([^{}]*)\}')

MATH_SPAN_RE = re.compile(r'\$([^$]*)\$')
MATH_PAREN_RE = re.compile(r'\\\(([^)]*)\\\)')
# 处理完还剩这些，就说明是真公式，不是几个变量名
LEFTOVER_RE = re.compile(r'\\[a-zA-Z]+|[{}]|\^|\\\\')


def _unwrap_math(inner):
    """把一小段 LaTeX 转成能用键盘打出来的纯文本。"""
    s = FONT_CMD_RE.sub(r'\1', inner)
    s = FONT_CMD_RE.sub(r'\1', s)                       # 嵌套一层的情况
    s = re.sub(r'\\(%s)\b' % '|'.join(GREEK), r'\1', s)  # \rho -> rho
    s = re.sub(r'\\(?:times|cdot)\b', ' x ', s)
    s = re.sub(r'\\(?:to|rightarrow|Rightarrow|mapsto)\b', ' -> ', s)
    s = re.sub(r'\\(?:leq|le)\b', ' <= ', s)
    s = re.sub(r'\\(?:geq|ge)\b', ' >= ', s)
    s = re.sub(r'\\(?:approx|sim)\b', ' ~ ', s)
    s = re.sub(r'\\(?:dots|ldots|cdots)\b', '...', s)
    s = re.sub(r'_\{([^{}]*)\}', r'_\1', s)             # h_{t+1} -> h_t+1
    s = re.sub(r'\\[,;:!]|\\ ', ' ', s)                 # 排版用的细空格
    s = s.replace('\\{', '{').replace('\\}', '}')
    return re.sub(r'\s+', ' ', s).strip()


def demath(sentence):
    """
    句子里的 LaTeX -> 可打字文本；整句就是公式的返回 None（调用方丢弃）。

    分两种处理，因为它们的价值不同：
    - `a context function $c$ is ...` —— 数学只是个变量名，剥掉 `$` 就是
      正常英文句子，留下来练打字没问题。
    - `$\\rho_s = \\{\\rho_1,\\dots,\\rho_m\\}$ are static components.` ——
      主语整个是公式，剥完还是一串符号，用户没法打也读不出来，丢掉。

    判据是处理后的结果本身（有没有残留命令、字母占比、有几个英文单词），
    而不是原句里出现了什么 —— 按输出判断才能同时接住两种情况。
    """
    if '$' not in sentence and '\\' not in sentence:
        return sentence
    out = MATH_PAREN_RE.sub(lambda m: _unwrap_math(m.group(1)), sentence)
    out = MATH_SPAN_RE.sub(lambda m: _unwrap_math(m.group(1)), out)
    out = re.sub(r'\s+([,.;:)])', r'\1', out)            # 剥括号留下的空格
    out = re.sub(r'\s+', ' ', out).strip()

    if LEFTOVER_RE.search(out):
        return None
    if not out:
        return None
    letters = sum(c.isalpha() or c.isspace() for c in out)
    if letters / len(out) < 0.72:                       # 符号密度太高
        return None
    if len(re.findall(r'[A-Za-z]{3,}', out)) < 3:       # 几乎没有成词
        return None
    return out


# ---------------------------------------------------------------- 断句

# 纯引用/脚注标记，本身不是句子：( Link ) / [4] / (Feb 2024).
CITATION_MARK_RE = re.compile(
    r'[\[(]\s*(?:link|source|pdf|\d+|'
    r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{4})'
    r'\s*[\])][.,]?', re.I)


def split_sentences(paragraph):
    """
    把一段拆成句子列表。

    规则对齐前端 splitEnArticle2()：句末标点切分，但缩写、小数、
    百分号、金额不算句子结束。前端还会再跑一遍自己的断句，
    我们这里拆得一致，它就不会再改动，lrcPosition 之类的下标才稳。

    合并条件要看**上一句结尾的最后一个 token**，不能只看整句结尾匹不匹配
    缩写表。原因是 findall 会把 "e.g." 切成 "e." 和 "g." 两段：
    第一段以 "e." 结尾（不在表里，但是个单字母缩写），合并后变成 "e. g."
    以 "g." 结尾（也不在表里）—— 两次都匹配不上，结果 e.g. 被拆成两"句"。
    实测 3806 句里有 511 个这样的碎片（'J.'、'e.'、'g.'）。
    所以额外把「单个字母 + 点」和「括号没闭合」也当成未结束。

    合并时的空格要**照抄原文**，不能按 token 形状猜：findall 的切点就在
    句号后面，空白全跑到下一段开头去了，strip 掉就丢了这个信息。
    照抄的话 "3.5" 拼回 "3.5"、"e.g." 拼回 "e.g."、"Dr. Smith" 拼回
    "Dr. Smith"，一条规则全对；猜的话小数会变成 "3. 5"。

    最后还要把**不含任何单词的碎片**并回上一句。典型来源是引号在句末标点
    外面：`... times 56789?" .` 被切成 `... 56789?` 和 `" .`，后者单独成
    一"句"，页面上就是一行只让你打一个引号加句点。实测三个词库里有 146 个
    这种（'.'、')'、'[4]'、'" .'）。判据是「有没有 2 个以上连续字母」，
    这样 TED 口语里合法的短句（'Yeah.'、'Nope.'）不会被误伤。
    另外单独并掉 `( Link )`、`[4]` 这类纯引用标记 —— 它们有字母但不是句子。

    并接时的空格同样照抄原文，`56789?` + `" .` 要拼成 `56789?" .`。
    """
    parts = re.findall(r'[^.!?]+[.!?]*', paragraph)
    sentences = []          # [(原文里的前导空白, 句子)]
    for raw in parts:
        seg = raw.strip()
        if not seg:
            continue
        lead = ' ' if raw[:1].isspace() else ''
        prev = sentences[-1][1] if sentences else ''
        unfinished = prev and (
            ABBREV_RE.search(prev)              # 缩写表：Dr. / etc. / e.g
            or re.search(r'\b\d+\.$', prev)     # 小数、编号：3.
            or re.search(r'\b[A-Za-z]\.$', prev)  # 单字母：e. / g. / J.（人名缩写）
            or re.search(r'\([^)]*$', prev)     # 括号没闭合，句子肯定没完
        )
        if unfinished:
            sentences[-1] = (sentences[-1][0], prev + lead + seg)
        else:
            sentences.append((lead, seg))

    merged = merge_fragments(sentences)
    # 段首就是碎片时上面没得可并，往后并
    if len(merged) > 1 and not re.search(r'[A-Za-z]{2,}', merged[0][1]):
        merged[1] = (merged[0][0], merged[0][1] + merged[1][0] + merged[1][1])
        merged.pop(0)
    return [s for _, s in merged]


def merge_fragments(sentences):
    """
    把碎片并回上一句。输入输出都是 [(前导空白, 句子)]。

    单独抽出来是因为要跑两次：断句之后一次，`split_long` 切完长句之后再一次。
    切长句可能正好在碎片前面下刀，把已经并好的 `... sense. [14]` 重新拆成
    `... sense.` 和 `[14]` —— 实测 ai-reading 里就有一处。
    """
    out = []
    for lead, seg in sentences:
        fragment = (not re.search(r'[A-Za-z]{2,}', seg)   # '.'、')'、'" .'
                    or CITATION_MARK_RE.fullmatch(seg))   # '( Link )'、'[4]'
        if out and fragment:
            out[-1] = (out[-1][0], out[-1][1] + lead + seg)
        # 句末标点在引号里面（口语转写最常见）：`...struggle?" And I thought`
        # 被切成 `...struggle?` 和 `" And I thought`。那个收尾引号属于上一句，
        # 留在行首会让用户先打一个莫名其妙的引号。实测 TED 里有 117 处。
        elif out and re.match(r'^["“”]\s*\S', seg):
            out[-1] = (out[-1][0], out[-1][1] + seg[0])
            out.append((lead, seg[1:].lstrip()))
        else:
            out.append((lead, seg))
    return out


def split_long(s, limit):
    """
    把超长句子切成几段，每段不超过 limit。

    不能贪心地「每次切满 limit」—— 那样最后一段是余数，经常只剩两三个词，
    页面上出现一行 'pretraining).' 这种。实测 200 上限下有 150 个这样的尾巴。
    改成先算需要几段、再按均分目标切，长度就均匀了。

    优先在标点（逗号、分号、破折号）后面断，其次退回空格 —— 从中间断开的
    半句读起来比在逗号处断更别扭。
    """
    if len(s) <= limit:
        return [s]
    n = -(-len(s) // limit)                     # 向上取整，需要几段
    target = -(-len(s) // n)                    # 每段的目标长度
    out = []
    while len(s) > limit:
        window = min(limit, int(target * 1.15))
        # 目标点附近的标点优先；找不到就用最后一个空格
        cut = max((s.rfind(c, target // 2, window) for c in (', ', '; ', ' — ', ' - ')),
                  default=-1)
        cut = cut + 1 if cut > 0 else s.rfind(' ', target // 2, window)
        if cut <= 0:
            cut = s.rfind(' ', 0, limit)
        if cut <= 0:
            break                               # 一个空格都没有，只能整段留着
        out.append(s[:cut].strip())
        s = s[cut:].strip()
    if s:
        out.append(s)
    return out


def build_text(paragraphs, max_sentence_chars=0, strip_math=False):
    """
    段落列表 -> (text, [[句子]...], 丢弃的公式句数)。

    text 用 '\\n' 连句子、'\\n\\n' 连段落，正好是前端期望的格式。

    strip_math 会丢句子，所以**丢空了的段落必须整段跳过**，
    不能留下空 section —— 空段落会让 '\\n\\n' 连出连续分隔符，
    前端拆出空段，译文下标全错。
    """
    sections = []
    dropped = 0
    for p in paragraphs:
        sents = split_sentences(p)
        if strip_math:
            kept = []
            for s in sents:
                r = demath(s)
                if r is None:
                    dropped += 1
                else:
                    kept.append(r)
            sents = kept
        if max_sentence_chars:
            # 超长句子（PDF 里常见）拆开，不然一行要打好几百字符。
            # 切完再并一次碎片：下刀点可能正好在 `[14]` 前面，把断句时
            # 已经并好的碎片重新拆出来。
            out = []
            for s in sents:
                out.extend(split_long(s, max_sentence_chars))
            sents = [s for _, s in merge_fragments([(' ', s) for s in out])]
        if sents:
            sections.append(sents)

    text = '\n\n'.join('\n'.join(s) for s in sections)
    return text, sections, dropped


# ---------------------------------------------------------------- 翻译

def _gtx_translate(line):
    """网页翻译用的端点。返回结构是 [[[译文, 原文, ...], ...], ...]。"""
    url = ('https://translate.googleapis.com/translate_a/single?'
           + urllib.parse.urlencode({
               'client': 'gtx', 'sl': 'en', 'tl': 'zh-CN',
               'dt': 't', 'q': line,
           }))
    data = json.loads(fetch(url, timeout=30, retries=1))
    return ''.join(seg[0] for seg in data[0] if seg and seg[0]).strip()


def _clients5_translate(line):
    """Chrome 划词翻译用的端点。限流额度和 gtx 分开，返回是 ["译文"]。"""
    url = ('https://clients5.google.com/translate_a/t?'
           + urllib.parse.urlencode({
               'client': 'dict-chrome-ex', 'sl': 'en', 'tl': 'zh-CN', 'q': line,
           }))
    data = json.loads(fetch(url, timeout=30, retries=1))
    while isinstance(data, list) and data:      # 有时多套一层
        if isinstance(data[0], str):
            return data[0].strip()
        data = data[0]
    return ''


def translate_lines(lines, sleep=0.4):
    """
    逐句翻译。用 Google 的免费 gtx 接口 —— 不需要 key，
    这是浏览器里网页翻译用的那个。

    逐句而不是整段发：译文必须和原文句子一一对应，
    整段发回来的句子数不保证一致，会让下标全乱。

    失败的句子返回空串占位（保住行数），调用方负责换成空白，
    末尾报告失败数。

    **429 要单独处理**：免费接口按 IP 限流，超了会连续几百句全失败，
    产出一篇全空的译文却"看起来成功了"。所以遇到 429 就指数退避重试，
    连续失败太多次直接放弃整篇（返回值里的 failed 会等于总数，
    上层据此丢弃译文）。别并发跑多个转换任务，这是最容易触发限流的做法。

    准备了两个端点：gtx 挂了自动切 clients5（Chrome 划词翻译用的那个，
    **限流额度是分开的**，实测 gtx 429 时它还能正常返回）。两个都挂了
    才算失败。切换是按端点记状态的，不是每句都重试一遍死掉的端点。
    """
    endpoints = [_gtx_translate, _clients5_translate]
    out, failed, streak = [], 0, 0
    for i, line in enumerate(lines):
        if not line.strip():
            out.append('')
            continue

        text = ''
        for attempt in range(4):
            try:
                text = endpoints[0](line)
                break
            except Exception as e:  # noqa: BLE001 —— 单句失败不该毁掉整篇
                if '429' not in str(e) or attempt == 3:
                    break
                if len(endpoints) > 1:
                    endpoints.append(endpoints.pop(0))   # 换端点，别死等
                    print(f'    被限流（429），换用 {endpoints[0].__name__}',
                          file=sys.stderr)
                    continue
                wait = 5 * (2 ** attempt)   # 5s / 10s / 20s
                print(f'    被限流（429），等 {wait}s 再试', file=sys.stderr)
                time.sleep(wait)

        out.append(text)
        if text:
            streak = 0
        else:
            failed += 1
            streak += 1
            # 连着 30 句都失败，说明不是偶发问题，继续打只是浪费时间
            if streak >= 30:
                print(f'    连续 {streak} 句失败，放弃这篇的翻译', file=sys.stderr)
                rest = len(lines) - len(out)
                out.extend([''] * rest)
                return out, failed + rest
        if (i + 1) % 50 == 0:
            print(f'    翻译 {i + 1}/{len(lines)}')
        time.sleep(sleep)  # 免费接口，别打太快
    return out, failed


def translate_article(sections, title):
    """
    翻译一篇文章，返回 (titleTranslate, textTranslate, 失败句数)。

    textTranslate 的结构严格按 sections 重建：同样的段数、同样的每段行数。

    失败的句子要用空格占位，**不能留空字符串** —— '\\n'.join(['', ''])
    是 '\\n\\n'，正好是段落分隔符，会在段落中间凭空多出一个段边界，
    后面所有译文的下标全错。用 ' ' 占位则是 ' \\n '，结构不受影响，
    页面上那句显示为空白（诚实反映"这句没翻出来"）。
    """
    flat = [s for sec in sections for s in sec]
    translated, failed = translate_lines([title] + flat)
    title_cn, body = translated[0], translated[1:]
    body = [t if t.strip() else ' ' for t in body]

    # 按原结构切回去
    out, pos = [], 0
    for sec in sections:
        out.append('\n'.join(body[pos:pos + len(sec)]))
        pos += len(sec)
    return title_cn, '\n\n'.join(out), failed


# ---------------------------------------------------------------- 组装

def make_article(idx, title, paragraphs, max_sentence_chars, do_translate,
                 strip_math=False):
    text, sections, math_dropped = build_text(
        paragraphs, max_sentence_chars, strip_math)
    if not text.strip():
        return None

    title = re.sub(r'\s+', ' ', title or '').strip() or f'Article {idx + 1}'
    if math_dropped:
        print(f'    丢弃 {math_dropped} 句纯公式')
    title_cn, text_cn, failed = '', '', 0
    if do_translate:
        total = sum(len(s) for s in sections)
        print(f'  翻译《{title[:40]}》（{total} 句）')
        title_cn, text_cn, failed = translate_article(sections, title)
        if failed:
            print(f'    {failed}/{total} 句翻译失败，留空白占位'
                  f'（页面里可以点「翻译」补）')
        # 全军覆没通常是被限流了（并发跑多个任务最容易触发）。
        # 一篇全是空白的译文没有价值，不如干脆不要，让页面显示"未翻译"。
        if failed >= total:
            print('    整篇都没翻出来（大概被限流了），丢弃译文')
            title_cn, text_cn = '', ''

    # 结构校验：译文的段数/每段行数必须和原文一致，否则前端会错位。
    # 不用 assert 中断整批 —— 一篇有问题不该让前面几十篇的翻译白跑。
    if text_cn:
        a = [len(s.split('\n')) for s in text.split('\n\n')]
        b = [len(s.split('\n')) for s in text_cn.split('\n\n')]
        if a != b:
            print(f'    译文结构不匹配（原文 {len(a)} 段 vs 译文 {len(b)} 段），'
                  f'丢弃译文保住原文', file=sys.stderr)
            title_cn, text_cn = '', ''

    return {
        'id': idx + 1,
        'title': title,
        'titleTranslate': title_cn,
        'text': text,
        'textTranslate': text_cn,
        'audioSrc': '',       # 没有配音。前端对空字符串是容忍的，只是不能跟读
        'lrcPosition': None,  # 没有音频自然也没有时间轴
        'question': None,
        'nameList': None,
        'quote': None,
        'userId': None,
    }


def upsert_manifest(entry, kind='article'):
    """按 url 覆盖或追加，重跑脚本不会产生重复条目。"""
    path = os.path.join(ROOT, 'public', 'list', f'{kind}.json')
    data = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)

    for i, x in enumerate(data):
        if x.get('url') == entry['url']:
            entry['id'] = x.get('id', entry['id'])  # 保住已有的学习进度
            data[i] = entry
            break
    else:
        data.append(entry)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    return path, len(data)


def gather_sources(args):
    """各种输入源统一成 [{'title':..., 'paragraphs':[...]}]。"""
    docs = []

    for url in args.url or []:
        print(f'抓取 {url}')
        raw = fetch(url)
        body = strip_html(raw)
        if not body:
            print('  没抽到正文，跳过', file=sys.stderr)
            continue
        docs.append({'title': html_title(raw),
                     'paragraphs': drop_boilerplate(body.split('\n\n'))})

    for url in args.ted or []:
        print(f'取 TED 文稿 {url}')
        try:
            title, paras = ted_transcript(url)
        except Exception as e:  # noqa: BLE001 —— 单个演讲失败不该毁掉整批
            print(f'  失败，跳过：{e}', file=sys.stderr)
            continue
        docs.append({'title': title, 'paragraphs': drop_boilerplate(paras)})
        print(f'  + {title[:50]}（{len(paras)} 段）')
        time.sleep(0.5)

    for src in args.pdf or []:
        print(f'解析 PDF {src}')
        if src.startswith(('http://', 'https://')):
            tmp = os.path.join('/tmp', f'twpdf-{abs(hash(src))}.pdf')
            with open(tmp, 'wb') as f:
                f.write(fetch(src, binary=True))
            src, cleanup = tmp, tmp
        else:
            cleanup = None
        try:
            body = clean_pdf_text(pdf_to_text(src))
            if not body:
                print('  取不到文字（可能是扫描版，需要 OCR），跳过', file=sys.stderr)
                continue
            name = os.path.splitext(os.path.basename(src))[0]
            docs.append({'title': name,
                         'paragraphs': drop_boilerplate(body.split('\n\n'))})
        finally:
            if cleanup and os.path.exists(cleanup):
                os.remove(cleanup)

    for feed in args.rss or []:
        print(f'解析订阅 {feed}')
        for it in parse_feed(fetch(feed), args.limit):
            # description 常常只是摘要，有 --fetch-full 就回原文页抓全文
            body = strip_html(it['html'])
            if args.fetch_full and it['link']:
                try:
                    full = strip_html(fetch(it['link']))
                    if len(full) > len(body):
                        body = full
                except Exception as e:  # noqa: BLE001
                    print(f'  抓全文失败，用摘要：{e}', file=sys.stderr)
            if body:
                docs.append({'title': it['title'],
                             'paragraphs': drop_boilerplate(body.split('\n\n'))})
                print(f'  + {it["title"][:50]}')
            time.sleep(0.3)

    for path in args.json or []:
        print(f'读取 JSON {path}')
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        for it in (data if isinstance(data, list) else [data]):
            body = it.get('text') or it.get('content') or ''
            if not body:
                continue
            docs.append({
                'title': it.get('title', ''),
                # 不过 drop_boilerplate：JSON 是自己准备的材料，
                # 想留什么是自己的事，脚本不该偷偷删段落
                'paragraphs': [p for p in re.split(r'\n\s*\n', body) if p.strip()],
                # JSON 里自带译文就直接用，不再机器翻译
                'titleTranslate': it.get('titleTranslate', ''),
                'textTranslate': it.get('textTranslate', ''),
            })

    for d in args.txt_dir or []:
        for path in sorted(glob.glob(os.path.join(d, '*.txt'))):
            with open(path, encoding='utf-8') as f:
                content = f.read().strip()
            if not content:
                continue
            # 约定：首行是标题，空行之后是正文
            lines = content.split('\n')
            title = lines[0].strip()
            body = '\n'.join(lines[1:]).strip() or content
            docs.append({
                'title': title,
                # 同 --json，自备的 txt 不过滤
                'paragraphs': [p for p in re.split(r'\n\s*\n', body) if p.strip()],
            })
            print(f'  + {title[:50]}')

    return docs


def main():
    ap = argparse.ArgumentParser(
        description='把网页/PDF/RSS/JSON/文本转成 TypeWords 文章词库',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--name', required=True, help='词库显示名')
    ap.add_argument('--en-name', required=True, help='英文标识，同时作文件名')
    ap.add_argument('--description', default='')
    ap.add_argument('--category', default='文章学习', help='分类，页面按它分组')
    ap.add_argument('--tags', nargs='*', default=[])
    ap.add_argument('--cover', default='', help='封面图路径，如 /imgs/covers/x.png')

    ap.add_argument('--url', nargs='*', help='网页地址，可多个')
    ap.add_argument('--ted', nargs='*', metavar='URL',
                    help='TED 演讲地址，取英文文稿（真人口语，练日常表达用）')
    ap.add_argument('--pdf', nargs='*', help='PDF 文件路径或地址')
    ap.add_argument('--rss', nargs='*', help='RSS/Atom 订阅地址')
    ap.add_argument('--json', nargs='*', help='JSON 文件，见文件头说明')
    ap.add_argument('--txt-dir', nargs='*', help='txt 目录，每文件一篇，首行标题')

    ap.add_argument('--limit', type=int, default=10, help='每个订阅取几篇，默认 10')
    ap.add_argument('--fetch-full', action='store_true',
                    help='订阅只给摘要时，回原文页抓全文')
    ap.add_argument('--translate', action='store_true',
                    help='机器翻译（Google 免费接口）。不加则译文留空，可在页面里点「翻译」')
    ap.add_argument('--max-sentence-chars', type=int, default=0, metavar='N',
                    help='拆分超过 N 字符的长句，0 表示不拆。PDF 建议 200')
    ap.add_argument('--dest', default=os.path.join(ROOT, 'dicts'))
    ap.add_argument('--no-manifest', action='store_true',
                    help='只写文章数据，不动 public/list/article.json。'
                         '在开发机上转换、再把 json 传到服务器时用 —— '
                         '仓库里的清单是个 1 条的占位文件，真正的清单在部署机上，'
                         '在这边 upsert 会把它覆盖成错的')
    ap.add_argument('--strip-math', action='store_true',
                    help='把 LaTeX 公式转成可打字的纯文本（$c$ -> c），'
                         '整句就是公式的直接丢弃。抓论文/AI 博客时加上，'
                         '不然会出现一堆打不出来的 $\\mathcal{H}_{k-1}$')
    ap.add_argument('--dry-run', action='store_true', help='只打印统计，不写文件')
    args = ap.parse_args()

    # 文件名会进 URL，受 server/routes/dicts 的 SAFE_NAME 白名单约束
    if not re.match(r'^[A-Za-z0-9_-]+$', args.en_name):
        print(f'--en-name 只能包含字母数字和 - _：{args.en_name}', file=sys.stderr)
        return 2
    if not any([args.url, args.ted, args.pdf, args.rss, args.json, args.txt_dir]):
        print('至少要给一个来源：--url / --ted / --pdf / --rss / --json / --txt-dir',
              file=sys.stderr)
        return 2

    docs = gather_sources(args)
    if not docs:
        print('没有取到任何内容', file=sys.stderr)
        return 1

    print(f'\n共 {len(docs)} 篇，开始转换')
    articles = []
    for i, d in enumerate(docs):
        # JSON 输入自带译文时不重复翻译
        pre_cn = d.get('textTranslate')
        art = make_article(i, d['title'], d['paragraphs'],
                           args.max_sentence_chars,
                           args.translate and not pre_cn,
                           args.strip_math)
        if not art:
            continue
        if pre_cn:
            art['titleTranslate'] = d.get('titleTranslate', '')
            art['textTranslate'] = pre_cn
            a = [len(s.split('\n')) for s in art['text'].split('\n\n')]
            b = [len(s.split('\n')) for s in pre_cn.split('\n\n')]
            if a != b:
                print(f'  《{art["title"][:30]}》自带译文结构不匹配'
                      f'（原文 {a} vs 译文 {b}），已丢弃译文', file=sys.stderr)
                art['textTranslate'] = ''
        art['id'] = len(articles) + 1
        articles.append(art)

    if not articles:
        print('没有生成任何文章', file=sys.stderr)
        return 1

    total_s = sum(len(s.split('\n')) for a in articles for s in a['text'].split('\n\n'))
    total_w = sum(len(a['text'].split()) for a in articles)
    have_cn = sum(1 for a in articles if a['textTranslate'])
    print(f'\n{len(articles)} 篇 / {total_s} 句 / 约 {total_w} 词，其中 {have_cn} 篇有译文')
    for a in articles[:8]:
        print(f'  [{a["id"]}] {a["title"][:56]}'
              f'（{sum(len(s.split(chr(10))) for s in a["text"].split(chr(10) * 2))} 句）')
    if len(articles) > 8:
        print(f'  ...另有 {len(articles) - 8} 篇')

    if args.dry_run:
        print('\n--dry-run，未写入任何文件')
        return 0

    out_dir = os.path.join(args.dest, 'en', 'article')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f'{args.en_name}.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False)
    print(f'\n已写入 {out_path}（{os.path.getsize(out_path):,} 字节）')

    entry = {
        'id': args.en_name,
        'enName': args.en_name,
        'name': args.name,
        'description': args.description or args.name,
        'categoryId': 0,
        'url': f'{args.en_name}.json',
        'length': len(articles),
        'language': 'en',
        'translateLanguage': 'zh_CN',
        'version': 1,
        'type': 'article',
        'isDefault': False,
        'recommended': False,
        'userId': None,
        'cover': args.cover or None,
        'hidden': False,
        'category': args.category,
        'tags': args.tags or [args.category],
    }
    if args.no_manifest:
        print('\n--no-manifest：没有改清单。把上面这个 json 传到部署机的 '
              'dicts/en/article/ 后，在那边补一条清单：')
        print('  ' + json.dumps(entry, ensure_ascii=False))
        return 0

    path, total = upsert_manifest(entry, 'article')
    print(f'清单已更新 {path}（共 {total} 个文章库）')
    print('清单是构建输入 —— 需要 docker compose up -d --build 才会生效。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
