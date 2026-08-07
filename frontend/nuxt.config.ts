export default defineNuxtConfig({
  compatibilityDate: '2026-08-07',
  devtools: { enabled: false },
  modules: ['@nuxt/ui', '@nuxtjs/i18n'],
  srcDir: 'app',
  serverDir: 'server',
  typescript: { strict: false },
  i18n: {
    locales: [
      { code: 'zh', name: '中文', file: 'zh.ts', language: 'zh-CN' },
      { code: 'en', name: 'English', file: 'en.ts', language: 'en' },
    ],
    defaultLocale: 'zh',
    strategy: 'no_prefix',
    langDir: 'locales',
    // 自动检测浏览器语言 + cookie(fw_locale) 记住用户选择；未知/未匹配回退中文
    detectBrowserLanguage: {
      useCookie: true,
      cookieKey: 'fw_locale',
      fallbackLocale: 'zh',
      redirectOn: 'root',
    },
  },
  nitro: {
    // 启用 WebSocket handler（用于 /api/ws/events 反向代理到后端）。
    // 实时通道与 REST 统一走唯一入口 :3000，浏览器不再直连后端 :8000。
    experimental: { websocket: true },
  },
})
