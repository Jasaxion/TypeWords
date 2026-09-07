/**
 * 外部词典目录（自部署时可选）。
 *
 * 官方词典数据总量约 430MB，不适合放进 git 仓库或 docker 镜像，
 * 所以放在宿主机目录里、运行时挂进容器，由这里读取。
 *
 * 为什么需要这个路由：nitro 的静态资源索引是在**构建时**扫描 public/ 生成的
 * （public-assets-data 虚拟模块），运行时才挂进来的文件不在索引里，静态处理器
 * 认不出来。好在静态处理器是 middleware，取不到资源就返回 undefined 放行；
 * 而且 public 目录的 baseURL 是 '/'，会被 publicAssetBases 过滤掉，
 * 因此 isPublicAssetURL('/dicts/...') 为 false，不会被提前 404 —— 于是
 * 请求会继续走到这个路由。
 *
 * 没挂载词典目录时，这里一律返回 undefined 放行，回落到 public/ 内置的那两个
 * 词典，行为与没有这个功能时完全一致。
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

export default defineEventHandler(async event => {
  const raw = getRouterParam(event, 'path', { decode: true })
  if (!raw) return

  // 只认 {lang}/{type}/{file}.json 这一种形状
  const parts = raw.split('/')
  if (parts.length !== 3) return

  const [lang, type, file] = parts
  if (!ALLOWED_LANGS.has(lang) || !ALLOWED_TYPES.has(type) || !SAFE_NAME.test(file)) return

  const root = resolve(DICTS_PATH)
  const target = resolve(root, lang, type, file)
  // 双保险：白名单已排除 ..，这里再确认解析结果确实落在根目录内
  if (!target.startsWith(root + sep)) return

  if (!existsSync(target)) return // 放行，让内置词典仍然可用

  const stat = statSync(target)
  if (!stat.isFile()) return

  // 前端取词典时带了 version 参数，官方更新词典会改 version，所以可以长缓存
  setResponseHeader(event, 'Content-Type', 'application/json; charset=utf-8')
  setResponseHeader(event, 'Content-Length', String(stat.size))
  setResponseHeader(event, 'Cache-Control', 'public, max-age=31536000, immutable')
  setResponseHeader(event, 'Last-Modified', stat.mtime.toUTCString())

  // 最大的词典有 17MB，流式返回，不要整份读进内存
  return sendStream(event, createReadStream(target))
})
