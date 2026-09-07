/**
 * 外部词典目录（自部署时可选）。
 *
 * 官方词典数据总量约 430MB，不适合放进 git 仓库或 docker 镜像，
 * 所以放在宿主机目录里、运行时挂进容器，由这里读取。
 *
 * 为什么需要这个路由：nitro 的静态资源索引是在**构建时**扫描 public/ 生成的
 * （public-assets-data 虚拟模块），运行时才挂进来的文件不在索引里，静态处理器
 * 认不出来，会直接 404。而 public 目录的 baseURL 是 '/'，会被 publicAssetBases
 * 过滤掉，因此 isPublicAssetURL('/dicts/...') 为 false，不会被提前 404 ——
 * 请求得以继续走到这个路由。
 *
 * 与镜像内置词典的关系：public/dicts 下那两个词典在索引里，由静态中间件
 * （注册在所有路由之前）直接返回，根本走不到这里，所以本路由的 404 不会挡住它们。
 * 反过来说，也不要试图用挂载目录去覆盖同名的内置词典 —— 索引里记着旧文件的
 * size，响应会被按旧 size 截断。要换内置词典得重新构建镜像。
 */

import { createReadStream, existsSync, statSync } from 'node:fs'
import { resolve, sep } from 'node:path'

/** 宿主机词典目录在容器内的挂载点 */
const DICTS_PATH = process.env.DICTS_PATH || './dicts'

/** 只允许这些子目录，对应官方的 dicts/{language}/{type}/ 结构 */
const ALLOWED_LANGS = new Set(['en', 'ja', 'de', 'code'])
const ALLOWED_TYPES = new Set(['word', 'article'])

/** 文件名白名单。官方词典文件名只含这些字符，顺带排除了 `..` 和路径分隔符 */
const SAFE_NAME = /^[A-Za-z0-9._-]+\.json$/

/**
 * 请求不该由本路由处理时统一走这里。
 *
 * 直接 return（undefined）会让 h3 继续找下一个 handler，但 /dicts/** 后面
 * 没有别的处理器了，最终会以 204 空响应收场 —— 既不符合语义，也会把
 * 「词典不存在」伪装成成功。所以这里显式 404。
 *
 * 注意：镜像内置的词典（public/dicts 下那两个）由 nitro 的静态中间件处理，
 * 在本路由之前就已经返回了，不会走到这里，因此这个 404 不会挡住它们。
 */
function notFound(): never {
  throw createError({ statusCode: 404, statusMessage: 'Dict not found' })
}

export default defineEventHandler(async event => {
  const raw = getRouterParam(event, 'path', { decode: true })
  if (!raw) notFound()

  // 只认 {lang}/{type}/{file}.json 这一种形状
  const parts = raw.split('/')
  if (parts.length !== 3) notFound()

  const [lang, type, file] = parts
  if (!ALLOWED_LANGS.has(lang) || !ALLOWED_TYPES.has(type) || !SAFE_NAME.test(file)) notFound()

  const root = resolve(DICTS_PATH)
  const target = resolve(root, lang, type, file)
  // 双保险：白名单已排除 ..，这里再确认解析结果确实落在根目录内
  if (!target.startsWith(root + sep)) notFound()

  if (!existsSync(target)) notFound()

  const stat = statSync(target)
  if (!stat.isFile()) notFound()

  // 前端取词典时带了 version 参数，官方更新词典会改 version，所以可以长缓存
  setResponseHeader(event, 'Content-Type', 'application/json; charset=utf-8')
  setResponseHeader(event, 'Content-Length', String(stat.size))
  setResponseHeader(event, 'Cache-Control', 'public, max-age=31536000, immutable')
  setResponseHeader(event, 'Last-Modified', stat.mtime.toUTCString())

  // 最大的词典有 17MB，流式返回，不要整份读进内存
  return sendStream(event, createReadStream(target))
})
