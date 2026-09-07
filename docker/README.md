# Docker 部署（NAS / 自建服务器）

## 一分钟上手

```bash
cp docker/env.example .env        # 按需修改端口和站点地址
docker compose up -d --build      # 构建并启动
```

打开 `http://<NAS 地址>:3000` 即可。

## 数据存在哪里

**默认（SSR 模式）：数据存在服务器磁盘上**，即 `typewords-data` 数据卷
（容器内 `/app/localdata`）。清浏览器缓存、换浏览器、换设备都能恢复。

浏览器里的 IndexedDB 仍然是主要读写层（保证离线可用、输入不卡），
但每次写入都会同步写一份到服务器；启动时两边比较 `updated_at`，谁新用谁。

```
浏览器操作 ──→ IndexedDB ──┐
                          ├──→ POST /api/storage/:key ──→ 服务器磁盘（数据卷）
启动时 ←── 比较 updated_at ─┘   谁新用谁，不是无脑覆盖
```

落在磁盘上的 4 个文件，与浏览器端的 key 一一对应：

| 文件 | 内容 |
|---|---|
| `typing-word-dict` | 词典、学习进度、错词本、收藏、已掌握 |
| `typing-word-setting` | 全部设置 |
| `PracticeSaveWord` | 单词练习断点 |
| `PracticeSaveArticle` | 文章练习断点 |

### 备份

```bash
# 导出数据卷到 tar
docker run --rm -v typewords_typewords-data:/data -v "$PWD":/backup \
  alpine tar czf /backup/typewords-data.tar.gz -C /data .
```

也可以直接用页面「设置 → 导出数据」下载 zip（含自定义文章的 mp3，数据卷里没有音频）。

### 想同时用手机和电脑？

服务器存储已经支持了：两边都访问同一个地址，靠 `updated_at` 比较新旧，
不会互相覆盖。要跨网络用，就在设置页额外配 Supabase（可选，与本方案不冲突）。

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
docker compose down               # 停止并删除容器（数据卷保留）
docker compose down -v            # 连数据卷一起删除（会丢数据！）
docker compose ps                 # 看状态和健康检查
```

## 配置说明

改 `.env` 即可，各项含义见 `docker/env.example`。几个要点：

- `ORIGIN` 是站点对外地址，用来生成 canonical / og:url 等绝对链接。
  SSR 模式下可用 `NUXT_PUBLIC_ORIGIN` 在运行时覆盖，改完重启即可，不必重新构建。
- `NUXT_APP_BASE_URL` 只在部署到子路径时需要（如 `/typewords/`）。
  存储接口用 `withAppBaseURL()` 拼地址，子路径下会自动变成 `/typewords/api/storage/:key`。
- `STORAGE_PATH` 是服务器数据目录，默认 `/app/localdata`，
  改了要同步改 compose 里的卷挂载点。
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
