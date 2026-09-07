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
cd /Users/zhanshaoxiong.3/Desktop/tmp-app/TypeWords-fork/TypeWords
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
真正的清单在 NAS 上。详见 `~/.claude/skills/typewords-article/SKILL.md`。

篇数/句数会随过滤规则变化，**不要把某次的数字当成基准** ——
清单里的 `length` 一律由 `scripts/deploy-article-dicts.py` 从生成的 json 现读。
建完先跑 `python3 scripts/check-article-dict.py /tmp/twout/en/article/*.json`，
非 0 就别传。

## 单词词表

`wordlists/` 不在这个仓库里 —— 词表和 ECDICT 都只在 NAS 上
（`/path/to/TypeWords/wordlists/`），因为本机的 `dicts/` 是空的，
建词库需要那 244 个官方词库当数据源。重建：

```bash
ssh -p 40022 root@192.168.1.10 'cd /path/to/TypeWords && \
  python3 scripts/build-word-dict.py --words wordlists/ai-ml.txt \
    --name "AI 与机器学习" --en-name ai-ml --category 专业领域 --tags 人工智能 \
    --ecdict ../build-cache/ecdict.csv'
```

`--ecdict` 必须显式给：ECDICT 在**仓库的上一级**，不是脚本默认的 `<repo>/build-cache/`。
建完 `chown 1000:1001` + `chmod go+r`，否则容器读不到。

## 改了过滤规则怎么办

别重新翻译（半小时以上、限流、不能并发）。只影响段落取舍的规则改动
（`BOILERPLATE_RE` / `REFERENCE_RE` / `is_token_spam` / `is_citation_block` / `fix_c1`）用：

```bash
python3 scripts/restrip-article-dict.py /tmp/twout/en/article/*.json
python3 scripts/check-article-dict.py   /tmp/twout/en/article/*.json
```

它从 `text` 和 `textTranslate` 删掉**同一批下标**。影响切句的改动
（`split_sentences`、`--max-sentence-chars`）它做不到，得按上面的命令重建。

清理完**要重新部署**：`deploy-article-dicts.py` 覆盖 NAS 上那份，
否则本地干净、线上还是脏的（`dicts/` 是运行时挂载，不用 `--rebuild`）。

判断某个库还有没有模板残留，数短行占比（少于 2 个英文单词的行），
书面文章超过 5% 就挨个看；口语库天然高一些，`Yeah.` 是真正文。
