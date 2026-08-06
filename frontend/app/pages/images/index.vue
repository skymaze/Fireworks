<script setup lang="ts">
import { errorMsg } from '~/composables/useApi'
const api = useApi()
const confirm = useConfirmDialog()

const imageName = ref('')
const info = ref<any>(null)
const checking = ref(false)
const error = ref('')
const notice = ref('')

const nodes = ref<any[]>([])
const headNodeId = ref<number | null>(null)
const workerIds = ref<number[]>([])
const starting = ref(false)

const transfers = ref<any[]>([])
const localArchives = ref<any[]>([])
const refreshingArchive = ref<string | null>(null)
const completedTransfers = ref<any[]>([])
const completedTotal = ref(0)
const completedOffset = ref(0)
const completedLimit = 20
const showCompleted = ref(false)
const loadingCompleted = ref(false)
const deletingCompleted = ref(false)

const fmt = (v: number) =>
  v >= 1e12 ? `${(v / 1e12).toFixed(1)} TB` : v >= 1e9 ? `${(v / 1e9).toFixed(1)} GB` : v >= 1e6 ? `${(v / 1e6).toFixed(0)} MB` : `${v || 0} KB`

const statusColor: Record<string, string> = {
  pulling: 'info', sending: 'warning', syncing: 'warning', loading: 'warning',
  completed: 'success', failed: 'error', paused: 'neutral', cancelled: 'neutral',
}
const statusLabel = (s: string) =>
  ({ pulling: '控制平面拉取中', sending: '发送到 head', syncing: 'RoCE 同步中', loading: '节点加载中', completed: '完成', failed: '失败', paused: '已暂停', cancelled: '已取消' })[s] || s

const progressOf = (t: any) => {
  const total = t.size_bytes || 1
  if (t.status === 'sending') return Math.min(100, ((t.sent_bytes || 0) / total) * 100)
  return Math.min(100, ((t.downloaded_bytes || 0) / total) * 100)
}

// 速度 / ETA（5s 轮询差值）
const speedSnapshot = ref<Record<number, { bytes: number; ts: number }>>({})
function computeSpeed(t: any): number | null {
  const prev = speedSnapshot.value[t.id]
  const now = Date.now()
  const bytes = t.status === 'sending' ? (t.sent_bytes || 0) : (t.downloaded_bytes || 0)
  if (!prev) {
    speedSnapshot.value[t.id] = { bytes, ts: now }
    return null
  }
  const dt = (now - prev.ts) / 1000
  speedSnapshot.value[t.id] = { bytes, ts: now }
  if (dt <= 0) return null
  const speed = (bytes - prev.bytes) / dt
  return speed > 0 ? speed : null
}
const fmtSpeed = (b: number) =>
  b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB/s` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB/s` : b >= 1e3 ? `${(b / 1e3).toFixed(0)} KB/s` : `${b.toFixed(0)} B/s`
const fmtEta = (sec: number) => {
  if (sec >= 3600) return `${(sec / 3600).toFixed(1)} 小时`
  if (sec >= 60) return `${Math.round(sec / 60)} 分钟`
  return `${Math.round(sec)} 秒`
}

async function checkImage() {
  const img = imageName.value.trim()
  if (!img) return
  checking.value = true
  error.value = ''
  info.value = null
  try {
    info.value = await api.get('/images/inspect', { image: img })
  } catch (e) {
    error.value = errorMsg(e)
  } finally {
    checking.value = false
  }
}

async function loadNodes() {
  nodes.value = await api.get('/nodes')
  if (!headNodeId.value && nodes.value.length) headNodeId.value = nodes.value[0].id
}

async function loadTransfers() {
  try {
    const list = await api.get('/images/transfers', { status: 'active' })
    for (const t of list) {
      const speed = computeSpeed(t)
      t._speed = speed
      t._eta = speed ? fmtEta(Math.max(0, ((t.size_bytes || 0) - (t.status === 'sending' ? t.sent_bytes : t.downloaded_bytes)) / speed)) : null
    }
    transfers.value = list
  } catch { /* ignore */ }
}

async function loadCompletedCount() {
  try {
    const r = await api.get('/images/transfers/count', { status: 'completed' })
    completedTotal.value = r.count || 0
  } catch { /* ignore */ }
}

async function loadCompletedTransfers(reset = false) {
  if (reset) {
    completedOffset.value = 0
    completedTransfers.value = []
  }
  loadingCompleted.value = true
  try {
    const list = await api.get('/images/transfers', {
      status: 'completed', limit: completedLimit, offset: completedOffset.value,
    })
    completedTransfers.value = [...completedTransfers.value, ...list]
    completedOffset.value += list.length
  } catch { /* ignore */ } finally {
    loadingCompleted.value = false
  }
}

function toggleCompleted() {
  showCompleted.value = !showCompleted.value
  if (showCompleted.value) loadCompletedTransfers(true)
}

async function removeTransfer(t: any) {
  const ok = await confirm.open({
    title: '删除任务',
    description: `确认删除传输任务 #${t.id}（${t.image}）？不影响控制平面归档`,
  })
  if (!ok) return
  await api.del(`/images/transfers/${t.id}`)
  notice.value = `已删除任务 #${t.id}`
  await loadTransfers()
}

const ACTIVE_TRANSFER_STATUSES = ['pulling', 'sending', 'syncing', 'loading']

async function pauseTransfer(t: any) {
  try {
    await api.post(`/images/transfers/${t.id}/pause`)
    notice.value = `传输任务 #${t.id} 已暂停`
    await loadTransfers()
  } catch (e) {
    error.value = errorMsg(e)
  }
}

async function resumeTransfer(t: any) {
  try {
    await api.post(`/images/transfers/${t.id}/resume`)
    notice.value = `传输任务 #${t.id} 已继续`
    await loadTransfers()
  } catch (e) {
    error.value = errorMsg(e)
  }
}

async function cancelTransfer(t: any) {
  const ok = await confirm.open({
    title: '取消任务',
    description: `确认取消传输任务 #${t.id}（${t.image}）？归档缓存保留，可重新发起`,
  })
  if (!ok) return
  try {
    await api.post(`/images/transfers/${t.id}/cancel`)
    notice.value = `传输任务 #${t.id} 已取消`
    await loadTransfers()
  } catch (e) {
    error.value = errorMsg(e)
  }
}

async function removeAllCompleted() {
  if (!completedTotal.value) return
  const ok = await confirm.open({
    title: '批量删除',
    description: `确认删除全部 ${completedTotal.value} 条已完成镜像任务？不影响控制平面归档`,
  })
  if (!ok) return
  deletingCompleted.value = true
  try {
    // 逐条删除（归档由「控制平面镜像归档」卡片单独管理）
    for (const t of completedTransfers.value) {
      await api.del(`/images/transfers/${t.id}`)
    }
    completedTransfers.value = []
    completedOffset.value = 0
    await loadCompletedCount()
  } catch (e) {
    error.value = errorMsg(e)
  } finally {
    deletingCompleted.value = false
  }
}

async function startTransfer(onlyPull = false) {
  const img = info.value?.image || imageName.value.trim()
  if (!img) return
  starting.value = true
  error.value = ''
  try {
    const body: Record<string, unknown> = { image: img }
    if (!onlyPull && headNodeId.value) {
      body.head_node_id = headNodeId.value
      body.sync_node_ids = workerIds.value
    }
    const t = await api.post('/images/transfer', body)
    notice.value = t.head_node_id
      ? `镜像传输任务 #${t.id} 已启动：控制平面拉取 → 发送 head → RoCE 同步 → 节点加载`
      : `镜像任务 #${t.id} 已启动：仅下载到控制平面`
    await loadTransfers()
  } catch (e) {
    error.value = errorMsg(e)
  } finally {
    starting.value = false
  }
}

// 拉取设置（代理：仅用于镜像拉取，不影响其他请求）
const pullSettings = ref({ dockerProxy: '' })
const savingPullSettings = ref(false)

async function loadPullSettings() {
  try {
    const s = await api.get('/images/settings')
    pullSettings.value = { dockerProxy: s.docker_proxy || '' }
  } catch { /* ignore */ }
}

async function savePullSettings() {
  savingPullSettings.value = true
  error.value = ''
  try {
    await api.put('/images/settings', { docker_proxy: pullSettings.value.dockerProxy || null })
    notice.value = '镜像拉取设置已保存'
    await loadPullSettings()
  } catch (e) {
    error.value = errorMsg(e)
  } finally {
    savingPullSettings.value = false
  }
}

async function loadLocalArchives() {
  try {
    localArchives.value = (await api.get('/images/local')).archives || []
  } catch { /* ignore */ }
}

async function removeLocalArchive(a: any) {
  const ok = await confirm.open({ title: '删除归档', description: `确认删除镜像归档「${a.image || a.file}」？` })
  if (!ok) return
  try {
    await api.del(`/images/local/${a.file}`)
    notice.value = '已删除归档'
    await loadLocalArchives()
  } catch (e) {
    error.value = errorMsg(e)
  }
}

async function refreshLocalArchive(a: any) {
  refreshingArchive.value = a.file
  error.value = ''
  try {
    const t = await api.post('/images/transfer', { image: a.image, force: true })
    notice.value = `已发起重新拉取任务 #${t.id}（${a.image}），完成后归档更新为最新版本`
    await loadTransfers()
  } catch (e) {
    error.value = errorMsg(e)
  } finally {
    refreshingArchive.value = null
  }
}

// 实时传输进度（WS 推送：agent 拉取进度 -> sent_bytes 实时更新）
const rt = useRealtime()

function onTransferProgress(msg: any) {
  if (msg.kind !== 'image') return
  const t = transfers.value.find((x: any) => x.id === msg.job_id)
  if (t) t.sent_bytes = msg.sent_bytes
}

onMounted(() => {
  loadNodes()
  loadTransfers()
  loadCompletedCount()
  loadPullSettings()
  loadLocalArchives()
  rt.on('transfer_progress', onTransferProgress)
  const t = setInterval(() => {
    loadTransfers()
    loadLocalArchives()
  }, 5000)
  onUnmounted(() => {
    clearInterval(t)
    rt.off('transfer_progress', onTransferProgress)
  })
})
</script>

<template>
  <div>
    <div class="flex items-center justify-between mb-4">
      <h1 class="text-xl font-bold">镜像管理</h1>
    </div>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />
    <UAlert v-if="notice" :title="notice" color="success" class="mb-4" />

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <div class="lg:col-span-2 space-y-4">
        <UCard>
          <template #header><div class="font-semibold">镜像拉取 / 分发</div></template>
          <div class="flex gap-2">
            <UInput
              v-model="imageName"
              class="flex-1"
              placeholder="如 ghcr.io/anemll/dspark-vllm-gx10:0.1.1"
              @keyup.enter="checkImage"
            />
            <UButton color="primary" :loading="checking" @click="checkImage">检查</UButton>
          </div>
          <div v-if="info" class="mt-3 space-y-3">
            <div class="text-xs text-gray-500">
              digest <span class="font-mono">{{ info.digest }}</span> ·
              大小 {{ fmt(info.size_bytes) }} · {{ info.layers }} 层 ·
              <UBadge :color="info.arch === 'arm64' || info.arch === 'aarch64' ? 'success' : 'warning'" variant="subtle" size="sm">
                {{ info.arch || '未知' }}/{{ info.os || '未知' }}
              </UBadge>
              <span v-if="info.arch === 'arm64' || info.arch === 'aarch64'" class="text-gray-700">✓ 适用于 DGX Spark</span>
              <span v-else class="text-warning">需 linux/arm64（DGX Spark 为 aarch64）</span>
            </div>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
              <UFormField label="接收节点（head，仅拉取时无需选择）">
                <USelect
                  v-model="headNodeId"
                  :items="nodes.map((n) => ({ label: `${n.name} (${n.ip})`, value: n.id }))"
                />
              </UFormField>
              <UFormField label="RoCE 同步节点（worker，可多选）">
                <USelect
                  v-model="workerIds"
                  multiple
                  :items="nodes.filter((n) => n.id !== headNodeId).map((n) => ({ label: n.name, value: n.id }))"
                />
              </UFormField>
            </div>
            <div class="flex items-center justify-end gap-2">
              <UButton variant="outline" :loading="starting" @click="startTransfer(true)">
                仅拉取
              </UButton>
              <UButton color="primary" :disabled="!headNodeId" :loading="starting" @click="startTransfer(false)">
                拉取并分发
              </UButton>
            </div>
            <p v-if="!headNodeId" class="text-right text-[11px] text-gray-400">
              未选择 head 时「拉取并分发」不可用，可先仅拉取到控制平面
            </p>
          </div>
        </UCard>

        <UCard>
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">镜像传输任务（{{ transfers.length }}）</div>
              <UButton size="xs" variant="ghost" @click="loadTransfers">刷新</UButton>
            </div>
          </template>
          <div v-if="!transfers.length" class="text-sm text-gray-400 py-4 text-center">暂无进行中或失败的任务</div>
          <div v-for="t in transfers" :key="t.id" class="mb-3 p-2 rounded-md border border-gray-200 dark:border-gray-700">
            <div class="flex items-center justify-between text-sm">
              <span class="font-mono text-xs break-all leading-5">{{ t.image }}</span>
              <div class="flex items-center gap-1 shrink-0">
                <UBadge :color="statusColor[t.status] || 'neutral'" variant="subtle">{{ statusLabel(t.status) }}</UBadge>
                <UButton v-if="ACTIVE_TRANSFER_STATUSES.includes(t.status)" size="xs" variant="ghost" @click="pauseTransfer(t)">暂停</UButton>
                <UButton v-if="t.status === 'paused'" size="xs" variant="ghost" @click="resumeTransfer(t)">继续</UButton>
                <UButton v-if="ACTIVE_TRANSFER_STATUSES.includes(t.status) || t.status === 'paused'" size="xs" variant="ghost" color="error" @click="cancelTransfer(t)">取消</UButton>
                <UButton v-if="t.status === 'failed' || t.status === 'cancelled'" size="xs" variant="ghost" color="error" @click="removeTransfer(t)">删除</UButton>
              </div>
            </div>
            <div class="text-xs text-gray-500 mt-1">
              <template v-if="t.status === 'sending'">
                已发送到 head {{ fmt(t.sent_bytes) }} / {{ fmt(t.size_bytes) }}
                · {{ Math.min(100, ((t.sent_bytes || 0) / (t.size_bytes || 1)) * 100).toFixed(0) }}%
              </template>
              <template v-else-if="t.size_bytes">
                控制平面拉取 {{ fmt(t.downloaded_bytes) }} / {{ fmt(t.size_bytes) }}
                · {{ Math.min(100, ((t.downloaded_bytes || 0) / t.size_bytes) * 100).toFixed(0) }}%
              </template>
            </div>
            <div v-if="(t.status === 'pulling' || t.status === 'sending') && t._speed" class="text-[11px] text-gray-400 mt-1">
              {{ t.status === 'sending' ? '发送速度' : '拉取速度' }} {{ fmtSpeed(t._speed) }}
              <span v-if="t._eta">· 预计剩余 {{ t._eta }}</span>
            </div>
            <UProgress
              class="mt-1"
              :model-value="progressOf(t)"
              :color="t.status === 'failed' ? 'error' : t.status === 'completed' ? 'success' : 'primary'"
              size="sm"
            />
            <div v-if="t.sync_jobs && Object.keys(t.sync_jobs).length" class="text-[11px] text-gray-400 mt-1">
              RoCE 同步: {{ Object.entries(t.sync_jobs).map(([k, v]) => `#${k} ${(v as any).status}`).join(' · ') }}
            </div>
            <div v-if="t.error" class="text-[11px] text-red-500 mt-1">{{ t.error }}</div>
          </div>
        </UCard>

        <UCard>
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">控制平面镜像归档（{{ localArchives.length }}）</div>
              <UButton size="xs" variant="ghost" @click="loadLocalArchives">刷新</UButton>
            </div>
          </template>
          <div v-if="!localArchives.length" class="text-sm text-gray-400 py-2 text-center">暂无归档，拉取后的镜像在此统一管理</div>
          <div v-for="a in localArchives" :key="a.file" class="flex items-center justify-between py-1.5 border-b border-gray-100 dark:border-gray-800/60 last:border-0">
            <div class="min-w-0">
              <div class="font-mono text-xs break-all">{{ a.image || a.file }}</div>
              <div class="text-xs text-gray-500">{{ fmt(a.size_bytes) }}</div>
            </div>
            <div class="flex gap-1 shrink-0">
              <UButton v-if="a.image" size="xs" variant="ghost" :loading="refreshingArchive === a.file" @click="refreshLocalArchive(a)">
                重新拉取
              </UButton>
              <UButton size="xs" variant="ghost" color="error" @click="removeLocalArchive(a)">删除</UButton>
            </div>
          </div>
        </UCard>

        <UCard>
          <template #header>
            <div class="flex items-center justify-between">
              <button class="flex items-center gap-1 font-semibold hover:text-primary" @click="toggleCompleted">
                <span :class="showCompleted ? 'rotate-90' : ''" class="inline-block transition-transform text-xs">▶</span>
                已完成任务（{{ completedTotal }}）
              </button>
              <div v-if="completedTotal" class="flex items-center gap-2">
                <UButton size="xs" variant="outline" color="error" :loading="deletingCompleted" @click="removeAllCompleted">
                  全部删除
                </UButton>
              </div>
            </div>
          </template>
          <div v-if="showCompleted">
            <div v-if="!completedTransfers.length" class="text-sm text-gray-400 py-2 text-center">暂无已完成任务</div>
            <div v-for="t in completedTransfers" :key="t.id" class="flex items-center gap-2 py-1.5 border-b border-gray-100 dark:border-gray-800/60 last:border-0">
              <span class="font-mono text-xs flex-1 min-w-0 break-all">{{ t.image }}</span>
              <span class="text-xs text-gray-500 shrink-0">{{ fmt(t.size_bytes) }}</span>
              <UButton size="xs" variant="ghost" color="error" @click="removeTransfer(t)">删除</UButton>
            </div>
            <div v-if="completedTransfers.length < completedTotal" class="flex justify-center mt-2">
              <UButton size="xs" variant="soft" :loading="loadingCompleted" @click="loadCompletedTransfers(false)">
                加载更多（已显示 {{ completedTransfers.length }} / {{ completedTotal }}）
              </UButton>
            </div>
          </div>
          <div v-else class="text-xs text-gray-400">点击展开查看历史任务（分页加载）</div>
        </UCard>
      </div>

      <UCard>
        <template #header>
          <div class="flex items-center justify-between">
            <div class="font-semibold">拉取设置</div>
            <UButton size="xs" color="primary" variant="soft" :loading="savingPullSettings" @click="savePullSettings">保存</UButton>
          </div>
        </template>
        <div class="space-y-2">
          <UFormField label="拉取代理" hint="支持 http://、https://、socks5://；留空直连">
            <UInput v-model="pullSettings.dockerProxy" placeholder="http://host:port 或 socks5://host:port" />
          </UFormField>
          <p class="text-[11px] text-gray-400">
            代理仅用于镜像拉取（skopeo / registry 请求），不影响模型下载、任务发布等其他网络请求。
          </p>
        </div>
      </UCard>

      <UCard>
        <template #header><div class="font-semibold">说明</div></template>
        <div class="text-xs text-gray-500 space-y-2">
          <p>与模型分发同构：管理平面先用 docker 从公网拉取镜像（仅一份，强制 linux/arm64 确保适配 DGX Spark），经管理网发送 head，再由 head 经 RoCE 高速网同步到 worker，最后各节点 docker load。</p>
          <p>解决多节点同时向公网拉镜像的带宽竞争与网络不稳定问题（尤其 ghcr.io）。</p>
          <p>节点已有同 digest 镜像时自动跳过加载（幂等）；任务支持重试与断点续传。</p>
        </div>
      </UCard>
    </div>
  </div>
</template>
