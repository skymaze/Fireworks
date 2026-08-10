/** 登录态管理（阶段一：单一用户）。
 *
 * 会话为后端 HttpOnly cookie：前端不存储/不透传任何 token，所有 /api 请求
 * （含 WebSocket 握手）浏览器自动携带，跨站脚本无法读取会话凭据。
 *
 * 状态用 useState 跨组件/守卫共享；`checked` 表示是否已向后端确认过登录态
 * （刷新页面后首次判空，避免误跳登录页）。
 */

export interface AuthState {
  setupRequired: boolean
  authenticated: boolean
  username: string | null
  /** 是否已调用过 /api/auth/status 完成确认 */
  checked: boolean
}

const initialAuthState = (): AuthState => ({
  setupRequired: true,
  authenticated: false,
  username: null,
  checked: false,
})

export function useAuth() {
  const state = useState<AuthState>('auth', initialAuthState)

  async function refresh(): Promise<AuthState> {
    try {
      // SSR 内部请求不会自动继承浏览器 cookie；显式透传当前请求 cookie，确保
      // 全局路由守卫在首屏服务端渲染阶段也能得到真实登录态。
      const headers = import.meta.server ? useRequestHeaders(['cookie']) : undefined
      const s = await $fetch<{
        setup_required: boolean
        authenticated: boolean
        username: string | null
      }>('/api/auth/status', { headers })
      state.value = {
        setupRequired: s.setup_required,
        authenticated: s.authenticated,
        username: s.username,
        checked: true,
      }
    } catch {
      // 网络/后端异常：保持上次状态，仅标记已确认（避免误跳登录页）
      state.value = { ...state.value, checked: true }
    }
    return state.value
  }

  async function login(username: string, password: string) {
    const r = await $fetch<{ ok: boolean; username: string }>('/api/auth/login', {
      method: 'POST',
      body: { username, password },
    })
    state.value = {
      setupRequired: false,
      authenticated: true,
      username: r.username,
      checked: true,
    }
    return r
  }

  async function setup(username: string, password: string) {
    const r = await $fetch<{ ok: boolean; username: string }>('/api/auth/setup', {
      method: 'POST',
      body: { username, password },
    })
    state.value = {
      setupRequired: false,
      authenticated: true,
      username: r.username,
      checked: true,
    }
    return r
  }

  async function logout() {
    try {
      await $fetch('/api/auth/logout', { method: 'POST' })
    } catch {
      // 会话可能已失效，忽略；以下状态重置仍执行
    }
    // 立即断开实时通道：登出后已建立的 WebSocket 不能再继续接收广播
    useRealtime().disconnect()
    // 通知实时通道：会话已失效，停止重连（下次登录会自动重建连接）
    state.value = { ...state.value, authenticated: false, username: null, checked: false }
  }

  /** 修改密码：成功后后端已吊销旧会话并为本会话重新签发，可保持在登录态。 */
  async function changePassword(oldPassword: string, newPassword: string) {
    await $fetch<{ ok: boolean }>('/api/auth/change-password', {
      method: 'POST',
      body: { old_password: oldPassword, new_password: newPassword },
    })
  }

  /** 会话失效（401 / WS 4401）时置为未登录，供全局拦截调用。 */
  function invalidate() {
    // 会话已失效：断开已建立的实时连接，避免继续接收广播
    useRealtime().disconnect()
    state.value = { ...state.value, authenticated: false, username: null }
  }

  return { state, refresh, login, setup, logout, changePassword, invalidate }
}
