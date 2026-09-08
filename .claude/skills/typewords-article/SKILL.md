---
name: typewords-article
description: Convert web pages, PDFs, RSS feeds, TED transcripts, JSON, or plain text into TypeWords article dictionaries, and build word dictionaries from real corpora. Use when the user wants to read or practice a specific article, paper, blog, or feed inside TypeWords, or wants to add a new vocabulary dictionary.
---

# 把任意文章导入 TypeWords

TypeWords 官方只带新概念英语 1-4。想练别的内容，用 `scripts/make-article-dict.py`
转换成词库格式。

这份文档记的**几乎全是实测踩出来的坑**，不是 API 说明 —— 脚本的参数看
`--help` 就行，这里写的是「为什么必须这么做」。

## 断句格式必须和前端对齐（最重要的约束）

前端 `genArticleSectionData()`（`app/core/hooks/article.ts`）这样解析：

```
text.split('\n\n')   ->  段落 section
    .split('\n')     ->  句子 sentence
```

`textTranslate` 用同样规则拆，然后**按下标一一对应**：第 i 段第 j 句的译文 =
`textTranslate` 第 i 段第 j 行。

所以**译文的段数、每段行数必须和原文完全一致**。多一行少一行，后面全部错位，
页面上表现为「译文跟原文对不上」。脚本里有 assert 保证这点，别绕过它。

由此推出两条容易忽略的要求：

1. **段落内不能有 `\n`**。网页源码常有软换行（paulgraham.com 每 70 列硬折一次），
   留在段落里会被前端当成句子分隔符，一句切成好几句。`strip_html` 用
   `re.sub(r'\s+', ' ', p)` 把 `\n` 一起合并掉。
2. **不能有空段落**。`filter(Boolean)` 会丢掉空段，导致下标偏移。

## 转换文章

```bash
S=scripts/make-article-dict.py

# 网页
python3 $S --name "Paul Graham 文集" --en-name pg-essays \
    --url https://paulgraham.com/greatwork.html --dest /tmp/twout --no-manifest

# TED 演讲（真人口语。TED 文稿是前端渲染的，普通 --url 抓不到，必须用 --ted）
... --ted https://www.ted.com/talks/xxx https://www.ted.com/talks/yyy

# PDF（论文）。PDF 段落常被硬换行切碎，加 --max-sentence-chars 200 拆长句。
# 论文一定要配 --strip-math，公式打不出来。实测 arXiv 1706.03762（Transformer）
# 出 252 句干净正文（作者邮箱、arXiv 页眉、注意力图的 `<EOS> <pad>` 刷屏都会被丢掉）
... --pdf paper.pdf --max-sentence-chars 200 --strip-math

# RSS。订阅只给摘要时加 --fetch-full 回原文页抓全文
... --rss https://lilianweng.github.io/index.xml --limit 8 --fetch-full

# JSON：[{"title","text","titleTranslate","textTranslate"}]，自带译文就不再机器翻译。
# 注意 textTranslate 必须**自己按句拆好、一句一行**（见上面的断句约束）：
#   text:          "First one. Second one."      -> 脚本自动拆成 2 行
#   textTranslate: "第一句。\n第二句。"           <- 你得自己拆，不拆会被丢掉
# 结构不匹配时脚本打一行 stderr 警告并丢弃该篇译文（原文保留），不会静默错行
... --json in.json

# 纯文本目录，每个 .txt 一篇，首行当标题
... --txt-dir ./talks

# 机器翻译（Google 免费接口）。不加则译文留空，可在页面里点「翻译」
... --translate

# LaTeX（论文、AI 博客）。$c$ 变成 c，整句是公式的丢掉
... --strip-math
```

脚本是**纯标准库**（只有 `--pdf` 需要 `pypdf`），不用装依赖。

`--translate` 逐句发请求 + 0.4s 间隔，2000 句要十几分钟。**放后台跑**，
别让工具调用超时。先用 `--dry-run` 看统计（几篇、几句、标题对不对）。

## 翻译接口会限流，**绝对不要并发**

免费接口按 IP 限流。同时跑两三个 `--translate` 一定触发 429，然后**连续几百句
全失败**，产出一篇全空的译文却「看起来成功了」。一次只跑一个，多个词库串行。

脚本里备了两个端点，gtx 挂了自动切 `clients5.google.com`（Chrome 划词翻译用的
那个，**限流额度是分开的** —— 实测 gtx 已经 429 时它还能正常返回）。想确认当前
能不能翻：

```bash
python3 -c "
import urllib.request,urllib.parse
u='https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=en&tl=zh-CN&q='+urllib.parse.quote('test sentence')
print(urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'Mozilla/5.0'}),timeout=15).read())"
```

被限到两个端点都不行时，**不要死等** —— 不带 `--translate` 先把原文建好，
译文在页面里点「翻译」按钮补（前端自己会调翻译）。原文可用比译文齐全重要。

## 抽取质量：先看再信

`strip_html` 是正则实现（没有 readability），对「正文在 `<p>` 里」的博客/新闻
够用，但要检查：

- **客户端渲染的站点抽不到东西**。实测 **VOA Learning English 完全不可用** ——
  正文不在 HTML 里，AMP 版也只有 CSS 里的 `wsw` 类名。TED 同样是前端渲染，
  所以单独写了 `--ted` 走 `__NEXT_DATA__`。
  判断方法：`curl 页面 | grep -c '<p'`，只有个位数就是客户端渲染。
- **模板残留**。`drop_boilerplate()` 已处理 ScienceDaily 的 `Date:/Source:/Summary:`
  行、BBC 的导航条、分享按钮、参考文献条目（`[12] Wei, J. ...`、BibTeX 块）。
  遇到新站点的新残留，往 `BOILERPLATE_RE` / `REFERENCE_RE` 加 pattern，
  **按开头匹配**，不要做全文关键词过滤（正文里正常提到 "source" 的句子不该被牵连）。
- **规则写对了也可能空转 —— 编码会把它废掉**。ScienceDaily 页尾「相关报道」
  每条形如 `Mar. 3, 2022 — 摘要...`，规则一直有、一直没生效：源站把 cp1252 的
  em dash 当字符存成了 **U+0097**（C1 控制字符）。页面本身是**合法 utf-8**，
  按声明解码完全正确，错在源站 —— 解码层查不出来，只能在归一化层修
  （`fix_c1()`，把 U+0080~U+009F 按 cp1252 还原）。
  后果不只是显示乱码：实测每篇尾部混进 6 段截断摘要，占全库 16%。
  **所以加完规则要验证它真的匹配上了**，别只看规则写得对不对：
  `print(bool(m.BOILERPLATE_RE.match(段落)))`，以及 `repr(段落[:80])` 看真实字节。
- **开头匹配挡不住「以机构名开头」的模板块**。ScienceDaily 每篇末尾的
  "Cite This Page" 有 MLA / APA / Chicago 三条，每条都从**机构名**起头
  （`University of X. "..." ScienceDaily. www.sciencedaily.com/...`），
  `BOILERPLATE_RE` 里那条 `cite this page` 永远匹配不到 —— 标题行本身太短，
  早被 `strip_html` 的长度过滤扔了。实测占某个库 376 段里的 36 段（10%）。
  这类块只能按**内容特征**认，`is_citation_block()` 用两条：
  `retrieved <日期> from` / `(accessed <日期>)` / 行首 URL 这种**引用标记**，
  或者「已知 TLD 的 URL 碎片行占全段 > 25%」。
  两个更宽的判据实测都误伤，别用：「段落含 `www.` 或 `.com/`」命中 5 段正经论述；
  「任意单词+句点单独成行算碎片」把 TED 的 `Yeah.` 和小标题
  `III. Inverting Consequentialist Reflection` 一起判掉。
  25% 这个占比阈值也是必需的，不能「有就删」：一段致谢正文末尾提了 `tagds.com`，
  碎片占比 1/5，低于阈值才保住。
- **同一个失败模式会出现好几次，找到一个就顺手查同类**。ScienceDaily 的
  "Journal Reference"（作者列表 + `DOI: 10.1126/...`）和上面的 "Cite This Page"
  一模一样：标题行短、被扔了，正文行从作者名起头，按开头匹配抓不到。
  这种块能用**唯一标记**认的时候就用标记，别去猜形状：`\bDOI:\s*10\.` /
  `doi.org/10.` 正文里不会出现，拿官方 NCE 275 篇回归命中 0 段；而「逗号分隔的
  人名占多数」这类形状判据会误伤正文里的致谢和列举。
  实测某个库里 13 段，DOI 尾巴还被断句切出 `aea3075` 这种孤立行。
- **黏在正文里的杂质只能按「行」剪，不能按「段」删**。TED 文稿的 `(Laughter)`
  `(Applause)` 是转写员标的现场反应，实测 2586 句里 24 句含提示，但**只有 2 句是
  整行只有提示** —— 另外 22 句和正文黏在一起（`(Laughter) I see you.`）。
  段落级过滤器（`BOILERPLATE_RE` 那一类）对这种情况只有两个结果：放过，或者
  把正文一起删。所以 `AUDIENCE_CUE` 在 `ted_transcript` 抽取时就 `sub` 掉，
  已建好的库走 `restrip` 的行级分支（`strip_cue_line`）。
  同理，规则**只列已知提示词**，别写成 `^\([A-Z]\w+\)`：讲者模仿语气的
  `(Higher pitch) Where did you leave it?` 是正文，正常插入语括号也是。
  剪译文时另有两个坑，见 `strip_cue_line` 的 docstring（成对破折号别误合、
  中文 `——` 是一个标点两个字符）。
- **有些块只能靠「跨段」判据认，单段判据必然误伤**。The Gradient 文末的
  "Works Cited" 是 MLA 全名格式（`Andreas, Jacob."Language Models..."Mit.edu, 2024`），
  `REFERENCE_RE` 那条作者判据要求名字是**缩写**（`Wei, J.`），全名不匹配；
  标题行 "Works Cited" 又太短被扔了。实测一篇 50 段里末尾 21 段全是文献（42%）。
  但**不能拿单段判据去删**：同一个库里 Extrinsic Hallucinations 末尾 7 段是
  「benchmark 逐个介绍」，每段都以 `TruthfulQA ( Lin et al. 2021 ) is designed to...`
  起头，段段命中单段判据，却是该留的好正文。所以 `works_cited_start()` 是三重判据：
  **位置**（只看文末）+ **长度**（连续区 ≥ 8 段，正好卡掉那 7 段误报）+
  **占比**（区内 ≥ 70% 是条目，容忍 `Designing an Intelligence .` 这种没有作者姓的）。
  单段判据里**年份是必需项**：实测文献 21/21 段有四位年份，正文 26/26 段没有。
  这种跨段规则在 `restrip` 里要放在**按段过滤之后**，用最终列表算占比，
  否则占比的分母和建库时不一样。
- **网址被断句切碎后，行首那半截不算「碎片」**。Lil'Log 每篇文末的自引块
  （`Weng, Lilian."..."Lil'Log (Jul 2026). https://lilianweng.github.io/posts/...`）
  被切成 6 行，只有 `github.` 和 `io/...` 命中 `URL_FRAGMENT`，占比 2/6 = 0.33
  看着够，但 `https://lilianweng.` 这行**不算**，正是它把占比压到阈值附近，
  13 篇里 7 篇全部漏过。补一条 `^\s*https?://` 之后 3/6 = 0.5 稳过。
  判据必须是**行首**（`^`）而不是「行内含 http」—— 正文中间放仓库地址很常见
  （实测有一句正文引了 karpathy/autoresearch 的地址），那是好句子。

**这三条（`works_cited_start` / `JOURNAL_REF` / `URL_FRAGMENT` 的行首 URL）
加起来把某个库的短行占比从 1.20% 压到 0.31%。** 每条都拿官方词库
（302 篇 / 2086 段）跑过误报回归，命中 0 段 —— 新规则**必须**先跑这个再上。

**怎么发现这类污染**：数**短行占比**，见下面「打字素材的质量」一节。

### 改了过滤规则：用 `restrip-article-dict.py`，不要重新翻译

翻译一个库半小时以上、限流、不能并发。规则改动如果只影响**段落取舍**
（`BOILERPLATE_RE` / `REFERENCE_RE` / `is_token_spam` / `is_citation_block` /
`works_cited_start` / `AUDIENCE_CUE` / `fix_c1`），直接原地清理：

```bash
python3 scripts/restrip-article-dict.py /tmp/twout/en/article/*.json
python3 scripts/check-article-dict.py   /tmp/twout/en/article/*.json
```

它会**同时**从 `text` 和 `textTranslate` 删掉同一批下标 —— 只删一边会让后面
每段译文错位，而段数仍然相等，checker 也查不出来。段数本来就不等的库它跳过。
影响**切句**的改动（`split_sentences`、`--max-sentence-chars`）它做不到，得重建。

它做两件事：**段级**（整段丢掉）和**行级**（剪 `(Laughter)` 这类黏在句子里的
杂质，英文和译文一起剪）。两者都是幂等的，重复跑不会越剪越少。

验证抽取结果的标准做法：

```python
import importlib.util
spec = importlib.util.spec_from_file_location('mad', 'scripts/make-article-dict.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

raw = m.fetch(url)
ps = m.drop_boilerplate([p for p in m.strip_html(raw).split('\n\n') if p.strip()])
print('paras:', len(ps), '| 含换行:', sum(1 for p in ps if '\n' in p))   # 必须是 0
for p in ps[:3]: print(repr(p[:110]))                                   # 肉眼看是不是正文
```

## 打字素材的质量：每一行都得是能打出来的

这是「抽到了正文」之外的一层要求 —— 抽出来的东西还得**适合逐字符敲**。
建完先跑一遍下面这个审计：

```bash
python3 - <<'PY'
import json,re
d=json.load(open('/tmp/twout/en/article/xxx.json'))
sents=[s for a in d for p in a['text'].split('\n\n') for s in p.split('\n')]
print('句数',len(sents))
print('残留 LaTeX/花括号:', sum(1 for s in sents if re.search(r'\$|\\[a-zA-Z]+|[{}]|\^',s)))
print('不含单词的行:',   sum(1 for s in sents if not re.search(r'[A-Za-z]{2,}',s)))
print('空句/空段:', sum(1 for s in sents if not s.strip()),
      sum(1 for a in d for p in a['text'].split('\n\n') if not p.strip()))
frag=[s for s in sents if len(re.findall(r'[a-zA-Z]{2,}',s))<2]
print(f'短行 {len(frag)} ({len(frag)/len(sents):.1%})', sorted(set(frag))[:8])
PY
```

各项的判断标准：

- **残留 LaTeX / 花括号 / `^` 必须是 0**（`--strip-math` 负责）。`$\mathcal{H}_{k-1}$`
  这种键盘上打不出来。脚本按**处理后的结果**判断去留：剥掉 `$` 还是正常英文句子的
  留下（`a context function $c$ is ...` → `... c is ...`），主语整个是公式的丢掉。
  实测 AI 博客 3806 句里有 455 句含数学，其中 277 句是纯公式（丢），
  178 句剥完可用（留）。
- **不含单词的行必须是 0**。来源是引号在句末标点外面：`...times 56789?" .` 被切成
  两段，后半段单独成「句」，页面上就是让你打一个引号加句点。`split_sentences`
  会把这种碎片、以及 `( Link )` / `[4]` 这类脚注标记并回上一句。
- **空句、空段必须是 0**，理由见上面的格式约束（下标错位）。
- **短行占比**是发现模板残留最省事的指标 —— 判据用「**少于 2 个英文单词**」，
  不要按字符长度（按长度会把合法口语短句 `Yeah.` 一起算进去，数字虚高到没法用：
  同一个 TED 库，按长度 <25 是 13%，按单词数是 1.8%）。
  书面文章超过 **5%** 就挨个看是什么；口语素材天然高一些，`Yeah.` `Zero.`
  是真的正文，别照着数字砍。
  实测某个库修完两轮（相关报道 16% + 引用块 10%）后 10.5% -> 0.1%。

另外两个已经修掉、但换新站点时可能再遇到的坑：

- **编码**：`fetch` 按响应头声明的 charset 解码，这一步没问题。真正的坑在**源站
  自己存错了** —— ScienceDaily 页面是合法 utf-8，但里面躺着 U+0097。
  解码正确，字符照样是打不出来的乱码，还会让靠破折号匹配的过滤规则空转。
  `fix_c1()` 在归一化层还原，见上面「抽取质量」一节。
- **长句切割留下的尾巴**：`--max-sentence-chars 200` 早期是贪心切（每段切满 200），
  余数经常只剩两三个词（`'pretraining).'`）。现在 `split_long` 先算需要几段再均分，
  并优先在逗号、分号处断。

## 已验证可用的素材源

| 用途 | 源 | 说明 |
|---|---|---|
| 日常口语 | TED 演讲 `--ted` | 真人讲的话，句式和书面文章不同；`(Laughter)` 这类现场提示会自动剪掉 |
| 口语/听力 | BBC Learning English `6-minute-english` | 对话体，`--url` 可抓 |
| AI 阅读 | `https://lilianweng.github.io/index.xml` | 需 `--fetch-full` + `--strip-math` |
| AI 评论长文 | `https://thegradient.pub/rss/` | feed 里就有全文（`content:encoded`） |
| 生命科学科普 | `sciencedaily.com/rss/plants_animals/biology.xml` 等 | 需 `--fetch-full`；同一天多个分区 RSS 会重复，记得去重 |
| 论文 | arXiv PDF | `--pdf` + `--max-sentence-chars 200` + `--strip-math` |

**不可用**：VOA Learning English（客户端渲染）、openai.com/blog/rss.xml（307）、
bair.berkeley.edu（超时）。

素材 URL 清单和重建命令在 `sources/`，想扩充就往对应 `.txt` 里加行后重跑。

## 发音：自己抓的文章库句子发音一定是机器音

不是 bug，是缺素材。前端有两条完全不同的路：

- **单词**：请求有道词典 `https://dict.youdao.com/dictvoice?audio=<word>&type=2`
  （`type=1` 英音）。这是**真人录音**库，自建词库照样能用 —— 实测自建的三个词库
  各抽 150 词，98%~100% 是真人录音。
- **文章句子**：`article.ts` 的 `playSentenceAudio` 只有在 `audioSrc` 和
  `audioPosition` 都有值时才播真实音频（官方新概念自带朗读 mp3 + `lrcPosition`
  逐句时间轴）。否则走 `else` 分支，把**整句**丢进上面那个**单词**接口 ——
  而那个接口**不接整句**，返回 120 字节的空响应，于是 `onerror` 触发，
  回退到浏览器 `speechSynthesis`。这就是机器音的来源。

所以抓来的网页文章没有朗读音频，句子发音只能是浏览器 TTS。想改善只能自己配
音频文件 + 逐句时间轴，或者在设置里换一个更自然的系统声色。

**顺便：怎么分辨某个词是真人录音还是机器合成** —— 按 mp3 字节数。真人录音
7~14KB 且大小不规则；机器合成的一律是 **480 的整数倍**（41280、35520、17760）。
`<500` 字节是查不到/报错。实测机器合成的多是专名和缩写（`Alex`、`mRNA`、
`t-cell`），屈折形式（`looks`、`showed`）反而都有真人录音。

## 加词库（不是文章）

`scripts/build-word-dict.py`。**不编造词条** —— 释义/音标/例句全部来自已下载的
官方词库或 ECDICT，查不到的词直接丢弃并列出来。

```bash
python3 scripts/build-word-dict.py --words wordlists/ai-ml.txt \
    --name "AI 与机器学习" --en-name ai-ml --category 专业领域 --tags 人工智能 \
    --ecdict path/to/ecdict.csv --dry-run
```

**两个数据源都得先准备好**：官方词库用 `scripts/fetch-dicts.py` 下载到 `dicts/`
（仓库里的 `dicts/` 是空的），ECDICT 是一个 63MB 的 csv，`--ecdict` 给路径。

先 `--dry-run` 看命中率（低于 50% 脚本自己会报错退出，那通常是词表写错了）。
实测三个自建词库 97.3% / 99.8% / 99.9% —— 丢掉的都是词典收不了的新造词
（`zero-shot` `pretrained` `vlms` `llms`），属于正常。

词表用 `scripts/harvest-terms.py` 从真实语料统计（arXiv / PubMed / 字幕词频），
**不要手写** —— 手写的覆盖面和排序都不可靠。建议加 `--fold-plurals`。

**坑**：官方词库里混着空壳条目（有 `word` 和 `id`，但 `trans` 全空；实测
`semantic`、`corpus`、`diffusion`、`verification`、`changed` 都是这样）。
本地命中会短路掉 ECDICT，产出「有词无释义」的条目。`is_usable()` 按
「有没有中文释义」判断，不是按「本地有没有这个词」。

**另一个坑**：官方词库里连 `https` 都收了（实测本地命中率 100%），所以
「查不到就丢」拦不住这类链接残片，只能在 `harvest-terms.py` 的 `LATEX_NOISE`
里显式排除。

### 词表里的屈折形式：只折 `-s`，别动 `-ing`/`-ed`

`fold_plurals` 一律留**词根**、丢屈折形，**不比词频**。最早写的是「留排名高的
那个」，被实测打回：`improves`/`remains` 的 `-s` 是动词三单不是复数，词典里
`improve` 才有完整释义；`process`/`processes` 里复数词频更高，按词频会把
`process` 删掉，留一个「process的复数」当词条。

**只折 `-s`。** `-ing`/`-ed` 在专业语料里是术语而不是屈折：`training`、
`learning`、`embedding`、`signaling`、`sequencing` 都必须留着，折掉就把领域
核心词删了。实测那一类有 35+30+25 个，全是该留的。

**别指望用 ECDICT 的 `exchange` 列（`0:` 段）做词形归并**，实测过，不行：

```
embedding      0:embed        <- 正是不该折的那类
signaling      0:signale      <- 这个"词根"根本不存在
significantly  (空)
training       0:training     <- 指向自己
```

只有最后那种反过来有用：`0:` 指向自身说明词典也把它当独立词条，可以当
「别折」的白名单；`0:` 的值本身不能当「该折成什么」的答案。

即便这样，**残留 5~10% 的屈折形是正常的**（`trained`/`train`、`studies`/`study`），
官方 CET4 是 1.2%。想更干净只能人工过一遍词表。

**词源/释义错配是上游数据的问题，不是建库脚本的**：实测官方 CET4 有 17.8% 的
词条 `etymology` 讲的是别的词（`data` 挂着 `dazzle` 的词源），CET6 17.6%，
自建的 13.5%~15.9% —— 比官方还低。别去「修」它。

**115 词没有音标**（多是 `dataset`、`fine-tuning`、`multimodal` 这类新词/合成词，
ECDICT 里就没有）。前端 `TypeWord.vue` 对音标有 `v-if` 守卫、列表页渲染空
`<span>`，不会报错也不影响练习，只是少一行音标。

## 页面分类：词库会分组，文章列表不会

**词库页**（`app/pages/(words)/dict-list.vue`）用 `groupBy(dict_list,'category')`
+ `groupByDictTags` 动态分组，`--category` / `--tags` 写什么就出现什么，
不用改前端。

**文章页**（`app/pages/(articles)/book-list.vue`）是**平铺的**，不分组 ——
那里的 `category` / `tags` 只进搜索框的匹配。所以给文章词库起名时别指望靠
category 归类，`--name` 本身要能认出来；而 `--category 口语` 的作用是让你搜
「口语」能搜到它。

两个字段**都不能省**：搜索里是 `item.category.toLowerCase()` 和
`item.tags.join('')`，没有就直接抛异常、搜索框废掉。

**还要注意 `/words` 和 `/articles` 首页只显示 `list/recommend_*.json`**
（一份人工挑的推荐列表，词库 15 条 / 文章 4 条），新增词库不会出现在那里 ——
要从「更多」进 `/dict-list`、`/book-list` 才看得到全部。找不到刚加的词库时
先确认是不是在看首页。

## 重建已部署的词库前：进度是按下标存的

`lastLearnIndex` 是**位置下标**，不是单词。原地替换一个已经在学的词库、
只要词序变了，「接着学」就指向另一批词 —— 页面不报错，进度悄悄错掉。

部署环境的 `data/typing-word-dict` 里存着每个词库的 `lastLearnIndex`，
对应 `enName` 是 0 才能原地重建；非 0 就换个新 `enName` 另建一个，旧的留着。

## 两条更新路径，别搞混

源于 nitro 的静态资源索引是**构建时**烧进产物的：

| 改什么 | 怎么生效 | 为什么 |
|---|---|---|
| `dicts/en/{word,article}/*.json` | **只要重启容器** | 走自定义 server route 从挂载目录读，不受索引约束 |
| `public/list/*.json`（清单） | **必须重新构建镜像** | 是构建输入。运行时替换会被按**旧 size 截断**，静默损坏 |

**`public/dicts/` 会遮挡挂载数据。** 上游仓库带了两个**样例**词典
`public/dicts/en/article/NCE_1.json`（只有 5 篇）和 `public/dicts/en/word/CET4_T.json`。
它们在 `public/` 下会被烧进构建产物，而静态中间件注册在自定义 `/dicts/` 路由
**之前** —— 于是同名的挂载文件永远取不到。实测后果：清单声明 `nce1` 有 72 篇，
页面实际只能读到 5 篇。**新增词典一律只放挂载目录 `dicts/`。**

## 完成后自查

```bash
# 结构自查（生成后、部署之前就跑，别等页面上看出问题）
python3 scripts/check-article-dict.py /tmp/twout/en/article/*.json

# 部署后：清单声明的篇数 == 实际取到的篇数
python3 scripts/check-live-dicts.py --base http://<host>:<port>
```

`check-live-dicts.py` 只看状态码没用 —— 被 `public/dicts/` 遮挡时两个请求都是
200，只有数量不一样。它逐个比「清单声明 vs 实际取到」，并把**自建词库**的差异算
FAIL、上游词库的只作参考（上游官方数据自己就有 55 个对不上，比如 `NCE_4`
清单写 48、文件里 47，那不是部署问题，别去改官方元数据）。新增了自建词库记得
同时更新脚本里 `--mine` 的默认列表，否则漏了也不会报。

**重建镜像之前跑这一步，`BAD` 是正常的**，别当成部署失败去回滚：`dicts/` 是
运行时挂载，改完立刻生效；`public/list/*.json` 烧在镜像里，要等重建。所以中间态
一定是「实际是新数量、清单是旧数量」。

`check-article-dict.py` 不要用「段数行数比一比」的土办法代替。它照抄前端
`genArticleSectionData()` 的解析，包括那处不对称：

```js
text.split('\n\n').filter(Boolean)   // 原文过滤空段
textTranslate.split('\n\n')[i]       // 译文按下标直取，不过滤
```

原文里有一个空段，前端就会把它压掉、译文却不会，从那段往后**所有译文全部对错行**，
而段数统计看起来还是「一致」的。脚本另外会查残留 LaTeX 和没有字母的孤立行
（`[14]`、`( Link )` 这种引用碎片打不出来也没意义）。退出码非 0 就别部署。

**句数故意不写在这里** —— 过滤规则一改就变（实测收紧 PDF 过滤后某次从 395 句
变 252 句），写死的数字只会变成错的参考。要看当前规模就跑
`check-article-dict.py`，清单里的 `length` 由部署脚本从文件现读。
