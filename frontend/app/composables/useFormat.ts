/** 语言感知的格式化：日期/时间/数字跟随应用语言（SSR 回退 zh-CN）；字节/速率/ETA 集中在此。 */

function i18nT(): (key: string) => string {
  try {
    // $t 由 @nuxtjs/i18n 注入 Nuxt app；SSR 与客户端均可用
    const nuxt = useNuxtApp() as any
    if (typeof nuxt?.$t === 'function') return nuxt.$t.bind(nuxt)
  } catch {
    /* 无 i18n 上下文时回退 */
  }
  return (key: string) => key
}

export function clientLocale(): string {
  try {
    const nuxt = useNuxtApp() as any
    return nuxt?.$i18n?.locale?.value === 'en' ? 'en' : 'zh-CN'
  } catch {
    return 'zh-CN'
  }
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

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

/** 字节数 → 人类可读字符串（单位通用，不随语言变化）。 */
export function fmtBytes(n: number | undefined | null): string {
  if (n == null || !Number.isFinite(n) || n < 0) return '—'
  if (n === 0) return '0 B'
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), BYTE_UNITS.length - 1)
  const v = n / 1024 ** i
  return `${v >= 100 ? Math.round(v) : v.toFixed(v >= 10 ? 1 : 2)} ${BYTE_UNITS[i]}`
}

/** 字节/秒 → 速率字符串（B/s ~ GB/s 自适应）。 */
export function fmtSpeed(bps: number | undefined | null): string {
  if (bps == null || !Number.isFinite(bps) || bps <= 0) return '—'
  if (bps >= 1024 ** 3) return `${(bps / 1024 ** 3).toFixed(2)} GB/s`
  if (bps >= 1024 ** 2) return `${(bps / 1024 ** 2).toFixed(1)} MB/s`
  if (bps >= 1024) return `${(bps / 1024).toFixed(1)} KB/s`
  return `${bps.toFixed(0)} B/s`
}

/** 剩余秒数 → 本地化时长（小时/分钟/秒）。 */
export function fmtEta(seconds: number | undefined | null): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return '—'
  const t = i18nT()
  const sec = Math.round(seconds)
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  const hu = t('format.hour')
  const mu = t('format.minute')
  const su = t('format.second')
  if (h) return `${h} ${hu} ${m} ${mu}`
  if (m) return `${m} ${mu} ${s} ${su}`
  return `${s} ${su}`
}
