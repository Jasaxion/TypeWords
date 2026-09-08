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
