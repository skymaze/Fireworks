// WebSocket 反向代理：浏览器 -> Nitro（同源 :3000） -> 后端 :8000 /ws/events。
//
// 实时通道与 REST 统一走唯一入口（前端端口），浏览器不再直连后端 :8000；
// 中继透传浏览器原始握手请求头（含会话 cookie），后端在握手阶段校验登录态
// （会话无效以 4401 关闭，中继原样转发关闭码给浏览器）。
// 后端只发/收 JSON 文本帧，双向按文本转发即可。
//
// 注意：crossws 的 peer.context 是只读 getter（NodePeer 返回 nodeReq._context），
// 不能用来存状态——上行连接用 Map<peer.id, upstream> 维护，close 时清理。
// 上行连接为异步建连：浏览器消息可能先于 upstream open 到达（竞态），
// 此时排队、upstream open 后补发，避免 log_subscribe 等控制消息丢失。
import { defineWebSocketHandler } from 'h3'

// peer.id -> 到后端的上行 WebSocket
const upstreams = new Map<string, WebSocket>()
// peer.id -> upstream 未就绪期间排队的浏览器消息
const pending = new Map<string, string[]>()
// 单连接 pending 队列长度上限：上游建连竞态期间的内存护栏
const MAX_PENDING = 64

export default defineWebSocketHandler({
  open(peer) {
    const reqHeaders = (peer.request as unknown as { headers?: unknown } | undefined)?.headers
    const getHeader = (name: string): string | undefined => {
      if (!reqHeaders) return undefined
      if (typeof (reqHeaders as Headers).get === 'function') {
        return (reqHeaders as Headers).get(name) ?? undefined
      }
      const raw = (reqHeaders as Record<string, unknown>)[name]
      return Array.isArray(raw) ? (raw as string[]).join(', ') : (raw as string | undefined)
    }
    // Origin 硬校验：浏览器连接必须与本站同源（跨站 WS 劫持纵深防御；
    // SameSite=Lax 的 HttpOnly 会话 cookie 已挡住大部分跨站场景）
    const origin = getHeader('origin')
    if (origin) {
      const host = getHeader('host') ?? ''
      let ok = false
      try {
        ok = new URL(origin).hostname === new URL(`http://${host}`).hostname
      } catch {
        ok = false
      }
      if (!ok) {
        try { peer.close(4403, 'origin rejected') } catch { /* 已关 */ }
        return
      }
    }
    const target = (process.env.API_PROXY_TARGET || 'http://localhost:8000').replace(
      /^http/, 'ws',
    )
    // 透传浏览器原始请求头（主要是 cookie），使后端 WS 认证生效
    const headers: Record<string, string> = {}
    const cookie = getHeader('cookie')
    if (cookie) headers.cookie = cookie

    const fail = (code: number, reason: string) => {
      try { peer.close(code, reason) } catch { /* 对端已关 */ }
    }
    let up: WebSocket
    try {
      // Nitro/undici 运行时支持 { headers }（透传 cookie）；lib.dom 类型过窄，故 as any
      up = new WebSocket(`${target}/ws/events`, { headers } as any)
    } catch {
      fail(1011, 'upstream connect failed')
      return
    }
    upstreams.set(peer.id, up)
    up.addEventListener('open', () => {
      // 建连完成：补发排队消息（log_subscribe 等控制消息）
      const queued = pending.get(peer.id)
      if (queued) {
        pending.delete(peer.id)
        for (const m of queued) {
          try { up.send(m) } catch { /* ignore */ }
        }
      }
    })
    up.addEventListener('message', (ev: MessageEvent) => {
      // 后端 send_json 发文本帧；binary/blob 一并按文本转发
      const data = typeof ev.data === 'string' ? ev.data : String(ev.data)
      try { peer.send(data) } catch { /* 对端已关闭 */ }
    })
    up.addEventListener('close', (ev: CloseEvent) => {
      upstreams.delete(peer.id)
      pending.delete(peer.id)
      // 后端关闭码（如 4401 会话失效）原样转发给浏览器
      fail(ev.code || 1006, (ev.reason || '').slice(0, 96))
    })
    up.addEventListener('error', () => {
      upstreams.delete(peer.id)
      pending.delete(peer.id)
      fail(1006, 'upstream error')
    })
  },
  message(peer, message) {
    const up = upstreams.get(peer.id)
    if (!up) return
    let data: string
    try {
      // crossws Message：rawData 为字符串时直接用，否则按字节流解码（后端均为 UTF-8 文本）
      data = typeof message.rawData === 'string'
        ? message.rawData
        : new TextDecoder().decode(message.uint8Array())
    } catch {
      return
    }
    if (up.readyState === WebSocket.OPEN) {
      try { up.send(data) } catch { /* ignore */ }
    } else if (up.readyState === WebSocket.CONNECTING) {
      // 上行未就绪（异步建连竞态）：排队，open 后补发（超限丢弃，防内存膨胀）
      const q = pending.get(peer.id) ?? []
      if (q.length < MAX_PENDING) {
        q.push(data)
        pending.set(peer.id, q)
      }
    }
    // CLOSED/CLOSING：连接即将终止，直接丢弃
  },
  close(peer) {
    const up = upstreams.get(peer.id)
    if (up) {
      upstreams.delete(peer.id)
      pending.delete(peer.id)
      try { up.close() } catch { /* ignore */ }
    }
  },
})
