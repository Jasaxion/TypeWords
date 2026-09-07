import { getLocalDataStorage, isValidStorageKey } from '../../utils/local-storage'

/**
 * POST /api/storage/:key
 *
 * 写入一条数据到服务器文件系统。body 为 { data: string | null }：
 * data 为 null 时删除该 key（用于清空练习进度）。
 */
export default defineEventHandler(async event => {
  const key = getRouterParam(event, 'key')

  if (!isValidStorageKey(key)) {
    throw createError({ statusCode: 400, statusMessage: 'Invalid storage key' })
  }

  const body = await readBody<{ data?: string | null }>(event)
  const value = body?.data ?? null

  const storage = getLocalDataStorage()

  if (value === null) {
    await storage.removeItem(key)
    return { success: true }
  }

  if (typeof value !== 'string') {
    throw createError({ statusCode: 400, statusMessage: 'data must be a JSON string or null' })
  }

  await storage.setItem(key, value)
  return { success: true }
})
