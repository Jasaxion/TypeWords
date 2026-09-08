/**
 * 外部音频目录（自部署时可选）。
 *
 * 为什么不能放 public/sound/：那是**构建输入**，nitro 在构建时扫 public/ 生成
 * 静态资源索引（连 size 一起记下来）。自建文章库的朗读音频有一百多 MB，
 * 放进去会把镜像撑大，而且每加一篇文章都要重新构建；运行时替换同名文件还会
 * 被按旧 size 截断，静默损坏。所以走和 dicts/ 一样的路子：宿主机目录、
 * 运行时挂载、由本路由读取。挂了这个路由之后，加音频不需要再构建镜像。
 *
 * 必须支持 Range 请求。前端播放某一句是 `audio.currentTime = start`，
 * 也就是往一个长音频里跳。服务端不支持 Range 的话，浏览器只能把整个文件
 * 下完才能跳 —— 最大的一篇是 68 分钟、十几 MB，每次跳句都重下一遍。
 * 另外 MP3 必须是**恒定码率**（CBR）才能按字节偏移准确定位；VBR 会跳错位置。
 */

import { createReadStream, existsSync, statSync } from 'node:fs'
import { resolve, sep } from 'node:path'

/** 宿主机音频目录在容器内的挂载点 */
const AUDIO_PATH = process.env.AUDIO_PATH || './audio'

/** 文件名白名单，顺带排除了 `..` 和路径分隔符 */
const SAFE_SEG = /^[A-Za-z0-9._-]+$/
const SAFE_NAME = /^[A-Za-z0-9._-]+\.(mp3|m4a|ogg|wav)$/

const MIME: Record<string, string> = {
  mp3: 'audio/mpeg',
  m4a: 'audio/mp4',
  // 带上 codecs=opus：合成脚本默认产出的就是 ogg 容器里的 opus，
  // 裸 audio/ogg 会让部分浏览器按 vorbis 去猜、canPlayType 返回空串。
  ogg: 'audio/ogg; codecs=opus',
  wav: 'audio/wav',
}

function notFound(): never {
  throw createError({ statusCode: 404, statusMessage: 'Audio not found' })
}

/**
 * 解析 Range 头。只处理单区间的 `bytes=a-b` / `bytes=a-` / `bytes=-n`，
 * 多区间（multipart/byteranges）用不到，直接当没有 Range 处理。
 */
function parseRange(header: string | undefined, size: number) {
  if (!header) return null
  const m = /^bytes=(\d*)-(\d*)$/.exec(header.trim())
  if (!m) return null
  const [, rawStart, rawEnd] = m
  if (!rawStart && !rawEnd) return null

  let start: number
  let end: number
  if (!rawStart) {
    // bytes=-n：最后 n 个字节
    const n = Number(rawEnd)
    if (!n) return null
    start = Math.max(0, size - n)
    end = size - 1
  } else {
    start = Number(rawStart)
    end = rawEnd ? Number(rawEnd) : size - 1
  }

  if (!Number.isFinite(start) || !Number.isFinite(end)) return null
  if (start > end || start >= size) return { unsatisfiable: true as const }
  return { start, end: Math.min(end, size - 1) }
}

export default defineEventHandler(async event => {
  const raw = getRouterParam(event, 'path', { decode: true })
  if (!raw) notFound()

  // 只认 {dict}/{file}.mp3 这一种形状，和 audio/<enName>/<n>.mp3 对应
  const parts = raw.split('/')
  if (parts.length !== 2) notFound()

  const [dict, file] = parts
  if (!SAFE_SEG.test(dict) || !SAFE_NAME.test(file)) notFound()

  const root = resolve(AUDIO_PATH)
  const target = resolve(root, dict, file)
  // 双保险：白名单已排除 ..，这里再确认解析结果确实落在根目录内
  if (!target.startsWith(root + sep)) notFound()

  if (!existsSync(target)) notFound()
  const stat = statSync(target)
  if (!stat.isFile()) notFound()

  const ext = file.slice(file.lastIndexOf('.') + 1).toLowerCase()
  setResponseHeader(event, 'Content-Type', MIME[ext] ?? 'application/octet-stream')
  // 告诉浏览器可以跳着取，否则它会把整个文件下完才肯 seek
  setResponseHeader(event, 'Accept-Ranges', 'bytes')
  // 音频内容不变，文件名里带库名和序号，可以长缓存
  setResponseHeader(event, 'Cache-Control', 'public, max-age=31536000, immutable')
  setResponseHeader(event, 'Last-Modified', stat.mtime.toUTCString())

  const range = parseRange(getRequestHeader(event, 'range'), stat.size)

  if (range && 'unsatisfiable' in range) {
    setResponseHeader(event, 'Content-Range', `bytes */${stat.size}`)
    throw createError({ statusCode: 416, statusMessage: 'Range Not Satisfiable' })
  }

  if (range) {
    const { start, end } = range
    setResponseStatus(event, 206)
    setResponseHeader(event, 'Content-Range', `bytes ${start}-${end}/${stat.size}`)
    setResponseHeader(event, 'Content-Length', String(end - start + 1))
    return sendStream(event, createReadStream(target, { start, end }))
  }

  setResponseHeader(event, 'Content-Length', String(stat.size))
  return sendStream(event, createReadStream(target))
})
