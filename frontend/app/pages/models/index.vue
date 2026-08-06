<script setup lang="ts">
import { errorMsg } from '~/composables/useApi'
const api = useApi()
const confirm = useConfirmDialog()

const query = ref('')
const directRepo = ref('')
const results = ref<any[]>([])
const searching = ref(false)
const error = ref('')
const notice = ref('')

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
const downloadMode = ref<'distribute' | 'local'>('distribute')

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
  error.value = ''
  try {
    const body: Record<string, unknown> = {
      endpoint: settings.value.endpoint === '__custom__' ? settings.value.customEndpoint : settings.value.endpoint,
      connections: settings.value.connections,
      chunk_size_mb: settings.value.chunkSizeMb,
    }
    if (settings.value.hfToken) body.hf_token = settings.value.hfToken
    await api.put('/models/settings', body)
    notice.value = '下载设置已保存'
    await loadSettings()
  } catch (e) {
    error.value = String(e)
  } finally {
    savingSettings.value = false
  }
}

async function clearToken() {
  await api.put('/models/settings', { hf_token: null })
  notice.value = '已清除 HF Token'
  await loadSettings()
}

const fmt = (v: number) =>
  v >= 1e12 ? `${(v / 1e12).toFixed(1)} TB` : v >= 1e9 ? `${(v / 1e9).toFixed(1)} GB` : v >= 1e6 ? `${(v / 1e6).toFixed(0)} MB` : `${v || 0} B`

async function search() {
  if (!query.value.trim()) return
  searching.value = true
  error.value = ''
  try {
    results.value = await api.get('/models/search', { q: query.value.trim(), limit: 12 })
  } catch (e) {
    error.value = String(e)
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
    error.value = String(e)
  }
}

async function directDownload() {
  const repo = directRepo.value.trim()
  if (!repo) {
    error.value = '请输入模型名称'
    return
  }
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) {
    error.value = '模型名称格式应为 owner/name（如 deepseek-ai/DeepSeek-V4-Flash-DSpark）'
    return
  }
  error.value = ''
  selectedModel.value = null
  try {
    modelInfo.value = await api.get(`/models/${repo}/info`)
    selectedModel.value = repo
    notice.value = `模型 ${repo} 已加载，可选择「仅下载」或「下载并分发」`
  } catch (e) {
    error.value = errorMsg(e)
  }
}

async function removeDownload(j: any) {
  const ok = await confirm.open({
    title: '删除任务',
    description: `确认删除失败任务 #${j.id}（${j.repo}）？将同时清理该模型的下载残留文件（已完成的 blobs 保留，可继续分发）`,
  })
  if (!ok) return
  try {
    await api.del(`/models/downloads/${j.id}?cleanup=1`)
    notice.value = `已删除任务 #${j.id} 并清理残留`
    await loadDownloads()
  } catch (e) {
    error.value = errorMsg(e)
  }
}

const ACTIVE_JOB_STATUSES = ['downloading', 'sending', 'syncing']

async function pauseDownload(j: any) {
  try {
    await api.post(`/models/downloads/${j.id}/pause`)
    notice.value = `任务 #${j.id} 已暂停（分片保留，可继续）`
    await loadDownloads()
  } catch (e) {
    error.value = errorMsg(e)
  }
}

async function resumeDownload(j: any) {
  try {
    await api.post(`/models/downloads/${j.id}/resume`)
    notice.value = `任务 #${j.id} 已继续（分片续传）`
    await loadDownloads()
  } catch (e) {
    error.value = errorMsg(e)
  }
}

async function cancelDownload(j: any) {
  const ok = await confirm.open({
    title: '取消任务',
    description: `确认取消任务 #${j.id}（${j.repo}）？已下载分片保留，之后可重试续传`,
  })
  if (!ok) return
  try {
    await api.post(`/models/downloads/${j.id}/cancel`)
    notice.value = `任务 #${j.id} 已取消`
    await loadDownloads()
  } catch (e) {
    error.value = errorMsg(e)
  }
}

async function loadNodes() {
  nodes.value = await api.get('/nodes')
  if (!headNodeId.value && nodes.value.length) headNodeId.value = nodes.value[0].id
}

// 任务速度 / 预计完成时间（基于 5s 轮询差值在前端计算）
const speedSnapshot = ref<Record<number, { bytes: number; ts: number }>>({})

function taskProgressBytes(j: any): number {
  return j.status === 'sending' ? (j.sent_bytes || 0) : (j.downloaded_bytes || 0)
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

const fmtSpeed = (b: number) =>
  b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB/s`
    : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB/s`
    : b >= 1e3 ? `${(b / 1e3).toFixed(0)} KB/s`
    : `${b.toFixed(0)} B/s`

const fmtEta = (sec: number) => {
  if (sec >= 3600) return `${(sec / 3600).toFixed(1)} 小时`
  if (sec >= 60) return `${Math.round(sec / 60)} 分钟`
  return `${Math.round(sec)} 秒`
}

function computeTaskEta(j: any, speed: number | null): string | null {
  if (!speed) return null
  const remaining = (j.total_bytes || 0) - taskProgressBytes(j)
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
    title: '删除任务',
    description: `确认删除已完成任务 #${j.id}（${j.repo}）？将同时清理该模型的下载残留`,
  })
  if (!ok) return
  try {
    await api.del(`/models/downloads/${j.id}?cleanup=1`)
    notice.value = `已删除任务 #${j.id}`
    completedDownloads.value = completedDownloads.value.filter((x) => x.id !== j.id)
    await loadCompletedCount()
  } catch (e) {
    error.value = errorMsg(e)
  }
}

async function removeAllCompleted() {
  if (!completedTotal.value) return
  const ok = await confirm.open({
    title: '批量删除',
    description: `确认删除全部 ${completedTotal.value} 条已完成任务？将同时清理各模型下载残留（已完成的 blobs 保留）`,
  })
  if (!ok) return
  deletingCompleted.value = true
  try {
    const r = await api.del('/models/downloads/all-completed?cleanup=1')
    notice.value = `已删除 ${r.deleted} 条任务，清理 ${r.cleaned_files} 个残留文件`
    completedDownloads.value = []
    completedOffset.value = 0
    await loadCompletedCount()
    await loadLocalModels()
  } catch (e) {
    error.value = errorMsg(e)
  } finally {
    deletingCompleted.value = false
  }
}

async function retryDownload(j: any) {
  try {
    const job = await api.post(`/models/downloads/${j.id}/retry`)
    notice.value = `已重新发起任务 #${job.id}（${j.repo}），断点续传`
    await loadDownloads()
  } catch (e) {
    error.value = errorMsg(e)
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
    error.value = '模型正在下载中，请等待完成后再删除'
    return
  }
  const label = m.status === 'complete' ? '已下载的模型缓存' : '未完成的下载残留'
  const ok = await confirm.open({ title: '删除模型', description: `确认删除${label}「${m.repo}」？` })
  if (!ok) return
  try {
    await api.del(`/models/local/${m.repo}`)
    notice.value = `已删除 ${m.repo}`
    await loadLocalModels()
  } catch (e) {
    error.value = errorMsg(e)
  }
}

// 仅分发（与下载解耦）：本地缓存 -> head -> RoCE 同步 worker
const distributingRepo = ref<string | null>(null)
const distHeadId = ref<number | null>(null)
const distWorkerIds = ref<number[]>([])
const distributing = ref(false)

function toggleDistribute(repo: string) {
  distributingRepo.value = distributingRepo.value === repo ? null : repo
  distHeadId.value = nodes.value[0]?.id ?? null
  distWorkerIds.value = []
}

async function doDistribute(repo: string) {
  distributing.value = true
  error.value = ''
  try {
    const job = await api.post('/models/distribute', {
      repo,
      head_node_id: distHeadId.value,
      sync_node_ids: distWorkerIds.value,
    })
    notice.value = `分发任务 #${job.id} 已启动：发送 head → RoCE 同步 worker`
    distributingRepo.value = null
    await loadDownloads()
  } catch (e) {
    error.value = errorMsg(e)
  } finally {
    distributing.value = false
  }
}

async function startDownload() {
  starting.value = true
  error.value = ''
  try {
    const body: Record<string, unknown> = { repo: selectedModel.value }
    if (downloadMode.value === 'distribute') {
      body.head_node_id = headNodeId.value
      body.sync_node_ids = workerIds.value
    }
    const job = await api.post('/models/download', body)
    notice.value = downloadMode.value === 'distribute'
      ? `传输任务 #${job.id} 已启动：管理平面下载 → 发送 head → RoCE 同步`
      : `下载任务 #${job.id} 已启动：仅下载到管理平面，之后可随时分发`
    await loadDownloads()
  } catch (e) {
    error.value = errorMsg(e)
  } finally {
    starting.value = false
  }
}

const statusColor: Record<string, string> = {
  downloading: 'info', sending: 'warning', syncing: 'warning', completed: 'success',
  failed: 'error', paused: 'neutral', cancelled: 'neutral',
}
const statusLabel = (s: string) =>
  ({ downloading: '管理平面下载中', sending: '发送到 head', syncing: 'RoCE 同步中', completed: '完成', failed: '失败', paused: '已暂停', cancelled: '已取消' })[s] || s

// 模型缓存多态状态：已下载 / 下载中 / 下载失败 / 未完成
const modelStatusLabel: Record<string, string> = {
  complete: '已下载', downloading: '下载中', failed: '下载失败', partial: '未完成',
}
const modelStatusColor: Record<string, string> = {
  complete: 'success', downloading: 'info', failed: 'error', partial: 'warning',
}

const progressOf = (j: any) => {
  const total = j.total_bytes || 1
  if (j.status === 'sending') return Math.min(100, ((j.sent_bytes || 0) / total) * 100)
  return Math.min(100, ((j.downloaded_bytes || 0) / total) * 100)
}

// 实时传输进度（WS 推送：agent 拉取进度 -> sent_bytes 实时更新）
const rt = useRealtime()

function onTransferProgress(msg: any) {
  if (msg.kind !== 'model') return
  const j = downloads.value.find((x: any) => x.id === msg.job_id)
  if (j) j.sent_bytes = msg.sent_bytes
}

onMounted(() => {
  loadNodes()
  loadDownloads()
  loadCompletedCount()
  loadLocalModels()
  loadSettings()
  rt.on('transfer_progress', onTransferProgress)
  const t = setInterval(() => {
    loadDownloads()
    loadLocalModels()
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
      <h1 class="text-xl font-bold">模型管理</h1>
    </div>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />
    <UAlert v-if="notice" :title="notice" color="success" class="mb-4" />

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <div class="lg:col-span-2 space-y-4">
        <UCard>
          <template #header><div class="font-semibold">搜索 Hugging Face 模型</div></template>
          <div class="flex gap-2">
            <UInput v-model="query" class="flex-1" placeholder="如 deepseek、qwen、llama…" @keyup.enter="search" />
            <UButton color="primary" :loading="searching" @click="search">搜索</UButton>
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
                <div class="text-xs text-gray-500">下载 {{ fmtNumber(m.downloads || 0) }} · ♥ {{ fmtNumber(m.likes || 0) }}</div>
              </div>
              <UButton size="xs" variant="ghost" @click.stop="pickModel(m.id)">选择</UButton>
            </div>
          </div>
        </UCard>

        <UCard>
          <template #header><div class="font-semibold">直接下载</div></template>
          <div class="flex gap-2">
            <UInput
              v-model="directRepo"
              class="flex-1"
              placeholder="输入模型名称，如 deepseek-ai/DeepSeek-V4-Flash-DSpark"
              @keyup.enter="directDownload"
            />
            <UButton variant="soft" @click="directDownload">直接下载</UButton>
          </div>
          <p class="text-[11px] text-gray-400 mt-2">已知模型名称时无需搜索，输入后校验存在性并直接进入下载配置。</p>
        </UCard>

        <UCard v-if="selectedModel">
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">{{ selectedModel }}</div>
              <div class="text-xs text-gray-500" v-if="modelInfo">总大小 {{ fmt(modelInfo.total_size) }} · {{ modelInfo.siblings?.length || 0 }} 个文件</div>
            </div>
          </template>
          <UAlert color="info" variant="subtle" class="mb-3" title="下载在管理平面后台完成（仅一份）">
            模型由管理平面从 Hugging Face 下载并统一管理，不占用节点磁盘、不重复消耗互联网带宽；
            完成后经管理网发送到 head，再由 head 经 RoCE 高速计算网同步到各 worker。
          </UAlert>
          <UFormField label="分发方式">
            <USelect
              v-model="downloadMode"
              :items="[
                { label: '下载并分发到节点', value: 'distribute' },
                { label: '仅下载到管理平面', value: 'local' },
              ]"
            />
          </UFormField>
          <div v-if="downloadMode === 'distribute'" class="grid grid-cols-1 md:grid-cols-2 gap-3">
            <UFormField label="模型接收节点（head）">
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
          <div class="flex justify-end mt-3">
            <UButton
              color="primary"
              :loading="starting"
              :disabled="downloadMode === 'distribute' && !headNodeId"
              @click="startDownload"
            >
              {{ downloadMode === 'distribute' ? '下载并分发' : '开始下载' }}
            </UButton>
          </div>
        </UCard>

        <UCard>
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">管理平面模型缓存（{{ localModels.length }}）</div>
              <UButton size="xs" variant="ghost" @click="loadLocalModels">刷新</UButton>
            </div>
          </template>
          <div v-if="!localModels.length" class="text-sm text-gray-400 py-2 text-center">暂无本地模型，下载后在此统一管理</div>
          <div v-for="m in localModels" :key="m.repo" class="py-1.5 border-b border-gray-100 dark:border-gray-800/60 last:border-0">
            <div class="flex items-center justify-between">
              <div class="min-w-0">
                <div class="font-mono text-xs">{{ m.repo }}</div>
                <div class="text-xs text-gray-500 mt-0.5 flex items-center gap-1.5">
                  <UBadge :color="modelStatusColor[m.status] || 'neutral'" variant="subtle" size="sm">
                    {{ modelStatusLabel[m.status] || m.status }}
                  </UBadge>
                  <span>{{ fmt(m.size_bytes) }}</span>
                </div>
              </div>
              <div class="flex gap-1 shrink-0">
                <UButton v-if="m.status === 'complete'" size="xs" variant="ghost" @click="toggleDistribute(m.repo)">分发</UButton>
                <UButton
                  size="xs"
                  variant="ghost"
                  color="error"
                  :disabled="m.status === 'downloading'"
                  @click="removeLocalModel(m)"
                >
                  {{ m.status === 'downloading' ? '下载中' : '删除' }}
                </UButton>
              </div>
            </div>
            <div v-if="distributingRepo === m.repo" class="mt-2 p-2 rounded-md bg-gray-100/70 dark:bg-gray-800/60 space-y-2">
              <USelect
                v-model="distHeadId"
                :items="nodes.map((n) => ({ label: `${n.name} (${n.ip})`, value: n.id }))"
                placeholder="接收节点（head）"
              />
              <USelect
                v-model="distWorkerIds"
                multiple
                :items="nodes.filter((n) => n.id !== distHeadId).map((n) => ({ label: n.name, value: n.id }))"
                placeholder="RoCE 同步节点（worker，可多选）"
              />
              <div class="flex justify-end gap-2">
                <UButton size="xs" variant="outline" @click="distributingRepo = null">取消</UButton>
                <UButton size="xs" color="primary" :loading="distributing" :disabled="!distHeadId" @click="doDistribute(m.repo)">
                  确认分发
                </UButton>
              </div>
            </div>
          </div>
        </UCard>

        <UCard>
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">下载/分发任务（{{ downloads.length }}）</div>
              <UButton size="xs" variant="ghost" @click="loadDownloads">刷新</UButton>
            </div>
          </template>
          <div v-if="!downloads.length" class="text-sm text-gray-400 py-4 text-center">暂无进行中或失败的任务</div>
          <div v-for="j in downloads" :key="j.id" class="mb-3 p-2 rounded-md border border-gray-200 dark:border-gray-700">
            <div class="flex items-center justify-between text-sm">
              <span class="font-mono text-xs break-all leading-5">{{ j.repo }}</span>
              <div class="flex items-center gap-1 shrink-0">
                <UBadge :color="statusColor[j.status] || 'neutral'" variant="subtle">{{ statusLabel(j.status) }}</UBadge>
                <UButton v-if="ACTIVE_JOB_STATUSES.includes(j.status)" size="xs" variant="ghost" @click="pauseDownload(j)">暂停</UButton>
                <UButton v-if="j.status === 'paused'" size="xs" variant="ghost" @click="resumeDownload(j)">继续</UButton>
                <UButton v-if="ACTIVE_JOB_STATUSES.includes(j.status) || j.status === 'paused'" size="xs" variant="ghost" color="error" @click="cancelDownload(j)">取消</UButton>
                <UButton v-if="j.status === 'failed'" size="xs" variant="ghost" @click="retryDownload(j)">重试</UButton>
                <UButton v-if="j.status === 'failed' || j.status === 'cancelled'" size="xs" variant="ghost" color="error" @click="removeDownload(j)">
                  删除
                </UButton>
              </div>
            </div>
            <div class="text-xs text-gray-500 mt-1">
              <template v-if="j.status === 'sending'">
                已发送到 head {{ fmt(j.sent_bytes) }} / {{ fmt(j.total_bytes) }}
                <span v-if="j.total_bytes" class="text-gray-700">· {{ Math.min(100, ((j.sent_bytes || 0) / j.total_bytes) * 100).toFixed(0) }}%</span>
              </template>
              <template v-else-if="j.total_bytes">
                管理平面下载 {{ fmt(j.downloaded_bytes) }} / {{ fmt(j.total_bytes) }}
                · {{ Math.min(100, ((j.downloaded_bytes || 0) / j.total_bytes) * 100).toFixed(0) }}%
              </template>
              <template v-else>
                管理平面下载 {{ fmt(j.downloaded_bytes) }}（总大小未知，下载中）
              </template>
            </div>
            <div v-if="(j.status === 'downloading' || j.status === 'sending') && j._speed" class="text-[11px] text-gray-400 mt-1">
              {{ j.status === 'sending' ? '发送速度' : '下载速度' }} {{ fmtSpeed(j._speed) }}
              <span v-if="j._eta">· 预计剩余 {{ j._eta }}</span>
            </div>
            <UProgress
              class="mt-1"
              :model-value="progressOf(j)"
              :color="j.status === 'failed' ? 'error' : j.status === 'completed' ? 'success' : 'primary'"
              size="sm"
            />
            <div v-if="j.sync_jobs && Object.keys(j.sync_jobs).length" class="text-[11px] text-gray-400 mt-1">
              RoCE 同步: {{ Object.entries(j.sync_jobs).map(([k, v]) => `#${k} ${(v as any).status}`).join(' · ') }}
            </div>
            <div v-if="j.error" class="text-[11px] text-red-500 mt-1">{{ j.error }}</div>
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
            <div v-if="!completedDownloads.length" class="text-sm text-gray-400 py-2 text-center">暂无已完成任务</div>
            <div v-for="j in completedDownloads" :key="j.id" class="flex items-center gap-2 py-1.5 border-b border-gray-100 dark:border-gray-800/60 last:border-0">
              <span class="font-mono text-xs flex-1 min-w-0 break-all">{{ j.repo }}</span>
              <span class="text-xs text-gray-500 shrink-0">{{ fmt(j.downloaded_bytes) }}</span>
              <UButton size="xs" variant="ghost" color="error" @click="removeCompleted(j)">删除</UButton>
            </div>
            <div v-if="completedDownloads.length < completedTotal" class="flex justify-center mt-2">
              <UButton size="xs" variant="soft" :loading="loadingCompleted" @click="loadCompletedDownloads(false)">
                加载更多（已显示 {{ completedDownloads.length }} / {{ completedTotal }}）
              </UButton>
            </div>
            <div v-else-if="completedDownloads.length" class="text-center text-xs text-gray-400 mt-1">
              已全部显示（{{ completedTotal }} 条）
            </div>
          </div>
          <div v-else class="text-xs text-gray-400">点击展开查看历史任务（分页加载）</div>
        </UCard>
      </div>

      <UCard>
        <template #header>
          <div class="flex items-center justify-between">
            <div class="font-semibold">下载设置</div>
            <UButton size="xs" color="primary" variant="soft" :loading="savingSettings" @click="saveSettings">保存</UButton>
          </div>
        </template>
        <div class="space-y-3">
          <UFormField label="下载源 (endpoint)" hint="官方源或镜像源，私有部署可自定义">
            <USelect
              v-model="settings.endpoint"
              :items="[
                { label: 'huggingface.co（官方）', value: 'https://huggingface.co' },
                { label: 'hf-mirror.com（国内镜像）', value: 'https://hf-mirror.com' },
                { label: '自定义', value: '__custom__' },
              ]"
            />
            <UInput
              v-if="settings.endpoint === '__custom__'"
              v-model="settings.customEndpoint"
              class="mt-2"
              placeholder="https://your-mirror.example.com"
            />
          </UFormField>
          <UFormField label="HF Token" hint="私有 / gated 仓库需要；留空表示不修改">
            <div class="flex gap-2">
              <UInput
                v-model="settings.hfToken"
                type="password"
                class="flex-1"
                :placeholder="settings.hasToken ? '已配置（留空保持不变）' : '匿名下载（公开仓库）'"
              />
              <UButton v-if="settings.hasToken" size="sm" variant="outline" @click="clearToken">清除</UButton>
            </div>
          </UFormField>
          <div class="grid grid-cols-2 gap-3">
            <UFormField label="单文件连接数" hint="1-32">
              <UInput v-model.number="settings.connections" type="number" min="1" max="32" />
            </UFormField>
            <UFormField label="分片大小 (MB)" hint="1-64">
              <UInput v-model.number="settings.chunkSizeMb" type="number" min="1" max="64" />
            </UFormField>
          </div>
          <p class="text-[11px] text-gray-400">
            多连接 Range 分块下载（参考 bodaay/HuggingFaceModelDownloader），支持断点续传与 sha256 校验；
            下载完成后逐文件校验通过才向节点分发。
          </p>
        </div>
      </UCard>
    </div>
  </div>
</template>
