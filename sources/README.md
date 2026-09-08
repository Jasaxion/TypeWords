# 文章词库的素材清单

每个 `.txt` 是一个文章词库的来源 URL，一行一个。这些 URL 是**逐个验证过能抓到
正文的**（很多站点正文是客户端渲染的，抽出来是空的），所以单独存下来，
想扩充词库时往对应文件里加行、重跑命令就行。

| 文件 | 词库 | 抓取方式 |
|---|---|---|
| `ted-spoken.txt` | TED 日常口语 | `--ted`（文稿在 `__NEXT_DATA__` 里，普通 `--url` 抓不到） |
| `ai-reading.txt` | AI 研究阅读 | `--url` + `--strip-math` |
| `life-science-read.txt` | 生命科学科普 | `--url` |

重建命令（`$(tr '\n' ' ' < ...)` 把清单展开成参数）：

```bash
S=scripts/make-article-dict.py

python3 $S --name "TED 日常口语" --en-name ted-spoken --category 口语 --tags TED \
    --ted $(tr '\n' ' ' < sources/ted-spoken.txt) \
    --max-sentence-chars 200 --translate --dest /tmp/twout --no-manifest

python3 $S --name "AI 研究阅读" --en-name ai-reading --category 专业阅读 --tags 人工智能 \
    --url $(tr '\n' ' ' < sources/ai-reading.txt) \
    --strip-math --max-sentence-chars 200 --translate --dest /tmp/twout --no-manifest

python3 $S --name "生命科学科普" --en-name life-science-read --category 专业阅读 --tags 生命科学 \
    --url $(tr '\n' ' ' < sources/life-science-read.txt) \
    --max-sentence-chars 200 --translate --dest /tmp/twout --no-manifest
```

**三条命令必须串行**，不能同时跑 —— 翻译走 Google 免费接口，按 IP 限流，
并发会连续几百句全失败、产出空译文却看起来成功了。

`--no-manifest` 是必须的：仓库里的 `public/list/article.json` 只是上游的占位文件，
真正的清单在部署环境上。详见 `.claude/skills/typewords-article/SKILL.md`。

篇数/句数会随过滤规则变化，**不要把某次的数字当成基准** ——
清单里的 `length` 一律由 `scripts/deploy-article-dicts.py` 从生成的 json 现读。
建完先跑 `python3 scripts/check-article-dict.py /tmp/twout/en/article/*.json`，
非 0 就别传。

## 单词词表

`wordlists/*.txt` 是 `harvest-terms.py` 从真实语料统计出来的词表。建词库需要
两个数据源，仓库里都不带：

- **官方词库**：`python3 scripts/fetch-dicts.py --dest dicts` 下载（仓库里的
  `dicts/` 是空的）
- **ECDICT**：一个 63MB 的 csv，`--ecdict` 显式给路径

```bash
python3 scripts/build-word-dict.py --words wordlists/ai-ml.txt \
    --name "AI 与机器学习" --en-name ai-ml --category 专业领域 --tags 人工智能 \
    --ecdict path/to/ecdict.csv --dry-run
```

先 `--dry-run` 看命中率，低于 50% 脚本自己会报错退出（通常是词表写错了）。

**注意 `--ecdict` 默认是 `<repo>/build-cache/`**，csv 放在别处就必须显式给路径。
词库文件如果是以 root 身份写的，建完要 `chown` 成容器的 uid:gid，否则容器读不到
（表现为页面上词库点开是空的）。

## 改了过滤规则怎么办

别重新翻译（半小时以上、限流、不能并发）。只影响段落取舍的规则改动
（`BOILERPLATE_RE` / `REFERENCE_RE` / `is_token_spam` / `is_citation_block` /
`works_cited_start` / `AUDIENCE_CUE` / `fix_c1`）用：

```bash
python3 scripts/restrip-article-dict.py /tmp/twout/en/article/*.json
python3 scripts/check-article-dict.py   /tmp/twout/en/article/*.json
```

它从 `text` 和 `textTranslate` 删掉**同一批下标**。影响切句的改动
（`split_sentences`、`--max-sentence-chars`）它做不到，得按上面的命令重建。

清理完**要重新部署**：`deploy-article-dicts.py` 覆盖远端那份，
否则本地干净、线上还是脏的（`dicts/` 是运行时挂载，不用 `--rebuild`）。

判断某个库还有没有模板残留，数短行占比（少于 2 个英文单词的行），
书面文章超过 5% 就挨个看；口语库天然高一些，`Yeah.` 是真正文。

一类反复出现的残留是**文末的参考文献区**，因为区块标题（"Works Cited"、
"Journal Reference"）行太短、被 `strip_html` 的长度过滤扔了，条目行从作者姓或
机构名起头，所有「按段落开头匹配」的规则都抓不到。现在有三条规则分别管
MLA 全名条目（`works_cited_start`）、DOI 行（`JOURNAL_REF`）和被断句切碎的
网址（`URL_FRAGMENT`）。发现新的一类时**先量化占多少段**再动手，
并且一定要拿官方词库（302 篇 / 2086 段）跑一遍误报回归。
