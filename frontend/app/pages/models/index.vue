<script setup lang="ts">
import { errorMsg } from '~/composables/useApi'
const { t } = useI18n()
const api = useApi()
const confirm = useConfirmDialog()

const query = ref('')
const directRepo = ref('')
const results = ref<any[]>([])
const searching = ref(false)
const toast = useToast()

const nodes = ref<any[]>([])
const downloads = ref<any[]>([])
const completedDownloads = ref<any[]>([])
const completedTotal = ref(0)
const completedOffset = ref(0)
const completedLimit = 20
const showCompleted = ref(false)
const deletingCompleted = ref(false)
const loadingCompleted = ref(false)
const localModels = ref<any[]>([])
const selectedModel = ref<string | null>(null)
const modelInfo = ref<any>(null)
const headNodeId = ref<number | null>(null)
const workerIds = ref<number[]>([])
const starting = ref(false)
const cacheOnly = ref(false)

// 下载设置（endpoint / token / 连接数 / 分片 / 镜像代理）
const settings = ref({
  endpoint: 'https://huggingface.co',
  customEndpoint: '',
  hasToken: false,
  connections: 8,
  chunkSizeMb: 8,
  hfToken: '',
})
const savingSettings = ref(false)
const presetEndpoints = ['https://huggingface.co', 'https://hf-mirror.com']

async function loadSettings() {
  try {
    const s = await api.get('/models/settings')
    settings.value = { ...settings.value, ...s, hfToken: '' }
    if (!presetEndpoints.includes(s.endpoint)) {
      settings.value.customEndpoint = s.endpoint
      settings.value.endpoint = '__custom__'
    }
  } catch { /* ignore */ }
}

async function saveSettings() {
  savingSettings.value = true
  try {
    const body: Record<string, unknown> = {
      endpoint: settings.value.endpoint === '__custom__' ? settings.value.customEndpoint : settings.value.endpoint,
      connections: settings.value.connections,
      chunk_size_mb: settings.value.chunkSizeMb,
    }
    if (settings.value.hfToken) body.hf_token = settings.value.hfToken
    await api.put('/models/settings', body)
    toast.add({ title: t('models.settings_saved'), color: 'success' })
    await loadSettings()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    savingSettings.value = false
  }
}

async function clearToken() {
  await api.put('/models/settings', { hf_token: null })
  toast.add({ title: t('models.token_cleared'), color: 'success' })
  await loadSettings()
}

async function search() {
  if (!query.value.trim()) return
  searching.value = true
  try {
    results.value = await api.get('/models/search', { q: query.value.trim(), limit: 12 })
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    searching.value = false
  }
}

async function pickModel(repo: string) {
  selectedModel.value = repo
  modelInfo.value = null
  try {
    modelInfo.value = await api.get(`/models/${repo}/info`)
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

async function directDownload() {
  const repo = directRepo.value.trim()
  if (!repo) {
    toast.add({ title: t('models.name_required'), color: 'error' })
    return
  }
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) {
    toast.add({ title: t('models.repo_format'), color: 'error' })
    return
  }
  selectedModel.value = null
  try {
    modelInfo.value = await api.get(`/models/${repo}/info`)
    selectedModel.value = repo
    toast.add({ title: t('models.repo_loaded', { repo }), color: 'success' })
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

async function removeDownload(j: any) {
  const ok = await confirm.open({
    title: t('models.delete_task_title'),
    description: t('models.delete_task_confirm', { id: j.id, repo: j.repo }),
  })
  if (!ok) return
  try {
    await api.del(`/models/downloads/${j.id}?cleanup=1`)
    toast.add({ title: t('models.task_deleted_cleaned', { id: j.id }), color: 'success' })
    await loadDownloads()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

const ACTIVE_JOB_STATUSES = ['downloading', 'sending', 'syncing']

async function pauseDownload(j: any) {
  try {
    await api.post(`/models/downloads/${j.id}/pause`)
    toast.add({ title: t('models.paused_task', { id: j.id }), color: 'success' })
    await loadDownloads()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

async function resumeDownload(j: any) {
  try {
    await api.post(`/models/downloads/${j.id}/resume`)
    toast.add({ title: t('models.resumed_task', { id: j.id }), color: 'success' })
    await loadDownloads()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

async function cancelDownload(j: any) {
  const ok = await confirm.open({
    title: t('models.cancel_task_title'),
    description: t('models.cancel_task_confirm', { id: j.id, repo: j.repo }),
  })
  if (!ok) return
  try {
    await api.post(`/models/downloads/${j.id}/cancel`)
    toast.add({ title: t('models.cancelled_task', { id: j.id }), color: 'success' })
    await loadDownloads()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

async function loadNodes() {
  nodes.value = await api.get('/nodes')
  if (!headNodeId.value && nodes.value.length) headNodeId.value = nodes.value[0].id
  if (!workerIds.value.length) {
    workerIds.value = nodes.value.filter((n) => n.id !== headNodeId.value).map((n) => n.id)
  }
}

watch(headNodeId, (headId) => {
  workerIds.value = nodes.value.filter((n) => n.id !== headId).map((n) => n.id)
})

// 任务速度 / 预计完成时间（基于 5s 轮询差值在前端计算）
const speedSnapshot = ref<Record<number, { bytes: number; ts: number }>>({})

function taskProgressBytes(j: any): number {
  if (j.status === 'syncing') {
    return (Object.values(j.sync_jobs || {}) as any[])
      .reduce((sum, item) => sum + (item.transferred_bytes || 0), 0)
  }
  return j.status === 'sending' ? (j.sent_bytes || 0) : (j.downloaded_bytes || 0)
}

function taskProgressTotal(j: any): number {
  if (j.status === 'syncing') {
    return (Object.values(j.sync_jobs || {}) as any[])
      .reduce((sum, item) => sum + (item.total_bytes || j.total_bytes || 0), 0)
  }
  return j.total_bytes || 0
}

function computeTaskSpeed(j: any): number | null {
  const prev = speedSnapshot.value[j.id]
  const now = Date.now()
  const bytes = taskProgressBytes(j)
  if (!prev) {
    speedSnapshot.value[j.id] = { bytes, ts: now }
    return null
  }
  const dt = (now - prev.ts) / 1000
  speedSnapshot.value[j.id] = { bytes, ts: now }
  if (dt <= 0) return null
  const speed = (bytes - prev.bytes) / dt
  return speed > 0 ? speed : null
}

function computeTaskEta(j: any, speed: number | null): string | null {
  if (!speed) return null
  const remaining = taskProgressTotal(j) - taskProgressBytes(j)
  if (remaining <= 0) return null
  return fmtEta(remaining / speed)
}

async function loadDownloads() {
  // 进行中 + 失败任务（轮询），并计算速度/预计完成时间
  try {
    const list = await api.get('/models/downloads', { status: 'active' })
    for (const j of list) {
      const speed = computeTaskSpeed(j)
      j._speed = speed
      j._eta = computeTaskEta(j, speed)
    }
    downloads.value = list
  } catch { /* ignore */ }
}

async function loadCompletedCount() {
  try {
    const r = await api.get('/models/downloads/count', { status: 'completed' })
    completedTotal.value = r.count || 0
  } catch { /* ignore */ }
}

async function loadCompletedDownloads(reset = false) {
  if (reset) {
    completedOffset.value = 0
    completedDownloads.value = []
  }
  loadingCompleted.value = true
  try {
    // 最新在前（id desc），分页：limit/offset
    const list = await api.get('/models/downloads', {
      status: 'completed', limit: completedLimit, offset: completedOffset.value,
    })
    completedDownloads.value = [...completedDownloads.value, ...list]
    completedOffset.value += list.length
  } catch { /* ignore */ } finally {
    loadingCompleted.value = false
  }
}

function toggleCompleted() {
  showCompleted.value = !showCompleted.value
  if (showCompleted.value) loadCompletedDownloads(true)  // 每次展开都刷新为最新 N 条
}

async function removeCompleted(j: any) {
  const ok = await confirm.open({
    title: t('models.delete_task_title'),
    description: t('models.delete_completed_confirm', { id: j.id, repo: j.repo }),
  })
  if (!ok) return
  try {
    await api.del(`/models/downloads/${j.id}?cleanup=1`)
    toast.add({ title: t('models.deleted_task', { id: j.id }), color: 'success' })
    completedDownloads.value = completedDownloads.value.filter((x) => x.id !== j.id)
    await loadCompletedCount()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

async function removeAllCompleted() {
  if (!completedTotal.value) return
  const ok = await confirm.open({
    title: t('models.bulk_delete_title'),
    description: t('models.bulk_delete_confirm', { count: completedTotal.value }),
  })
  if (!ok) return
  deletingCompleted.value = true
  try {
    const r = await api.del('/models/downloads/all-completed?cleanup=1')
    toast.add({ title: t('models.bulk_deleted', { deleted: r.deleted, cleaned: r.cleaned_files }), color: 'success' })
    completedDownloads.value = []
    completedOffset.value = 0
    await loadCompletedCount()
    await loadLocalModels()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    deletingCompleted.value = false
  }
}

async function retryDownload(j: any) {
  try {
    const job = await api.post(`/models/downloads/${j.id}/retry`)
    toast.add({ title: t('models.retried', { id: job.id, repo: j.repo }), color: 'success' })
    await loadDownloads()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

async function loadLocalModels() {
  try {
    localModels.value = (await api.get('/models/local')).models || []
  } catch {
    /* ignore */
  }
}

async function removeLocalModel(m: any) {
  if (m.status === 'downloading') {
    toast.add({ title: t('models.cannot_delete_downloading'), color: 'error' })
    return
  }
  const label = m.status === 'complete' ? t('models.cache_complete_label') : t('models.cache_partial_label')
  const ok = await confirm.open({ title: t('models.delete_model_title'), description: t('models.delete_model_confirm', { label, repo: m.repo }) })
  if (!ok) return
  try {
    await api.del(`/models/local/${m.repo}`)
    toast.add({ title: t('models.deleted_repo', { repo: m.repo }), color: 'success' })
    await loadLocalModels()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

// 仅分发（与下载解耦）：本地缓存 -> head -> Agent 高速直传 worker
const distributingRepo = ref<string | null>(null)
const distHeadId = ref<number | null>(null)
const distWorkerIds = ref<number[]>([])
const distributing = ref(false)

watch(distHeadId, (headId) => {
  if (distributingRepo.value) {
    distWorkerIds.value = nodes.value.filter((n) => n.id !== headId).map((n) => n.id)
  }
})

function toggleDistribute(repo: string) {
  distributingRepo.value = distributingRepo.value === repo ? null : repo
  distHeadId.value = nodes.value[0]?.id ?? null
  distWorkerIds.value = nodes.value.filter((n) => n.id !== distHeadId.value).map((n) => n.id)
}

async function doDistribute(repo: string) {
  distributing.value = true
  try {
    const job = await api.post('/models/distribute', {
      repo,
      head_node_id: distHeadId.value,
      sync_node_ids: distWorkerIds.value,
    })
    toast.add({ title: t('models.distribute_started', { id: job.id }), color: 'success' })
    distributingRepo.value = null
    await loadDownloads()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    distributing.value = false
  }
}

async function startDownload() {
  starting.value = true
  try {
    const body: Record<string, unknown> = { repo: selectedModel.value }
    if (!cacheOnly.value) {
      body.head_node_id = headNodeId.value
      body.sync_node_ids = workerIds.value
    }
    const job = await api.post('/models/download', body)
    toast.add({ title: !cacheOnly.value ? t('models.download_distribute_started', { id: job.id }) : t('models.download_started', { id: job.id }), color: 'success' })
    await loadDownloads()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    starting.value = false
  }
}

const statusColor: Record<string, 'primary' | 'secondary' | 'success' | 'info' | 'warning' | 'error' | 'neutral'> = {
  downloading: 'info', sending: 'warning', syncing: 'warning', completed: 'success',
  failed: 'error', paused: 'neutral', cancelled: 'neutral',
}
// 模型缓存多态状态（状态枚举 → i18n 文案）
const modelStatusLabel = (s: string) =>
  ({ complete: t('status.complete'), downloading: t('status.downloading'), failed: t('status.failed'), partial: t('status.partial') })[s] || s
const modelStatusColor: Record<string, 'primary' | 'secondary' | 'success' | 'info' | 'warning' | 'error' | 'neutral'> = {
  complete: 'success', downloading: 'info', failed: 'error', partial: 'warning',
}

const progressOf = (j: any) => {
  const total = taskProgressTotal(j) || 1
  return Math.min(100, (taskProgressBytes(j) / total) * 100)
}

const nodeName = (id: string | number) => nodes.value.find((n) => String(n.id) === String(id))?.name || String(id)

// 实时传输进度（WS 推送：agent 拉取进度 -> sent_bytes 实时更新）
const rt = useRealtime()

function onTransferProgress(msg: any) {
  const j = downloads.value.find((x: any) => x.id === msg.job_id)
  if (!j) return
  if (msg.kind === 'model') j.sent_bytes = msg.sent_bytes
  if (msg.kind === 'model-sync') {
    j.sync_jobs ||= {}
    j.sync_jobs[String(msg.node_id)] = {
      ...(j.sync_jobs[String(msg.node_id)] || {}),
      status: 'syncing', transferred_bytes: msg.sent_bytes, total_bytes: msg.total_bytes,
    }
  }
}

let refreshTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  loadNodes()
  loadDownloads()
  loadCompletedCount()
  loadLocalModels()
  loadSettings()
  rt.on('transfer_progress', onTransferProgress)
  refreshTimer = setInterval(() => {
    loadDownloads()
    loadLocalModels()
  }, 5000)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  refreshTimer = null
  rt.off('transfer_progress', onTransferProgress)
})
</script>

<template>
  <UDashboardPanel id="models">
    <template #header>
      <UDashboardNavbar :title="$t('models.title')" >
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
            <template #header><div class="font-semibold">{{ $t('models.search_title') }}</div></template>
            <div class="flex gap-2">
              <UInput v-model="query" class="flex-1" :placeholder="$t('models.search_placeholder')" @keyup.enter="search" />
              <UButton color="primary" :loading="searching" @click="search">{{ $t('common.search') }}</UButton>
            </div>
            <div v-if="results.length" class="mt-3 space-y-2">
              <div
                v-for="m in results"
                :key="m.id"
                class="flex items-center justify-between p-2 rounded-md border border-gray-200 dark:border-gray-700 cursor-pointer hover:border-primary"
                @click="pickModel(m.id)"
              >
                <div>
                  <div class="text-sm font-medium">{{ m.id }}</div>
                  <div class="text-xs text-gray-500">{{ $t('models.downloads_likes', { downloads: fmtNumber(m.downloads || 0), likes: fmtNumber(m.likes || 0) }) }}</div>
                </div>
                <UButton size="xs" variant="ghost" @click.stop="pickModel(m.id)">{{ $t('models.select') }}</UButton>
              </div>
            </div>
          </UCard>

          <UCard>
            <template #header><div class="font-semibold">{{ $t('models.direct_download') }}</div></template>
            <div class="flex gap-2">
              <UInput
                v-model="directRepo"
                class="flex-1"
                :placeholder="$t('models.repo_placeholder')"
                @keyup.enter="directDownload"
              />
              <UButton variant="soft" @click="directDownload">{{ $t('models.direct_download') }}</UButton>
            </div>
            <p class="text-[11px] text-gray-400 mt-2">{{ $t('models.direct_hint') }}</p>
          </UCard>

          <UCard v-if="selectedModel">
            <template #header>
              <div class="font-semibold">{{ selectedModel }}</div>
            </template>
            <div class="text-xs text-gray-500 mb-3" v-if="modelInfo">
              {{ $t('models.total_size', { size: fmtBytes(modelInfo.total_size), count: modelInfo.siblings?.length || 0 }) }}
            </div>
            <UAlert
              color="info"
              variant="subtle"
              class="mb-3"
              :title="$t('models.download_info_title')"
              :description="$t('models.download_info')"
            />
            <div class="flex items-center justify-between gap-4 mb-3">
              <div>
                <div class="text-sm font-medium">{{ $t('models.cache_only') }}</div>
                <div class="text-xs text-gray-500">{{ $t('models.cache_only_hint') }}</div>
              </div>
              <USwitch v-model="cacheOnly" />
            </div>
            <div v-if="!cacheOnly" class="grid grid-cols-1 md:grid-cols-2 gap-3">
              <UFormField :label="$t('models.head_node')">
                <USelectMenu value-key="value"
                  v-model="headNodeId"
                  :items="nodes.map((n) => ({ label: `${n.name} (${n.ip})`, value: n.id }))"
                />
              </UFormField>
              <UFormField :label="$t('models.roce_sync_nodes')">
                <USelectMenu value-key="value"
                  v-model="workerIds"
                  multiple
                  :items="nodes.filter((n) => n.id !== headNodeId).map((n) => ({ label: n.name, value: n.id }))"
                />
              </UFormField>
            </div>
            <template #footer>
              <div class="flex justify-end">
              <UButton
                color="primary"
                :loading="starting"
                :disabled="!cacheOnly && !headNodeId"
                @click="startDownload"
              >
                {{ cacheOnly ? $t('models.btn_download') : $t('models.btn_distribute') }}
              </UButton>
              </div>
            </template>
          </UCard>

          <UCard>
            <template #header>
              <div class="flex items-center justify-between">
                <div class="font-semibold">{{ $t('models.cache_title', { count: localModels.length }) }}</div>
                <UButton size="xs" variant="ghost" @click="loadLocalModels">{{ $t('common.refresh') }}</UButton>
              </div>
            </template>
            <div v-if="!localModels.length" class="text-sm text-gray-400 py-2 text-center">{{ $t('models.no_cache') }}</div>
            <div v-for="m in localModels" :key="m.repo" class="py-1.5 border-b border-gray-100 dark:border-gray-800/60 last:border-0">
              <div class="flex items-center justify-between">
                <div class="min-w-0">
                  <div class="font-mono text-xs">{{ m.repo }}</div>
                  <div class="text-xs text-gray-500 mt-0.5 flex items-center gap-1.5">
                    <UBadge :color="modelStatusColor[m.status] || 'neutral'" variant="subtle" size="sm">
                      {{ modelStatusLabel(m.status) }}
                    </UBadge>
                    <span>{{ fmtBytes(m.size_bytes) }}</span>
                  </div>
                </div>
                <div class="flex gap-1 shrink-0">
                  <UButton v-if="m.status === 'complete'" size="xs" variant="ghost" @click="toggleDistribute(m.repo)">{{ $t('models.distribute') }}</UButton>
                  <UButton
                    size="xs"
                    variant="ghost"
                    color="error"
                    :disabled="m.status === 'downloading'"
                    @click="removeLocalModel(m)"
                  >
                    {{ m.status === 'downloading' ? $t('status.downloading') : $t('common.delete') }}
                  </UButton>
                </div>
              </div>
              <div v-if="distributingRepo === m.repo" class="mt-2 p-2 rounded-md bg-gray-100/70 dark:bg-gray-800/60 space-y-2">
                <USelectMenu value-key="value"
                  v-model="distHeadId"
                  :items="nodes.map((n) => ({ label: `${n.name} (${n.ip})`, value: n.id }))"
                  :placeholder="$t('models.dist_head_placeholder')"
                />
                <USelectMenu value-key="value"
                  v-model="distWorkerIds"
                  multiple
                  :items="nodes.filter((n) => n.id !== distHeadId).map((n) => ({ label: n.name, value: n.id }))"
                  :placeholder="$t('models.roce_sync_nodes')"
                />
                <div class="flex justify-end gap-2">
                  <UButton size="xs" variant="outline" @click="distributingRepo = null">{{ $t('common.cancel') }}</UButton>
                  <UButton size="xs" color="primary" :loading="distributing" :disabled="!distHeadId" @click="doDistribute(m.repo)">
                    {{ $t('models.confirm_distribute') }}
                  </UButton>
                </div>
              </div>
            </div>
          </UCard>

          <UCard>
            <template #header>
              <div class="flex items-center justify-between">
                <div class="font-semibold">{{ $t('models.ongoing_title', { count: downloads.length }) }}</div>
                <UButton size="xs" variant="ghost" @click="loadDownloads">{{ $t('common.refresh') }}</UButton>
              </div>
            </template>
            <div v-if="!downloads.length" class="text-sm text-gray-400 py-4 text-center">{{ $t('models.no_ongoing') }}</div>
            <div v-for="j in downloads" :key="j.id" class="mb-3 p-2 rounded-md border border-gray-200 dark:border-gray-700">
              <div class="flex items-center justify-between text-sm">
                <span class="font-mono text-xs break-all leading-5">{{ j.repo }}</span>
                <div class="flex items-center gap-1 shrink-0">
                  <UBadge :color="statusColor[j.status] || 'neutral'" variant="subtle">{{ statusLabel(j.status) }}</UBadge>
                  <UButton v-if="ACTIVE_JOB_STATUSES.includes(j.status)" size="xs" variant="ghost" @click="pauseDownload(j)">{{ $t('models.pause') }}</UButton>
                  <UButton v-if="j.status === 'paused'" size="xs" variant="ghost" @click="resumeDownload(j)">{{ $t('models.resume') }}</UButton>
                  <UButton v-if="ACTIVE_JOB_STATUSES.includes(j.status) || j.status === 'paused'" size="xs" variant="ghost" color="error" @click="cancelDownload(j)">{{ $t('common.cancel') }}</UButton>
                  <UButton v-if="j.status === 'failed'" size="xs" variant="ghost" @click="retryDownload(j)">{{ $t('models.retry') }}</UButton>
                  <UButton v-if="j.status === 'failed' || j.status === 'cancelled'" size="xs" variant="ghost" color="error" @click="removeDownload(j)">
                    {{ $t('common.delete') }}
                  </UButton>
                </div>
              </div>
              <div class="text-xs text-gray-500 mt-1">
                <template v-if="j.status === 'sending'">
                  {{ $t('models.sent_to_head', { sent: fmtBytes(j.sent_bytes), total: fmtBytes(j.total_bytes) }) }}
                  <span v-if="j.total_bytes" class="text-gray-700">· {{ Math.min(100, ((j.sent_bytes || 0) / j.total_bytes) * 100).toFixed(0) }}%</span>
                </template>
                <template v-else-if="j.status === 'syncing'">
                  {{ $t('models.worker_transfer', { done: fmtBytes(taskProgressBytes(j)), total: fmtBytes(taskProgressTotal(j)) }) }}
                </template>
                <template v-else-if="j.total_bytes">
                  {{ $t('models.plane_download', { done: fmtBytes(j.downloaded_bytes), total: fmtBytes(j.total_bytes), pct: Math.min(100, ((j.downloaded_bytes || 0) / j.total_bytes) * 100).toFixed(0) }) }}
                </template>
                <template v-else>
                  {{ $t('models.plane_download_unknown', { done: fmtBytes(j.downloaded_bytes) }) }}
                </template>
              </div>
              <div v-if="ACTIVE_JOB_STATUSES.includes(j.status) && j._speed" class="text-[11px] text-gray-400 mt-1">
                {{ j.status === 'sending' ? $t('models.send_speed') : j.status === 'syncing' ? $t('models.transfer_speed') : $t('models.download_speed') }} {{ fmtSpeed(j._speed) }}
                <span v-if="j._eta">{{ $t('common.eta', { eta: j._eta }) }}</span>
              </div>
              <UProgress
                class="mt-1"
                :model-value="progressOf(j)"
                :color="j.status === 'failed' ? 'error' : j.status === 'completed' ? 'success' : 'primary'"
                size="sm"
              />
              <div v-if="j.sync_jobs && Object.keys(j.sync_jobs).length" class="space-y-1 mt-2">
                <div v-for="(worker, nodeId) in j.sync_jobs" :key="nodeId" class="text-[11px] text-gray-500">
                  <div class="flex justify-between gap-3">
                    <span>{{ nodeName(nodeId) }} · {{ statusLabel((worker as any).status) }}</span>
                    <span>{{ fmtBytes((worker as any).transferred_bytes || 0) }} / {{ fmtBytes((worker as any).total_bytes || j.total_bytes) }}</span>
                  </div>
                  <UProgress
                    :model-value="Math.min(100, ((worker as any).transferred_bytes || 0) / ((worker as any).total_bytes || j.total_bytes || 1) * 100)"
                    size="xs"
                  />
                  <div v-if="(worker as any).current_file" class="truncate text-gray-400 mt-0.5">{{ (worker as any).current_file }}</div>
                  <div v-if="(worker as any).error" class="text-red-500 mt-0.5">{{ (worker as any).error }}</div>
                </div>
              </div>
              <div v-if="j.error" class="text-[11px] text-red-500 mt-1">{{ j.error }}</div>
            </div>
          </UCard>

          <UCard>
            <template #header>
              <div class="flex items-center justify-between">
                <UButton color="neutral" variant="link" class="p-0 font-semibold" @click="toggleCompleted">
                  <span :class="showCompleted ? 'rotate-90' : ''" class="inline-block transition-transform text-xs">▶</span>
                  {{ $t('models.completed_title', { count: completedTotal }) }}
                </UButton>
                <div v-if="completedTotal" class="flex items-center gap-2">
                  <UButton size="xs" variant="outline" color="error" :loading="deletingCompleted" @click="removeAllCompleted">
                    {{ $t('models.delete_all') }}
                  </UButton>
                </div>
              </div>
            </template>
            <div v-if="showCompleted">
              <div v-if="!completedDownloads.length" class="text-sm text-gray-400 py-2 text-center">{{ $t('models.no_completed') }}</div>
              <div v-for="j in completedDownloads" :key="j.id" class="flex items-center gap-2 py-1.5 border-b border-gray-100 dark:border-gray-800/60 last:border-0">
                <span class="font-mono text-xs flex-1 min-w-0 break-all">{{ j.repo }}</span>
                <span class="text-xs text-gray-500 shrink-0">{{ fmtBytes(j.downloaded_bytes) }}</span>
                <UButton size="xs" variant="ghost" color="error" @click="removeCompleted(j)">{{ $t('common.delete') }}</UButton>
              </div>
              <div v-if="completedDownloads.length < completedTotal" class="flex justify-center mt-2">
                <UButton size="xs" variant="soft" :loading="loadingCompleted" @click="loadCompletedDownloads(false)">
                  {{ $t('models.load_more', { shown: completedDownloads.length, total: completedTotal }) }}
                </UButton>
              </div>
              <div v-else-if="completedDownloads.length" class="text-center text-xs text-gray-400 mt-1">
                {{ $t('models.all_shown', { count: completedTotal }) }}
              </div>
            </div>
            <div v-else class="text-xs text-gray-400">{{ $t('models.expand_history') }}</div>
          </UCard>
        </div>

        <UCard>
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">{{ $t('models.settings_title') }}</div>
              <UButton size="xs" color="primary" variant="soft" :loading="savingSettings" @click="saveSettings">{{ $t('common.save') }}</UButton>
            </div>
          </template>
          <div class="space-y-3">
            <UFormField :label="$t('models.endpoint_label')" :hint="$t('models.endpoint_hint')">
              <USelectMenu value-key="value"
                v-model="settings.endpoint"
                :items="[
                  { label: $t('models.hf_official'), value: 'https://huggingface.co' },
                  { label: $t('models.hf_mirror'), value: 'https://hf-mirror.com' },
                  { label: $t('models.custom'), value: '__custom__' },
                ]"
              />
              <UInput
                v-if="settings.endpoint === '__custom__'"
                v-model="settings.customEndpoint"
                class="mt-2"
                placeholder="https://your-mirror.example.com"
              />
            </UFormField>
            <UFormField :label="$t('models.token_label')" :hint="$t('models.token_hint')">
              <div class="flex gap-2">
                <UInput
                  v-model="settings.hfToken"
                  type="password"
                  class="flex-1"
                  :placeholder="settings.hasToken ? $t('models.token_configured') : $t('models.token_anonymous')"
                />
                <UButton v-if="settings.hasToken" size="sm" variant="outline" @click="clearToken">{{ $t('models.clear') }}</UButton>
              </div>
            </UFormField>
            <div class="grid grid-cols-2 gap-3">
              <UFormField :label="$t('models.connections_label')" :hint="$t('models.connections_hint')">
                <UInput v-model.number="settings.connections" type="number" min="1" max="32" />
              </UFormField>
              <UFormField :label="$t('models.chunk_label')" :hint="$t('models.chunk_hint')">
                <UInput v-model.number="settings.chunkSizeMb" type="number" min="1" max="64" />
              </UFormField>
            </div>
            <p class="text-[11px] text-gray-400">
              {{ $t('models.settings_note') }}
            </p>
          </div>
        </UCard>
      </div>
    </div>
    </template>
  </UDashboardPanel>
</template>
