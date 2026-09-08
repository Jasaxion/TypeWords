# 自建词库/文章库：本地构建，远端只放数据

这些脚本**只在本地跑**，部署机上不留脚本、不跑程序 —— 部署机只接收生成好的
json。`deploy-article-dicts.py` 是唯一会碰远端的脚本，它只做三件事：
传文件、改属主、（可选）触发重新构建镜像。

## 配置

部署目标从环境变量读，不写死在仓库里：

```bash
export TW_SSH="-p 40022 root@192.168.1.10"   # ssh 参数，或配好 ~/.ssh/config 后就一个主机名
export TW_REMOTE=/path/to/TypeWords          # 远端仓库路径
export TW_OWNER=1000:1001                    # 容器的 uid:gid，查法：docker exec <容器> id
export TW_BASE=http://127.0.0.1:3000         # check-live-dicts.py 用
export TW_TTS_KEY=sk-...                     # synth-article-audio.py / add-article.py 用
export TW_TTS_URL=https://<网关>/api/v1/services/aigc/multimodal-generation/generation
export TW_LLM_URL=https://<网关>/compatible-mode/v1/chat/completions   # add-article.py 翻译用
export TW_LLM_KEY=sk-...                     # 翻译的 key，不给就用 TW_TTS_KEY
```

`TW_OWNER` 必须和容器一致。ssh 进去是 root，写出来的文件是 `root:root 0600`，
容器读不到 —— 页面上表现为「词库点开是空的」，不报错。

放在 `.env.local`（已被 `.gitignore` 忽略）里 `source` 一下最省事。

## 本地需要先准备的两个数据源

建**单词**词库要查释义/音标/例句，两个数据源仓库里都不带：

```bash
python3 scripts/fetch-dicts.py --dest dicts      # 官方词库，约 430MB，可续传
# ECDICT: 一个 63MB 的 csv，自己下载，用 --ecdict 给路径
```

建**文章**词库不需要这两个，只要能联网抓页面。

## 为什么不在容器里跑

容器镜像里**没有 python**（只有 node），所以脚本不能 `docker exec` 进去跑。
脚本本身是纯标准库（只有 `--pdf` 需要 `pypdf`），本地有 python3 就行。

真要容器化的话，需要另做一个装了 python 的构建镜像，把 `dicts/` 和词表挂进去 ——
目前没做，因为在本地跑一样能出结果，而且本地还能直接看生成的 json。

## 流程

### 加**一篇**进已有的库：一条命令（add-article.py）

日常最常用的就是这个。抽正文 → 翻译 → 合成朗读 → 追加 → 自查，一条命令走完：

```bash
python3 scripts/add-article.py --url https://example.com/post --into my-reading
```

来源可以是 `--url` / `--rss --limit 3` / `--pdf` / `--txt` / `--ted` / `--json`
（`--txt-dir` 也在）。默认音色 Chelsie、opus 编码、用 `qwen-plus` 翻译
（`--translate-free` 换回 Google 免费接口，`--no-translate` 不翻）。
先看规模不动手就加 `--dry-run`。

**为什么是「追加」而不是重建**：`lastLearnIndex` 是**位置下标**，`audioSrc`
也按数组下标命名。重建整库会重排顺序 —— 页面不报错，只是「接着学」指到别的
文章、每篇念的是别人的音频。所以这个脚本只往末尾加，已有条目一个都不动。
音频也只给新加的几篇合成，上传是增量的。

**改之前先把部署机上那份拉下来**。`--into` 读的是本地 `dicts/en/article/<name>.json`，
在旧副本上追加会把线上后来加的文章覆盖掉。脚本会告诉你当前是几篇，但没法
替你判断哪份新。

往已有库追加**不用** `--rebuild`：清单里的 `length` 会过期，但前端在
`normalizeStoredDict()` 里按 `articles.length` 重算。只有新建库
（`--create --name "..."`）才要加清单条目 + rebuild。

### 建一个**新库**（一次转一批，make-article-dict.py）

```bash
# 1. 建（文章）
python3 scripts/make-article-dict.py --name "xxx" --en-name xxx \
    --url $(tr '\n' ' ' < sources/xxx.txt) --dest /tmp/twout --no-manifest

# 2. 自查，非 0 就别传
python3 scripts/check-article-dict.py /tmp/twout/en/article/*.json

# 3. 传
python3 scripts/deploy-article-dicts.py --file /tmp/twout/en/article/xxx.json \
    --name "xxx" --category 专业阅读 --tags xxx

# 4. 清单变了就重新构建镜像（只加数据不用）
python3 scripts/deploy-article-dicts.py --rebuild

# 5. 验证线上
python3 scripts/check-live-dicts.py
```

第 4 步为什么必须：`dicts/` 是运行时挂载，换文件立刻生效；
`public/list/*.json` 是**构建输入**（nitro 构建时扫 `public/` 生成静态资源索引），
运行时替换会被按**旧 size 截断**，静默损坏。

第 5 步在**部署机上**跑最快 —— 几百个词库要逐个下载，走局域网可能几分钟都跑不完，
走 `127.0.0.1` 一分钟出结果。但那是一次 `curl`，不是在部署机上跑构建。

## 朗读音频（可选，但抓来的文章不做就是机器音）

官方的新概念英语自带真人配音 mp3 + 逐句时间轴，所以听着正常。自己抓的文章没有
这份数据，前端会把**整句**丢进有道的**单词**接口，那接口不接整句、返回 120 字节
空响应，于是回退到浏览器 `speechSynthesis` —— 就是那个机械嗓子。

`synth-article-audio.py` 用云端 TTS 逐句合成、每篇拼一个 mp3，并把
`audioSrc` + `lrcPosition` 写回词库 json：

```bash
export TW_TTS_KEY=sk-...
# 私有网关再给这两个（默认走 dashscope 公网地址）
export TW_TTS_URL=https://<网关>/api/v1/services/aigc/multimodal-generation/generation
export TW_TTS_MODEL=qwen3-tts-flash

# 0. 换网关/换模型后先确认音色表还对
python3 scripts/synth-article-audio.py --dict <json> --list-voices

# 1. 先 dry-run 看规模（句数/时长/体积/耗时）
python3 scripts/synth-article-audio.py --dict /tmp/twout/en/article/xxx.json --dry-run

# 2. 合成（按句缓存，中断后重跑只补缺的）
python3 scripts/synth-article-audio.py --dict /tmp/twout/en/article/xxx.json \
    --voice Ethan --out /tmp/twaudio

# 3. 自查：时间轴条数/单调性/末尾是否超出音频长度，以及线上 Range
python3 scripts/check-article-audio.py /tmp/twout/en/article/xxx.json --audio /tmp/twaudio

# 4. 传音频（运行时挂载，不用 --rebuild）+ 传改过的 json
python3 scripts/deploy-article-dicts.py --audio-dir /tmp/twaudio/xxx
python3 scripts/deploy-article-dicts.py --file /tmp/twout/en/article/xxx.json --name "..."
```

规模参考：三个自建文章库合计 6440 句 / 65 万字符 ≈ 12 小时音频，
默认 opus 24k 下约 **124MB**（同音质的 mp3 要 250MB），2 并发约 1.5 小时。
想先试效果就挑最小的库（`life-science-read`，649 句，约 10 分钟、14MB）。

### 为什么默认 opus 而不是 mp3

同一段 26 秒朗读，和源 wav 比梅尔谱距离（越小越接近源）：

| 编码 | 体积（12h 折算） | 梅尔距离 |
|---|---|---|
| opus 24k | 124 MB | **3.20** |
| mp3 48k | 247 MB | 3.20 |
| opus 16k | 82 MB | 3.58 |
| mp3 32k | 165 MB | 7.15 |
| mp3 24k | 124 MB | 18.85 |

即**同音质省一半体积，同体积音质好一倍**。mp3 在低码率崩掉是因为 LAME 会硬性
低通（32k 时切在 ~11kHz），opus 本来就是为语音设计的。跳句精度也验过，
opus 各码率 envelope 相关 0.97~0.999，和 mp3 一个水平，不影响
`audio.currentTime = start` 定位。

兼容性是唯一代价：Safari 要 **17.5+**（macOS Sonoma / iOS 17.5+）才支持
ogg-opus，Chrome/Firefox/Edge 一直支持。要照顾更老的 Safari 就 `--codec mp3`。

合成用的采样率也从 24k 降到 16k（朗读够用，接口的 `sample_rate` 参数是真生效的）。

### 这里的坑比别处多

- **硬换行的 txt 会让句数凭空变多。** `text` 是用 `\n` 连句子的，所以句子内部
  只要还留着换行，前端 `split('\n')` 就把一句读成两行 —— 译文和 `lrcPosition`
  全部错位，页面**不报错**。`strip_html`/`clean_pdf_text` 自己会压空白，但
  `--json` / `--txt` / `--txt-dir` 这些自备材料的路径不会。现在统一在
  `build_text` 里压（实测一篇 10 句的硬换行 txt 会被前端读成 14 句）。
- **音频不能放 `public/sound/`。** 那是构建输入，进了镜像每加一篇文章都要重建，
  而且运行时替换同名文件会被按**旧 size 截断**。走运行时挂载路由
  `server/routes/audio/[...path].get.ts`（`AUDIO_PATH`，compose 里挂
  `AUDIO_DIR`），和 `dicts/` 一个套路。
- **那个路由必须支持 HTTP Range。** 前端跳句是 `audio.currentTime = start`：
  没有 Range，每次跳句都要下整个文件（最长的一篇 68 分钟）。
- **mp3 必须是 CBR**（用 `-b:a` 不是 `-q:a`），否则按字节偏移估时间会跳错位置。
  opus 不受这个限制。
- **接口只认 `sample_rate`，不认 `format`。** 传 `format=mp3`/`audio_format=mp3`
  都被静默忽略、照样返回 WAV。压缩只能在本地转。
- **`lrcPosition` 的顺序必须和前端切句顺序完全一致** —— `text` 按 `\n\n` 分段、
  段内按 `\n` 分句、展平后逐个对应。错位了页面**不报错**，只是每句念的是别的
  句子。所以脚本里的切分逻辑是照抄前端的，别"优化"。
- **改了采样率/编码要清缓存吗？** 不用，缓存 key 里带了采样率，换了自动重合成。
- **模型名写错的报错会误导你。** 走 websocket（`dashscope.audio.tts_v2`）时，
  如果模型名不在网关的模型列表里，请求会落到 cosyvoice 引擎，然后**所有**音色名
  都报 `Engine error [411]` —— 看着像音色名不对，其实是模型名不对。
  OpenAI 兼容的 `/audio/speech` 在私有网关上直接 404。能用的是 DashScope 原生
  HTTP 的 `multimodal-generation` 路径。
- **网关限流很紧**，实测 4 路并发一半是 `Throttling.RateQuota`，2 路稳定。

### 翻译：默认 LLM，免费接口留着兜底

`add-article.py` 默认用网关上的 `qwen-plus` 逐行翻译，比 Google 免费接口
（`make-article-dict.py --translate` 用的那个）明显通顺，也认得
`1.5% rule` 这种。免费接口还留着，加 `--translate-free` 就切回去 ——
不需要 key，但限流紧、质量一般。

保命的是**行数必须守住**：译文按下标和原文对应，多一行少一行都会让那一段
之后全部错位（页面不报错，只是中英对不上）。所以：

- 提示词要求逐行输出、每行带原编号，回来的行**按编号还原**而不是按顺序 ——
  能容忍模型加个"以下是翻译："、把 `1.` 写成 `1、`、甚至顺序打乱。
- 编号对不上就**整批重试**，重试还不行就整批留空占位，绝不错位。
- 最后 `structure_ok()` 再核一遍段数和每段行数，不符就**丢掉译文保住原文**。

实测 95 行跨 3 个批次（每批 40 行）零漂移、零空行，约 56 秒。

## 踩过的坑

- **不要用 scp。** 远端 shell 只要打了横幅（tmux 之类）就会把 scp 协议搞乱，
  表现是 exit 0 但内容是旧的。脚本用 tar 管道。
- **不要从 NAS 的 Docker 面板点启动。** 面板进程 `dockermgr` 不排空构建输出的
  管道，会让 compose 卡在 `anon_pipe_write` 死等（曾卡 56 分钟）。
  始终从 SSH 跑并重定向输出，脚本已经这么做了。
- **`public/dicts/` 会遮挡挂载数据。** 上游带的两个样例词典
  （`public/dicts/en/article/NCE_1.json` 只有 5 篇）会被烧进构建产物，而静态
  中间件排在自定义 `/dicts/` 路由之前 —— 同名的挂载文件永远取不到。
  新增词典一律只放挂载目录 `dicts/`。
- **重建已在学的词库前先看进度。** `lastLearnIndex` 是位置下标，词序一变
  「接着学」就指向别的词，页面不报错。查远端 `data/typing-word-dict` 里对应
  `enName` 是不是 0；非 0 就换个新 `enName` 另建。

其余的抽取质量、断句约束、翻译限流等，见
`.claude/skills/typewords-article/SKILL.md`。
