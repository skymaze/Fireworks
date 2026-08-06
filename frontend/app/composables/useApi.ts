/** API 封装：所有请求走同源 /api，由 Nitro server 路由代理到后端。
 *
 * 全局 401 拦截：业务端点的会话失效（登出/过期/被吊销）统一处理——
 * 置登录态为未登录并跳转 /login；认证端点自身（/api/auth/*）不触发，
 * 避免登录页轮询 status 时造成跳转循环。
 */

export function useApi() {
  /** 统一请求入口：附带 401 处理 */
  const request = async (path: string, options: Record<string, unknown> = {}) => {
    try {
      return await $fetch(`/api${path}`, options)
    } catch (e: any) {
      if (typeof window !== 'undefined' && e?.response?.status === 401 && !path.startsWith('/auth')) {
        const auth = useAuth()
        auth.invalidate()
        if (window.location.pathname !== '/login') {
          await navigateTo('/login')
        }
      }
      throw e
    }
  }

  const get = async (path: string, params?: Record<string, unknown>) => {
    const qs = params
      ? '?' + new URLSearchParams(
          Object.entries(params)
            .filter(([, v]) => v !== undefined && v !== null && v !== '')
            .map(([k, v]) => [k, String(v)]),
        ).toString()
      : ''
    return await request(path + qs)
  }
  const post = async (path: string, body?: unknown) =>
    await request(path, { method: 'POST', body })
  const patch = async (path: string, body?: unknown) =>
    await request(path, { method: 'PATCH', body })
  const put = async (path: string, body?: unknown) =>
    await request(path, { method: 'PUT', body })
  const del = async (path: string) =>
    await request(path, { method: 'DELETE' })

  return { get, post, patch, put, del }
}

export function errorMsg(e: unknown): string {
  if (typeof e === 'object' && e && 'data' in e) {
    const data = (e as { data: { detail?: unknown } }).data
    if (typeof data?.detail === 'string') return data.detail
    if (Array.isArray(data?.detail)) {
      return data.detail.map((d: { msg?: string }) => d.msg || '').join('; ')
    }
  }
  return String(e)
}
