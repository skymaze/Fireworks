<script setup lang="ts">
import { errorMsg } from '~/composables/useApi'
const { t } = useI18n()
const api = useApi()
const confirm = useConfirmDialog()
const toast = useToast()

const imageName = ref('')
const pulling = ref(false)

const nodes = ref<any[]>([])
const clusters = ref<any[]>([])

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

const statusColor: Record<string, 'primary' | 'secondary' | 'success' | 'info' | 'warning' | 'error' | 'neutral'> = {
  pulling: 'info', packing: 'info', sending: 'warning', syncing: 'warning', loading: 'warning',
  completed: 'success', failed: 'error', paused: 'neutral', cancelled: 'neutral',
}

const progressOf = (t: any) => {
  const total = t.size_bytes || 1
  if (t.status === 'sending') return Math.min(100, ((t.sent_bytes || 0) / total) * 100)
  if (t.status === 'syncing') {
    const jobs = Object.values(t.sync_jobs || {}) as any[]
    if (!jobs.length) return 100
    const done = jobs.reduce((sum, job) => sum + (job.transferred_bytes || 0), 0)
    const syncTotal = jobs.reduce((sum, job) => sum + (job.total_bytes || total), 0)
    return Math.min(100, done / (syncTotal || 1) * 100)
  }
  if (t.status === 'loading' || t.status === 'completed') return 100
  return Math.min(100, ((t.downloaded_bytes || 0) / total) * 100)
}

function progressBytes(t: any) {
  if (t.status === 'sending') return t.sent_bytes || 0
  if (t.status === 'syncing') {
    return (Object.values(t.sync_jobs || {}) as any[])
      .reduce((sum, job) => sum + (job.transferred_bytes || 0), 0)
  }
  return t.downloaded_bytes || 0
}

function progressTotal(t: any) {
  if (t.status !== 'syncing') return t.size_bytes || 0
  return (Object.values(t.sync_jobs || {}) as any[])
    .reduce((sum, job) => sum + (job.total_bytes || t.size_bytes || 0), 0)
}

// 速度 / ETA（5s 轮询差值）
const speedSnapshot = ref<Record<number, { bytes: number; ts: number }>>({})
function computeSpeed(t: any): number | null {
  const prev = speedSnapshot.value[t.id]
  const now = Date.now()
  const bytes = progressBytes(t)
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

async function pullImage() {
  const img = imageName.value.trim()
  if (!img) return
  pulling.value = true
  try {
    await api.get('/images/inspect', { image: img })
    const res = await api.post('/images/transfer', { image: img })
    toast.add({ title: t('images.pull_started', { id: res.id }), color: 'success' })
    await loadTransfers()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    pulling.value = false
  }
}

async function loadNodes() {
  nodes.value = await api.get('/nodes')
}

async function loadClusters() {
  clusters.value = await api.get('/clusters')
}

async function loadTransfers() {
  try {
    const list = await api.get('/images/transfers', { status: 'active' })
    for (const t of list) {
      const speed = computeSpeed(t)
      t._speed = speed
      t._eta = speed ? fmtEta(Math.max(0, (progressTotal(t) - progressBytes(t)) / speed)) : null
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

async function removeTransfer(x: any) {
  const ok = await confirm.open({
    title: t('images.delete_task_title'),
    description: t('images.delete_task_confirm', { id: x.id, image: x.image }),
  })
  if (!ok) return
  await api.del(`/images/transfers/${x.id}`)
  toast.add({ title: t('images.deleted_task', { id: x.id }), color: 'success' })
  await loadTransfers()
}

const ACTIVE_TRANSFER_STATUSES = ['pulling', 'packing', 'sending', 'syncing', 'loading']

async function pauseTransfer(x: any) {
  try {
    await api.post(`/images/transfers/${x.id}/pause`)
    toast.add({ title: t('images.paused_task', { id: x.id }), color: 'success' })
    await loadTransfers()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

async function resumeTransfer(x: any) {
  try {
    await api.post(`/images/transfers/${x.id}/resume`)
    toast.add({ title: t('images.resumed_task', { id: x.id }), color: 'success' })
    await loadTransfers()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

async function cancelTransfer(x: any) {
  const ok = await confirm.open({
    title: t('images.cancel_task_title'),
    description: t('images.cancel_task_confirm', { id: x.id, image: x.image }),
  })
  if (!ok) return
  try {
    await api.post(`/images/transfers/${x.id}/cancel`)
    toast.add({ title: t('images.cancelled_task', { id: x.id }), color: 'success' })
    await loadTransfers()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

async function removeAllCompleted() {
  if (!completedTotal.value) return
  const ok = await confirm.open({
    title: t('images.bulk_delete_title'),
    description: t('images.bulk_delete_confirm', { count: completedTotal.value }),
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
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    deletingCompleted.value = false
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
    try {
    await api.put('/images/settings', { docker_proxy: pullSettings.value.dockerProxy || null })
    toast.add({ title: t('images.settings_saved'), color: 'success' })
    await loadPullSettings()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
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
  const ok = await confirm.open({ title: t('images.delete_archive_title'), description: t('images.delete_archive_confirm', { image: a.image || a.file }) })
  if (!ok) return
  try {
    await api.del(`/images/local/${a.file}`)
    toast.add({ title: t('images.archive_deleted'), color: 'success' })
    await loadLocalArchives()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

async function refreshLocalArchive(a: any) {
  refreshingArchive.value = a.file
    try {
    const res = await api.post('/images/transfer', { image: a.image, force: true })
    toast.add({ title: t('images.repull_started', { id: res.id, image: a.image }), color: 'success' })
    await loadTransfers()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    refreshingArchive.value = null
  }
}

const distributingArchive = ref<any>(null)
const distributing = ref(false)

async function distributeArchive(selection: { clusterId: number; headNodeId: number; syncNodeIds: number[] }) {
  if (!distributingArchive.value?.image) return
  distributing.value = true
  try {
    const res = await api.post('/images/transfer', {
      image: distributingArchive.value.image,
      cluster_id: selection.clusterId,
      head_node_id: selection.headNodeId,
      sync_node_ids: selection.syncNodeIds,
    })
    toast.add({ title: t('images.transfer_started', { id: res.id }), color: 'success' })
    distributingArchive.value = null
    await loadTransfers()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    distributing.value = false
  }
}

// 实时传输进度（WS 推送：agent 拉取进度 -> sent_bytes 实时更新）
const rt = useRealtime()

function onTransferProgress(msg: any) {
  const t = transfers.value.find((x: any) => x.id === msg.job_id)
  if (!t) return
  if (msg.kind === 'image') t.sent_bytes = msg.sent_bytes
  if (msg.kind === 'image-sync' && msg.node_id) {
    t.sync_jobs ||= {}
    t.sync_jobs[String(msg.node_id)] = {
      ...(t.sync_jobs[String(msg.node_id)] || {}),
      status: 'syncing',
      transferred_bytes: msg.sent_bytes,
      total_bytes: msg.total_bytes,
    }
  }
}

let refreshTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  loadNodes()
  loadClusters()
  loadTransfers()
  loadCompletedCount()
  loadPullSettings()
  loadLocalArchives()
  rt.on('transfer_progress', onTransferProgress)
  refreshTimer = setInterval(() => {
    loadTransfers()
    loadLocalArchives()
  }, 5000)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  refreshTimer = null
  rt.off('transfer_progress', onTransferProgress)
})
</script>

<template>
  <UDashboardPanel id="images">
    <template #header>
      <UDashboardNavbar :title="$t('images.title')" >
        <template #leading>
          <UDashboardSidebarCollapse />
        </template>
      </UDashboardNavbar>
          </template>
    <template #body>
    <div>

      <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div class="lg:col-span-2 space-y-4">
          <UCard>
            <template #header><div class="font-semibold">{{ $t('images.pull_distribute') }}</div></template>
            <div class="flex gap-2">
              <UInput
                v-model="imageName"
                class="flex-1"
                :placeholder="$t('images.image_placeholder')"
                @keyup.enter="pullImage"
              />
              <UButton color="primary" :loading="pulling" @click="pullImage">{{ $t('images.pull_only') }}</UButton>
            </div>
            <p class="mt-2 text-[11px] text-gray-400">{{ $t('images.pull_hint') }}</p>
          </UCard>

          <UCard>
            <template #header>
              <div class="flex items-center justify-between">
                <div class="font-semibold">{{ $t('images.transfers_title', { count: transfers.length }) }}</div>
                <UButton size="xs" variant="ghost" @click="loadTransfers">{{ $t('common.refresh') }}</UButton>
              </div>
            </template>
            <div v-if="!transfers.length" class="text-sm text-gray-400 py-4 text-center">{{ $t('images.no_transfers') }}</div>
            <div v-for="t in transfers" :key="t.id" class="mb-3 p-2 rounded-md border border-gray-200 dark:border-gray-700">
              <div class="flex items-center justify-between text-sm">
                <span class="font-mono text-xs break-all leading-5">{{ t.image }}</span>
                <div class="flex items-center gap-1 shrink-0">
                  <UBadge :color="statusColor[t.status] || 'neutral'" variant="subtle">{{ statusLabel(t.status) }}</UBadge>
                  <UButton v-if="ACTIVE_TRANSFER_STATUSES.includes(t.status)" size="xs" variant="ghost" @click="pauseTransfer(t)">{{ $t('images.pause') }}</UButton>
                  <UButton v-if="t.status === 'paused'" size="xs" variant="ghost" @click="resumeTransfer(t)">{{ $t('images.resume') }}</UButton>
                  <UButton v-if="ACTIVE_TRANSFER_STATUSES.includes(t.status) || t.status === 'paused'" size="xs" variant="ghost" color="error" @click="cancelTransfer(t)">{{ $t('common.cancel') }}</UButton>
                  <UButton v-if="t.status === 'failed' || t.status === 'cancelled'" size="xs" variant="ghost" color="error" @click="removeTransfer(t)">{{ $t('common.delete') }}</UButton>
                </div>
              </div>
              <div class="text-xs text-gray-500 mt-1">
                <template v-if="t.status === 'sending'">
                  {{ $t('images.sent_to_head', { sent: fmtBytes(t.sent_bytes), total: fmtBytes(t.size_bytes), pct: Math.min(100, ((t.sent_bytes || 0) / (t.size_bytes || 1)) * 100).toFixed(0) }) }}
                </template>
                <template v-else-if="t.status === 'syncing'">
                  {{ $t('images.sync_summary', { done: fmtBytes(progressBytes(t)), total: fmtBytes(progressTotal(t)), pct: progressOf(t).toFixed(0) }) }}
                </template>
                <template v-else-if="t.size_bytes">
                  {{ $t('images.plane_pull', { done: fmtBytes(t.downloaded_bytes), total: fmtBytes(t.size_bytes), pct: Math.min(100, ((t.downloaded_bytes || 0) / t.size_bytes) * 100).toFixed(0) }) }}
                </template>
              </div>
              <div v-if="['pulling', 'sending', 'syncing'].includes(t.status) && t._speed" class="text-[11px] text-gray-400 mt-1">
                {{ t.status === 'sending' ? $t('images.send_speed') : t.status === 'syncing' ? $t('images.sync_speed') : $t('images.pull_speed') }} {{ fmtSpeed(t._speed) }}
                <span v-if="t._eta">{{ $t('common.eta', { eta: t._eta }) }}</span>
              </div>
              <UProgress
                class="mt-1"
                :model-value="progressOf(t)"
                :color="t.status === 'failed' ? 'error' : t.status === 'completed' ? 'success' : 'primary'"
                size="sm"
              />
              <div v-if="t.sync_jobs && Object.keys(t.sync_jobs).length" class="space-y-1 mt-2">
                <div v-for="(job, nodeId) in t.sync_jobs" :key="nodeId" class="text-[11px] text-gray-500">
                  <div class="flex justify-between gap-2">
                    <span>{{ $t('images.node_transfer', { node: nodes.find(n => String(n.id) === String(nodeId))?.name || `#${nodeId}` }) }}</span>
                    <span>{{ statusLabel((job as any).status) }} · {{ fmtBytes((job as any).transferred_bytes || 0) }} / {{ fmtBytes((job as any).total_bytes || t.size_bytes) }}</span>
                  </div>
                  <UProgress :model-value="Math.min(100, ((job as any).transferred_bytes || 0) / ((job as any).total_bytes || t.size_bytes || 1) * 100)" size="xs" />
                </div>
              </div>
              <div v-if="t.error" class="text-[11px] text-red-500 mt-1">{{ t.error }}</div>
            </div>
          </UCard>

          <UCard>
            <template #header>
              <div class="flex items-center justify-between">
                <div class="font-semibold">{{ $t('images.archives_title', { count: localArchives.length }) }}</div>
                <UButton size="xs" variant="ghost" @click="loadLocalArchives">{{ $t('common.refresh') }}</UButton>
              </div>
            </template>
            <div v-if="!localArchives.length" class="text-sm text-gray-400 py-2 text-center">{{ $t('images.no_archives') }}</div>
            <div v-for="a in localArchives" :key="a.file" class="flex items-center justify-between py-1.5 border-b border-gray-100 dark:border-gray-800/60 last:border-0">
              <div class="min-w-0">
                <div class="font-mono text-xs break-all">{{ a.image || a.file }}</div>
                <div class="text-xs text-gray-500">{{ fmtBytes(a.size_bytes) }}</div>
              </div>
              <div class="flex gap-1 shrink-0">
                <UButton v-if="a.image" size="xs" variant="ghost" @click="distributingArchive = a">
                  {{ $t('images.distribute') }}
                </UButton>
                <UButton v-if="a.image" size="xs" variant="ghost" :loading="refreshingArchive === a.file" @click="refreshLocalArchive(a)">
                  {{ $t('images.repull') }}
                </UButton>
                <UButton size="xs" variant="ghost" color="error" @click="removeLocalArchive(a)">{{ $t('common.delete') }}</UButton>
              </div>
            </div>
          </UCard>

          <UCard>
            <template #header>
              <div class="flex items-center justify-between">
                <UButton color="neutral" variant="link" class="p-0 font-semibold" @click="toggleCompleted">
                  <span :class="showCompleted ? 'rotate-90' : ''" class="inline-block transition-transform text-xs">▶</span>
                  {{ $t('images.completed_title', { count: completedTotal }) }}
                </UButton>
                <div v-if="completedTotal" class="flex items-center gap-2">
                  <UButton size="xs" variant="outline" color="error" :loading="deletingCompleted" @click="removeAllCompleted">
                    {{ $t('images.delete_all') }}
                  </UButton>
                </div>
              </div>
            </template>
            <div v-if="showCompleted">
              <div v-if="!completedTransfers.length" class="text-sm text-gray-400 py-2 text-center">{{ $t('images.no_completed') }}</div>
              <div v-for="t in completedTransfers" :key="t.id" class="flex items-center gap-2 py-1.5 border-b border-gray-100 dark:border-gray-800/60 last:border-0">
                <span class="font-mono text-xs flex-1 min-w-0 break-all">{{ t.image }}</span>
                <span class="text-xs text-gray-500 shrink-0">{{ fmtBytes(t.size_bytes) }}</span>
                <UButton size="xs" variant="ghost" color="error" @click="removeTransfer(t)">{{ $t('common.delete') }}</UButton>
              </div>
              <div v-if="completedTransfers.length < completedTotal" class="flex justify-center mt-2">
                <UButton size="xs" variant="soft" :loading="loadingCompleted" @click="loadCompletedTransfers(false)">
                  {{ $t('images.load_more', { shown: completedTransfers.length, total: completedTotal }) }}
                </UButton>
              </div>
            </div>
            <div v-else class="text-xs text-gray-400">{{ $t('images.expand_history') }}</div>
          </UCard>
        </div>

        <UCard>
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">{{ $t('images.pull_settings') }}</div>
              <UButton size="xs" color="primary" variant="soft" :loading="savingPullSettings" @click="savePullSettings">{{ $t('common.save') }}</UButton>
            </div>
          </template>
          <div class="space-y-2">
            <UFormField :label="$t('images.proxy_label')" :hint="$t('images.proxy_hint')">
              <UInput v-model="pullSettings.dockerProxy" :placeholder="$t('images.proxy_placeholder')" />
            </UFormField>
            <p class="text-[11px] text-gray-400">
              {{ $t('images.proxy_note') }}
            </p>
          </div>
        </UCard>

        <UCard>
          <template #header><div class="font-semibold">{{ $t('images.info') }}</div></template>
          <div class="text-xs text-gray-500 space-y-2">
            <p>{{ $t('images.info_1') }}</p>
            <p>{{ $t('images.info_2') }}</p>
            <p>{{ $t('images.info_3') }}</p>
          </div>
        </UCard>
      </div>
      <DistributionModal
        :open="!!distributingArchive"
        :title="$t('images.distribute_title')"
        :resource="distributingArchive?.image || ''"
        :clusters="clusters"
        :loading="distributing"
        @update:open="(open) => { if (!open && !distributing) distributingArchive = null }"
        @submit="distributeArchive"
      />
    </div>
    </template>
  </UDashboardPanel>
</template>
