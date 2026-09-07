/**
 * 服务器端本地数据存储（Nitro fs 驱动）。
 *
 * 数据落在 nuxt.config.ts 里 nitro.storage.localdata 指定的目录，
 * 默认 ./localdata，Docker 中通过 STORAGE_PATH 指向挂载的数据卷。
 */

/** 允许读写的 key 白名单。与浏览器端的存储 key 一一对应。 */
const ALLOWED_KEYS = new Set([
  'typing-word-dict', // SAVE_DICT_KEY.key      词典/进度/错词本/收藏
  'typing-word-setting', // SAVE_SETTING_KEY.key   全部设置
  'PracticeSaveWord', // PRACTICE_WORD_CACHE.key    单词练习断点
  'PracticeSaveArticle', // PRACTICE_ARTICLE_CACHE.key 文章练习断点
])

/**
 * 校验 key。
 *
 * 白名单是必需的，不只是为了规范：fs 驱动会把 key 映射成文件路径，
 * 放开任意 key 等于把文件系统读写暴露出去（`../` 穿越）。
 */
export function isValidStorageKey(key: string | undefined): key is string {
  return typeof key === 'string' && ALLOWED_KEYS.has(key)
}

export function getLocalDataStorage() {
  return useStorage('localdata')
}

export function getAllowedStorageKeys(): string[] {
  return [...ALLOWED_KEYS]
}
