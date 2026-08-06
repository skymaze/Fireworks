export default defineNuxtConfig({
  compatibilityDate: '2025-07-15',
  devtools: { enabled: false },
  modules: ['@nuxt/ui'],
  srcDir: 'app',
  serverDir: 'server',
  typescript: { strict: false },
  nitro: {
    // 启用 WebSocket handler（用于 /api/ws/events 反向代理到后端）。
    // 实时通道与 REST 统一走唯一入口 :3000，浏览器不再直连后端 :8000。
    experimental: { websocket: true },
  },
})
