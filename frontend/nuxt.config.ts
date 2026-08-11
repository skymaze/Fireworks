export default defineNuxtConfig({
  compatibilityDate: '2026-08-07',
  devtools: { enabled: false },
  modules: ['@nuxt/ui', '@nuxtjs/i18n'],
  vite: {
    build: {
      // ECharts 已按组件引入，但折线、柱状和拓扑图共享的渲染内核仍会被
      // Rolldown 合并为单个 600+ KiB chunk。把稳定的第三方内核单独分组，
      // 既保留浏览器长期缓存，也避免任一产物超过 Vite 的 500 KiB 阈值。
      rolldownOptions: {
        output: {
          codeSplitting: {
            groups: [
              {
                name: 'zrender',
                test: /node_modules[\\/]zrender[\\/]/,
                priority: 30,
              },
              {
                name: 'echarts',
                test: /node_modules[\\/]echarts[\\/]/,
                priority: 20,
              },
            ],
          },
        },
      },
    },
  },
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
        'lucide:refresh-cw', 'lucide:activity',
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
