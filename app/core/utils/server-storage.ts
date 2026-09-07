import { withAppBaseURL } from './base-url'
import { SyncDataType } from '../types/enum'

/**
 * 服务器文件系统存储（自部署时可用）。
 *
 * 与 Supabase 同级的一个同步后端：数据存在部署机器（如 NAS）的磁盘上，
 * 浏览器只是缓存。清浏览器缓存、换浏览器、换设备都能恢复。
 *
 * 只在 SSR 模式下存在（静态部署没有服务端运行时），因此所有调用都要能
 * 静默失败 —— 探测不到 /api/storage 时整体退化成纯浏览器存储，与原行为一致。
 */

/** 与 SyncDataType 对应的服务端存储 key，和浏览器端 IndexedDB 的 key 保持一致 */
const STORAGE_KEY_BY_TYPE: Record<SyncDataType, string> = {
  [SyncDataType.dict]: 'typing-word-dict',
  [SyncDataType.setting]: 'typing-word-setting',
  [SyncDataType.practice_word]: 'PracticeSaveWord',
  [SyncDataType.practice_article]: 'PracticeSaveArticle',
}

export type ServerStoragePayload = {
  val: unknown
  version?: number
  updated_at?: string
}

/**
 * 是否可用。null = 尚未探测。
 * 探测一次即缓存，避免每次保存都多打一个请求。
 */
let available: boolean | null = null
let probing: Promise<boolean> | null = null

function apiUrl(key: string): string {
  return withAppBaseURL(`/api/storage/${encodeURIComponent(key)}`)
}

function storageKey(type: SyncDataType): string {
  return STORAGE_KEY_BY_TYPE[type]
}

/**
 * 探测服务端存储是否可用。
 *
 * 静态部署下 /api/storage 会返回 SPA 兜底的 HTML 而非 JSON，
 * 所以不能只看 res.ok，必须确认响应体真的是我们的接口格式。
 */
export function probe(): Promise<boolean> {
  if (available !== null) return Promise.resolve(available)
  if (probing) return probing

  probing = (async () => {
    if (typeof window === 'undefined') return false
    try {
      const res = await fetch(apiUrl(STORAGE_KEY_BY_TYPE[SyncDataType.dict]), {
        headers: { Accept: 'application/json' },
      })
      if (!res.ok) return false
      const contentType = res.headers.get('content-type') ?? ''
      if (!contentType.includes('application/json')) return false
      const body = await res.json()
      return body?.success === true
    } catch {
      return false
    } finally {
      probing = null
    }
  })()

  probing.then(result => {
    available = result
    if (result) console.log('[server-storage] 服务器文件存储可用，数据将持久化到部署机器磁盘')
  })

  return probing
}

/** 同步读取探测结果，不触发探测。用于不该等待的路径。 */
export function isAvailable(): boolean {
  return available === true
}

/** 读取一条数据；不可用或无数据时返回 null */
export async function getItem(type: SyncDataType): Promise<ServerStoragePayload | null> {
  if (!(await probe())) return null
  try {
    const res = await fetch(apiUrl(storageKey(type)), { headers: { Accept: 'application/json' } })
    if (!res.ok) return null
    const body = await res.json()
    const raw = body?.data
    if (!raw || typeof raw !== 'string') return null
    return JSON.parse(raw) as ServerStoragePayload
  } catch (e) {
    console.warn('[server-storage] 读取失败', type, e)
    return null
  }
}

/** 只读 meta（updated_at / version），用于比较新旧而不搬运整份数据 */
export async function getMeta(type: SyncDataType): Promise<{ updated_at?: string; version?: number } | null> {
  const payload = await getItem(type)
  if (!payload) return null
  return { updated_at: payload.updated_at, version: payload.version }
}

/**
 * 写入一条数据。
 *
 * 失败只告警不抛错：服务端存储是「额外的」持久化层，它挂掉不该让
 * 用户的练习流程中断 —— 浏览器本地那一份已经写成功了。
 */
export async function setItem(type: SyncDataType, payload: ServerStoragePayload): Promise<boolean> {
  if (!(await probe())) return false
  try {
    const res = await fetch(apiUrl(storageKey(type)), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // val 为 null 表示清空，服务端会删除该文件
      body: JSON.stringify({ data: payload.val === null ? null : JSON.stringify(payload) }),
    })
    return res.ok
  } catch (e) {
    console.warn('[server-storage] 写入失败', type, e)
    return false
  }
}

export const ServerStorage = {
  probe,
  isAvailable,
  getItem,
  getMeta,
  setItem,
}
