/**
 * 实时数据通道（WebSocket 单例）。
 *
 * - 经前端代理（Nitro server）同源连接 ws(s)://{host}/api/ws/events，
 *   会话 cookie 自动携带，不直连后端端口；
 * - 自动重连（指数退避 1s→30s），`connected` 暴露连接状态供页面降级轮询；
 * - 会话失效（后端 4401 关闭，如登出/过期）时停止重连，交由登录守卫/A 401 跳转处理；
 * - 事件订阅：on(type, handler) / off(type, handler)；send() 发送控制消息
 *   （日志订阅/退订）。
 *
 * 服务端事件类型：
 *   metrics            {node_id, data}         指标（每 5s/节点）
 *   node_status        {node_id, status, last_seen}  节点上线/下线（WS 连接+心跳看门狗秒级判定）
 *   container_status   {task_id, node_id, container, status}
 *   task_status        {task_id, status}
 *   transfer_progress  {kind, job_id, sent_bytes, total_bytes}
 *   log                {container, line}
 *   log_end            {container}
 */

type Handler = (payload: any) => void

const listeners = new Map<string, Set<Handler>>()
let ws: WebSocket | null = null
let retry = 0
let retryTimer: ReturnType<typeof setTimeout> | null = null
// 连接建立前的订阅消息排队，onopen 后补发（避免 log_subscribe 等控制消息丢失）
let pending: Record<string, unknown>[] = []

export function useRealtime() {
  const connected = ref(false)

  function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    // 同源连接，由 Nitro server 反向代理到后端 /ws/events
    ws = new WebSocket(`${proto}://${location.host}/api/ws/events`)
    ws.onopen = () => {
      retry = 0
      connected.value = true
      const queued = pending
      pending = []
      for (const m of queued) ws!.send(JSON.stringify(m))
    }
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        const type = msg?.type
        if (!type) return
        const hs = listeners.get(type)
        if (hs) for (const h of hs) h(msg)
      } catch { /* ignore malformed */ }
    }
    ws.onclose = (e) => {
      ws = null
      connected.value = false
      if (e.code !== 4401) {
        // 会话失效（4401）：停止重连，避免对已失效会话反复握手
        scheduleReconnect()
      }
    }
    ws.onerror = () => {
      // onclose 会触发重连
    }
  }

  function scheduleReconnect() {
    if (retryTimer) return
    const delay = Math.min(1000 * 2 ** retry, 30000)
    retry++
    retryTimer = setTimeout(() => {
      retryTimer = null
      connect()
    }, delay)
  }

  /** 主动关闭实时连接（登出/会话失效时调用），并重置重连状态。 */
  function disconnect() {
    if (retryTimer) {
      clearTimeout(retryTimer)
      retryTimer = null
    }
    retry = 0
    pending = []
    const sock = ws
    ws = null
    connected.value = false
    if (sock) {
      try { sock.close() } catch { /* ignore */ }
    }
  }

  function on(type: string, handler: Handler) {
    if (!listeners.has(type)) listeners.set(type, new Set())
    listeners.get(type)!.add(handler)
    connect()
  }

  function off(type: string, handler: Handler) {
    listeners.get(type)?.delete(handler)
    if (listeners.get(type)?.size === 0) listeners.delete(type)
  }

  function send(msg: Record<string, unknown>) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg))
    } else {
      pending.push(msg) // 排队，连接建立后补发
      connect()
    }
  }

  return { connected, on, off, send, disconnect }
}
