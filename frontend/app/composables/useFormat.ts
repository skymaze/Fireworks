/** 语言感知的格式化：日期/时间/数字跟随应用语言（SSR 回退 zh-CN）；字节/速率/ETA 集中在此。 */

/** 取 i18n 翻译函数（global scope，含 locale 文件 messages）。
 *
 * 不能用 nuxt.$t：它映射到 root scope（legacy 全局注入），locale 文件
 * messages 注册在 global scope（useI18n() 返回的 composer）——root scope
 * 下翻译不到任何 key，只会原样返回 key 字符串。
 * 非 setup 上下文（事件处理等）调用会抛错，调用方回退兜底。
 */
export function i18nT(): (key: string, params?: Record<string, unknown>) => string {
  try {
    const { t } = useI18n()
    return t as (key: string, params?: Record<string, unknown>) => string
  } catch {
    return (key: string) => key
  }
}

export function clientLocale(): string {
  try {
    return useI18n().locale.value === 'en' ? 'en' : 'zh-CN'
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

/** 剩余秒数 → 本地化时长（小时/分钟/秒）。
 *
 * 用 Intl.DurationFormat（TC39 标准，浏览器/Node 内置，语言数据随 ICU 自带），
 * 不依赖 i18n 单位词 key；不支持的环境降级为手动拼接。
 */
export function fmtEta(seconds: number | undefined | null): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return '—'
  const locale = clientLocale()
  const sec = Math.round(seconds)
  const dur = {
    hours: Math.floor(sec / 3600),
    minutes: Math.floor((sec % 3600) / 60),
    seconds: sec % 60,
  }
  // 只显示最高两个有效单位（6 小时 19 分钟 / 19 分钟 5 秒 / 5 秒）
  const part = dur.hours
    ? { hours: dur.hours, minutes: dur.minutes }
    : dur.minutes
      ? { minutes: dur.minutes, seconds: dur.seconds }
      : { seconds: dur.seconds }
  const DF = (Intl as unknown as { DurationFormat?: new (l: string, o: object) => { format(d: object): string } }).DurationFormat
  if (typeof DF === 'function') {
    try {
      return new DF(locale, { style: 'short' }).format(part)
    } catch {
      /* 降级到手动拼接 */
    }
  }
  const u = locale === 'en' ? { h: 'h', m: 'min', s: 's' } : { h: '小时', m: '分钟', s: '秒' }
  if (dur.hours) return `${dur.hours} ${u.h} ${dur.minutes} ${u.m}`
  if (dur.minutes) return `${dur.minutes} ${u.m} ${dur.seconds} ${u.s}`
  return `${dur.seconds} ${u.s}`
}
