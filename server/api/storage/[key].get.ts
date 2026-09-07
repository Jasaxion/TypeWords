import { getLocalDataStorage, isValidStorageKey } from '../../utils/local-storage'

/**
 * GET /api/storage/:key
 *
 * 从服务器文件系统读取一条数据。返回结构与浏览器 IndexedDB 中的存储格式一致，
 * 即 { val, version, updated_at } 的 JSON 字符串，交由前端同步层比较新旧。
 */
export default defineEventHandler(async event => {
  const key = getRouterParam(event, 'key')

  if (!isValidStorageKey(key)) {
    throw createError({ statusCode: 400, statusMessage: 'Invalid storage key' })
  }

  const storage = getLocalDataStorage()
  const data = await storage.getItem<string>(key)

  return { success: true, data: data ?? null }
})
