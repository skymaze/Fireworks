/** 全局登录守卫：未登录访问业务页 -> /login；未初始化 -> /login?setup=1；已登录访问 /login -> /。
 *
 * 只在客户端评估（会话 cookie 由浏览器携带，SSR 无浏览器会话上下文）。
 */
export default defineNuxtRouteMiddleware(async (to) => {
  if (import.meta.server) return

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
