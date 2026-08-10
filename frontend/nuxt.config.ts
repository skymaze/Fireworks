export default defineNuxtConfig({
  compatibilityDate: '2026-08-07',
  devtools: { enabled: false },
  modules: ['@nuxt/ui', '@nuxtjs/i18n'],
  // 导航/用户菜单图标定义在 TS computed 中，自动扫描只覆盖模板字面量；
  // 显式打进客户端 bundle，保证 SSR/离线环境无需回退 Iconify API。
  icon: {
    clientBundle: {
      icons: [
        'lucide:house', 'lucide:server', 'lucide:boxes', 'lucide:cpu',
        'lucide:image', 'lucide:list-checks', 'lucide:store', 'lucide:rocket',
        'lucide:circle-user', 'lucide:chevrons-up-down', 'lucide:key',
        'lucide:languages', 'lucide:palette', 'lucide:power', 'lucide:monitor',
        'lucide:sun', 'lucide:moon', 'lucide:user', 'lucide:lock',
      ],
    },
  },
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
