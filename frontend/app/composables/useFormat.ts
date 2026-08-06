/** 客户端语言感知的格式化（navigator.language，SSR 回退 zh-CN）。 */

export function clientLocale(): string {
  if (typeof navigator !== 'undefined' && navigator.language) return navigator.language
  return 'zh-CN'
}

/** 日期时间（ISO/时间戳/Date）→ 客户端语言本地化字符串。 */
export function fmtDateTime(v: string | number | Date | null | undefined): string {
  if (!v) return '—'
  return new Date(v).toLocaleString(clientLocale())
}

/** 秒级时间戳 → 客户端语言本地化时间（图表横轴）。 */
export function fmtTime(tsSeconds: number): string {
  return new Date(tsSeconds * 1000).toLocaleTimeString(clientLocale())
}

/** 数字 → 客户端语言本地化（千分位等）。 */
export function fmtNumber(v: number): string {
  return v.toLocaleString(clientLocale())
}
