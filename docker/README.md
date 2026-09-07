# Docker 部署（NAS / 自建服务器）

## 一分钟上手

```bash
cp docker/env.example .env        # 按需修改端口和站点地址
mkdir -p data                     # 数据目录，必须先建，见下方说明
python3 scripts/fetch-dicts.py    # 可选：下载官方全部词典（约 430MB）
docker compose up -d --build      # 构建并启动
```

打开 `http://<NAS 地址>:3000` 即可。

## 数据存在哪里

**默认（SSR 模式）：数据存在服务器磁盘上**，即项目目录下的 `./data`
（容器内 `/app/localdata`）。清浏览器缓存、换浏览器、换设备都能恢复。

浏览器里的 IndexedDB 仍然是主要读写层（保证离线可用、输入不卡），
但每次写入都会同步写一份到服务器；启动时两边比较 `updated_at`，谁新用谁。

```
浏览器操作 ──→ IndexedDB ──┐
                          ├──→ POST /api/storage/:key ──→ ./data（宿主机目录）
启动时 ←── 比较 updated_at ─┘   谁新用谁，不是无脑覆盖
```

落在磁盘上的 4 个文件，与浏览器端的 key 一一对应：

| 文件 | 内容 |
|---|---|
| `typing-word-dict` | 词典、学习进度、错词本、收藏、已掌握 |
| `typing-word-setting` | 全部设置 |
| `PracticeSaveWord` | 单词练习断点 |
| `PracticeSaveArticle` | 文章练习断点 |

都是 JSON 文本，可以直接 `cat` 查看。

### 目录权限（重要）

`./data` 是 bind mount，**Docker 不会像命名卷那样自动调整属主**，
所以宿主机目录必须能被容器内的用户写入，否则数据静默写不进去
（页面看着正常，因为浏览器那份还在，但服务器上是空的）。

两件事：

1. **先手动建目录**。目录不存在时 Docker 会用 root 建出来，容器内非 root
   用户就写不进去了：
   ```bash
   mkdir -p data
   ```

2. **让 PUID/PGID 匹配目录属主**。查看自己的 uid/gid 并填进 `.env`：
   ```bash
   id -u    # -> PUID
   id -g    # -> PGID
   ```
   群晖等 NAS 的普通用户通常不是 1000（常见 1026 起），务必确认。

启动后验证一下真的写进去了 —— 在页面上练几个词，然后：

```bash
ls -l data/       # 应该能看到上面那几个文件
```

如果是空的，看 `docker compose logs` 里有没有 `EACCES` / `permission denied`。

### 备份

直接复制目录即可：

```bash
cp -r data data-backup-$(date +%F)
# 或打包
tar czf typewords-data-$(date +%F).tar.gz data/
```

也可以用页面「设置 → 导出数据」下载 zip（含自定义文章的 mp3，`data/` 里没有音频）。

### 换个位置存

不想放在项目目录下（比如想放到 NAS 的存储卷上），改 `.env` 里的 `DATA_DIR`：

```bash
DATA_DIR=/volume1/docker/typewords-data
```

相对路径以 compose 文件所在目录为基准，绝对路径直接生效。

### 想同时用手机和电脑？

服务器存储已经支持了：两边都访问同一个地址，靠 `updated_at` 比较新旧，
不会互相覆盖。要跨网络用，就在设置页额外配 Supabase（可选，与本方案不冲突）。

## 词典：用上官方的全部词库

镜像里只内置了 CET-4 和新概念英语 1（跟着上游仓库走）。官方站点的
241 个词库 + 4 篇文章托管在 `files.typewords.cc`，数据和内置的那份完全一致
（`CET4_T.json` md5 相同），可以直接下载来用：

```bash
python3 scripts/fetch-dicts.py          # 全部，约 430MB
```

不想全下，可以按分类或体积挑：

```bash
python3 scripts/fetch-dicts.py --list                       # 先看看都有什么
python3 scripts/fetch-dicts.py --category 中国考试 国际考试   # 只要这些分类
python3 scripts/fetch-dicts.py --max-size 3                 # 只要小于 3MB 的
```

分类有：`中国考试`、`国际考试`、`青少年英语`、`代码练习`。
脚本可以反复运行，已下好的会跳过，中断了直接重跑即可。

下完之后**需要重新构建镜像**：

```bash
docker compose up -d --build
```

为什么要重新构建 —— 词典数据本身是运行时挂载的（`./dicts`，改完重启就行），
但词典**清单** `public/list/*.json` 不行：nitro 的静态资源索引在构建时生成，
里面记着每个文件的体积，运行时替换同名文件会被按旧体积截断。
所以脚本会更新 `public/list/`，那部分必须走构建。

之后再补充词典（比如先只下了四六级，后来想加雅思），如果清单没变化，
只要 `docker compose restart` 就够了。

数据放在 `./dicts`，想换位置改 `.env` 的 `DICTS_DIR`。
目录不存在或为空时自动回落到内置的两个词典，不影响启动。

## 两个构建目标

`Dockerfile` 里有两个 target，共用同一套依赖和构建层：

| target | 方式 | 数据存哪 | 适用 |
|---|---|---|---|
| `ssr`（默认） | `nuxt build` + nitro | **服务器磁盘**，持久 | NAS 自部署 |
| `static` | `nuxt generate` + nginx | 仅浏览器，清缓存即丢 | 纯静态托管（如 OSS/Pages） |

启动静态版本：

```bash
docker compose --profile static up -d --build typewords-static
```

## 常用命令

```bash
docker compose logs -f            # 看日志
docker compose up -d --build      # 拉了新代码后重新构建
docker compose down               # 停止并删除容器（./data 保留）
docker compose ps                 # 看状态和健康检查
```

## 配置说明

改 `.env` 即可，各项含义见 `docker/env.example`。几个要点：

- `ORIGIN` 是站点对外地址，用来生成 canonical / og:url 等绝对链接。
  SSR 模式下可用 `NUXT_PUBLIC_ORIGIN` 在运行时覆盖，改完重启即可，不必重新构建。
- `NUXT_APP_BASE_URL` 只在部署到子路径时需要（如 `/typewords/`）。
  存储接口用 `withAppBaseURL()` 拼地址，子路径下会自动变成 `/typewords/api/storage/:key`。
- `DATA_DIR` 是宿主机上的数据目录，默认 `./data`。
- `DICTS_DIR` 是宿主机上的词典目录，默认 `./dicts`（见上文「词典」）。
  容器内以只读方式挂载到 `/app/dicts`，对应 `DICTS_PATH`。
- `PUID` / `PGID` 是容器内的运行身份，必须能写入 `DATA_DIR`（见上文「目录权限」）。
- `STORAGE_PATH` 是**容器内**的数据目录，默认 `/app/localdata`，
  一般不用改；真要改就得同步改 compose 里的挂载点右侧。
- **不要设置 `HOST`**。`nuxt.config.ts` 用它作为 `public.host` 的默认值，
  而 nitro 在运行时用同名变量决定监听地址 —— 在 SSR 容器里设置会导致启动失败。

## 反向代理

用 Lucky / nginx / Traefik 时转发到容器 3000 端口即可，
记得把 `ORIGIN` 改成对外的真实地址（如 `https://words.example.com`）。

**关于鉴权**：`/api/storage/*` 没有做鉴权 —— 能打开页面的人就能读写数据。
局域网自用没问题；公网暴露时请务必在反向代理层加认证（Lucky 的基础认证等）。
写入 key 有白名单（`server/utils/local-storage.ts`），不能借它读写任意文件。

## 构建时的注意事项

- **内存**。Nuxt 构建 + 预渲染比较吃内存，`.env` 里的 `NODE_MAX_OLD_SPACE`
  默认给到 4096MB。如果 NAS 内存小、构建被 OOM kill，先调低这个值试试；
  仍然失败就在 PC 上构建后推镜像（见下）。
- **架构**。NAS 多为 x86_64，部分是 arm64。在本机构建再推送时要指定平台：
  ```bash
  docker build --platform linux/amd64 --target ssr -t typewords .
  ```
- **`.git` 不在 `.dockerignore` 里**，这是故意的：`nuxt.config.ts` 读取当前 commit
  作为站点版本号，应用在版本变化时会自动为本地数据创建快照备份。
  没有 `.git` 时会回退成 `unknown`，功能不受影响，只是失去版本标识。
- `public/robots.txt` 内容是 `Disallow: /`（上游用于禁止镜像站被收录）。
  自建自用无需改动；若想被搜索引擎收录，需自行修改该文件。

## 本地开发

```bash
pnpm install
pnpm dev        # http://localhost:5567，数据存 ./localdata/
```

`localdata/` 已加入 `.gitignore` 和 `.dockerignore`。
