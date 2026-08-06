// 运行时 /api 代理到后端（读取 API_PROXY_TARGET 环境变量，构建时无需注入）
export default defineEventHandler(async (event) => {
  const target = process.env.API_PROXY_TARGET || 'http://localhost:8000'
  const url = getRequestURL(event)
  const dest = `${target}${url.pathname}${url.search}`
  return await proxyRequest(event, dest)
})
