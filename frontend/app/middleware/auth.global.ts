/** 全局登录守卫：未登录访问业务页 -> /login；未初始化 -> /login?setup=1；已登录访问 /login -> /。
 *
 * SSR 时 useAuth.refresh 会透传当前请求 cookie，因此首屏即可决定布局/重定向，
 * 避免服务端先渲染仪表盘、客户端再跳登录页造成 hydration mismatch。
 */
export default defineNuxtRouteMiddleware(async (to) => {
  const auth = useAuth()
  const s = await auth.refresh()

  const onLogin = to.path === '/login'
  if (onLogin) {
    if (s.authenticated) return navigateTo('/')
    return
  }
  if (!s.authenticated) {
    if (s.setupRequired) {
      return navigateTo({ path: '/login', query: { setup: '1' } })
    }
    return navigateTo('/login')
  }
})
