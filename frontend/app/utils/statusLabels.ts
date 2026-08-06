/** 状态枚举 → i18n 文案（后端状态值为英文机器值，展示时按当前语言映射）。 */

const STATUS_KEYS: Record<string, string> = {
  // 节点 / Agent
  online: 'status.online',
  offline: 'status.offline',
  unknown: 'status.unknown',
  error: 'status.error',
  // 任务
  published: 'status.published',
  running: 'status.running',
  paused: 'status.paused',
  stopped: 'status.stopped',
  // 模型传输
  downloading: 'status.downloading',
  sending: 'status.sending',
  syncing: 'status.syncing',
  completed: 'status.completed',
  failed: 'status.failed',
  cancelled: 'status.cancelled',
  // 镜像传输
  pulling: 'status.pulling',
  loading: 'status.loading',
  // 本地模型缓存
  complete: 'status.complete',
  partial: 'status.partial',
  // 容器状态
  exited: 'status.exited',
  created: 'status.created',
  restarting: 'status.restarting',
  dead: 'status.dead',
  // 角色
  head: 'status.head',
  worker: 'status.worker',
}

export function statusLabel(status?: string | null): string {
  if (!status) return ''
  const key = STATUS_KEYS[status]
  if (!key) return status // 未知枚举原样显示（多为后端新增值）
  try {
    const nuxt = useNuxtApp() as any
    if (typeof nuxt?.$t === 'function') return nuxt.$t(key) as string
  } catch {
    /* 回退原始值 */
  }
  return status
}
