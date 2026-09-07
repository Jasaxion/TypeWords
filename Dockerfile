# TypeWords 多阶段构建
#
# 提供两个构建目标（target）：
#
#   ssr     nuxt build + nitro node-server —— 支持把数据持久化到服务器磁盘（默认）
#   static  nuxt generate + nginx —— 纯静态，镜像最小，但数据只存在浏览器里
#
# 数据存哪里是二者的关键区别：
#   ssr    通过 /api/storage 把数据写到 STORAGE_PATH 指向的目录（挂数据卷即持久化），
#          浏览器缓存被清空、换浏览器、换设备都能恢复。
#   static 没有服务端运行时，数据只在浏览器 IndexedDB，清缓存即丢失。
#
# 单独构建（不使用 compose 时）：
#   docker build --target ssr    -t typewords .
#   docker build --target static -t typewords:static .

ARG NODE_IMAGE=node:22-bookworm-slim
ARG NGINX_IMAGE=nginx:1.29-alpine
ARG PNPM_VERSION=10.33.0

# --------------------------------------------------------------------------- #
# 1. 依赖安装：单独一层，只要 package.json / lock 没变就命中缓存
# --------------------------------------------------------------------------- #
FROM ${NODE_IMAGE} AS deps
ARG PNPM_VERSION
RUN npm install -g pnpm@${PNPM_VERSION}
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
# 原生依赖（sharp / esbuild / @parcel/watcher）的构建脚本已在
# pnpm-workspace.yaml 的 allowBuilds 中放行，无需额外参数。
RUN pnpm install --frozen-lockfile

# --------------------------------------------------------------------------- #
# 2. 构建基础层：源码 + 构建期环境变量
# --------------------------------------------------------------------------- #
FROM ${NODE_IMAGE} AS builder
ARG PNPM_VERSION
# git 用于 nuxt.config.ts 读取当前 commit（设置页展示版本号，
# 同时用于版本变化时自动创建数据快照）。没有 .git 时会回退成 unknown。
RUN npm install -g pnpm@${PNPM_VERSION} \
    && apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN git config --global --add safe.directory /app

# 站点地址等构建期变量。注意：静态构建会把 runtimeConfig.public
# 内联进产物，改这些值必须重新构建镜像。
ARG ORIGIN=http://localhost:8080
ARG NUXT_APP_BASE_URL=/
ARG API_BASE=http://localhost/
ARG VITE_PASSWORD_RSA_PUBLIC_KEY=
ARG NODE_MAX_OLD_SPACE=4096
ENV ORIGIN=${ORIGIN} \
    NUXT_APP_BASE_URL=${NUXT_APP_BASE_URL} \
    API_BASE=${API_BASE} \
    VITE_PASSWORD_RSA_PUBLIC_KEY=${VITE_PASSWORD_RSA_PUBLIC_KEY} \
    NODE_ENV=production \
    NUXT_TELEMETRY_DISABLED=1
ENV NODE_OPTIONS=--max-old-space-size=${NODE_MAX_OLD_SPACE}

# 注意：这里不要设置 HOST。nuxt.config 用它作为 public.host 的默认值，
# 而 nitro 在运行时用同名变量决定监听地址，容器里设置会导致启动失败。

FROM builder AS builder-static
RUN pnpm generate

FROM builder AS builder-ssr
RUN pnpm build

# --------------------------------------------------------------------------- #
# 3a. 运行目标：静态站点（数据只存浏览器，清缓存会丢）
# --------------------------------------------------------------------------- #
FROM ${NGINX_IMAGE} AS static
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder-static /app/.output/public /usr/share/nginx/html
EXPOSE 80
# 若把 NUXT_APP_BASE_URL 改成了子路径，请同步改这里的探测地址
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO /dev/null http://127.0.0.1/ || exit 1

# --------------------------------------------------------------------------- #
# 3b. 运行目标：Node SSR（默认，支持服务器磁盘持久化）
# --------------------------------------------------------------------------- #
FROM ${NODE_IMAGE} AS ssr
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000
ENV NUXT_TELEMETRY_DISABLED=1
# 数据目录，对应 nuxt.config.ts 的 nitro.storage.localdata
ENV STORAGE_PATH=/app/localdata
COPY --from=builder-ssr --chown=node:node /app/.output ./.output
# 预建数据目录并交给 node 用户。compose 里会把宿主机 ./data 挂到这里，
# 那种情况下属主由宿主机目录决定（bind mount 不会自动 chown），
# 所以 compose 用 user: 指定 PUID/PGID 来匹配。
RUN mkdir -p /app/localdata && chown -R node:node /app/localdata
USER node
# 这里不声明 VOLUME：声明后未挂载时 Docker 会创建匿名卷，
# 与 compose 中挂 ./data 的做法冲突，也会留下一堆无主的匿名卷。
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", ".output/server/index.mjs"]
