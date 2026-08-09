<script setup lang="ts">
import { errorMsg } from '~/composables/useApi'
const { t } = useI18n()
const api = useApi()
const confirm = useConfirmDialog()
const toast = useToast()

const imageName = ref('')
const info = ref<any>(null)
const checking = ref(false)
const error = ref('')

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

const statusColor: Record<string, 'primary' | 'secondary' | 'success' | 'info' | 'warning' | 'error' | 'neutral'> = {
  pulling: 'info', sending: 'warning', syncing: 'warning', loading: 'warning',
  completed: 'success', failed: 'error', paused: 'neutral', cancelled: 'neutral',
}

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

const ACTIVE_TRANSFER_STATUSES = ['pulling', 'sending', 'syncing', 'loading']

async function pauseTransfer(x: any) {
  try {
    await api.post(`/images/transfers/${x.id}/pause`)
    toast.add({ title: t('images.paused_task', { id: x.id }), color: 'success' })
    await loadTransfers()
  } catch (e) {
    error.value = errorMsg(e)
  }
}

async function resumeTransfer(x: any) {
  try {
    await api.post(`/images/transfers/${x.id}/resume`)
    toast.add({ title: t('images.resumed_task', { id: x.id }), color: 'success' })
    await loadTransfers()
  } catch (e) {
    error.value = errorMsg(e)
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
    error.value = errorMsg(e)
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
    const res = await api.post('/images/transfer', body)
    toast.add({ title: res.head_node_id ? t('images.transfer_started', { id: res.id }) : t('images.pull_started', { id: res.id }), color: 'success' })
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
    toast.add({ title: t('images.settings_saved'), color: 'success' })
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
  const ok = await confirm.open({ title: t('images.delete_archive_title'), description: t('images.delete_archive_confirm', { image: a.image || a.file }) })
  if (!ok) return
  try {
    await api.del(`/images/local/${a.file}`)
    toast.add({ title: t('images.archive_deleted'), color: 'success' })
    await loadLocalArchives()
  } catch (e) {
    error.value = errorMsg(e)
  }
}

async function refreshLocalArchive(a: any) {
  refreshingArchive.value = a.file
  error.value = ''
  try {
    const res = await api.post('/images/transfer', { image: a.image, force: true })
    toast.add({ title: t('images.repull_started', { id: res.id, image: a.image }), color: 'success' })
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
  <UDashboardPanel id="images">
    <template #header>
      <UDashboardNavbar :toggle="false" :title="$t('images.title')" />
    </template>
    <template #body>
    <div>
      <UAlert v-if="error" :title="error" color="error" class="mb-4" />

      <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div class="lg:col-span-2 space-y-4">
          <UCard>
            <template #header><div class="font-semibold">{{ $t('images.pull_distribute') }}</div></template>
            <div class="flex gap-2">
              <UInput
                v-model="imageName"
                class="flex-1"
                :placeholder="$t('images.image_placeholder')"
                @keyup.enter="checkImage"
              />
              <UButton color="primary" :loading="checking" @click="checkImage">{{ $t('images.check') }}</UButton>
            </div>
            <div v-if="info" class="mt-3 space-y-3">
              <div class="text-xs text-gray-500">
                digest <span class="font-mono">{{ info.digest }}</span> ·
                {{ $t('images.info_size', { size: fmtBytes(info.size_bytes), layers: info.layers }) }}
                <UBadge :color="info.arch === 'arm64' || info.arch === 'aarch64' ? 'success' : 'warning'" variant="subtle" size="sm">
                  {{ info.arch || $t('images.arch_unknown') }}/{{ info.os || $t('images.arch_unknown') }}
                </UBadge>
                <span v-if="info.arch === 'arm64' || info.arch === 'aarch64'" class="text-gray-700">{{ $t('images.suitable') }}</span>
                <span v-else class="text-warning">{{ $t('images.needs_arm') }}</span>
              </div>
              <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                <UFormField :label="$t('images.receiving_node')">
                  <USelectMenu value-key="value"
                    v-model="headNodeId"
                    :items="nodes.map((n) => ({ label: `${n.name} (${n.ip})`, value: n.id }))"
                  />
                </UFormField>
                <UFormField :label="$t('images.roce_sync_nodes')">
                  <USelectMenu value-key="value"
                    v-model="workerIds"
                    multiple
                    :items="nodes.filter((n) => n.id !== headNodeId).map((n) => ({ label: n.name, value: n.id }))"
                  />
                </UFormField>
              </div>
              <div class="flex items-center justify-end gap-2">
                <UButton variant="outline" :loading="starting" @click="startTransfer(true)">
                  {{ $t('images.pull_only') }}
                </UButton>
                <UButton color="primary" :disabled="!headNodeId" :loading="starting" @click="startTransfer(false)">
                  {{ $t('images.pull_distribute_btn') }}
                </UButton>
              </div>
              <p v-if="!headNodeId" class="text-right text-[11px] text-gray-400">
                {{ $t('images.no_head_hint') }}
              </p>
            </div>
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
                <template v-else-if="t.size_bytes">
                  {{ $t('images.plane_pull', { done: fmtBytes(t.downloaded_bytes), total: fmtBytes(t.size_bytes), pct: Math.min(100, ((t.downloaded_bytes || 0) / t.size_bytes) * 100).toFixed(0) }) }}
                </template>
              </div>
              <div v-if="(t.status === 'pulling' || t.status === 'sending') && t._speed" class="text-[11px] text-gray-400 mt-1">
                {{ t.status === 'sending' ? $t('images.send_speed') : $t('images.pull_speed') }} {{ fmtSpeed(t._speed) }}
                <span v-if="t._eta">{{ $t('common.eta', { eta: t._eta }) }}</span>
              </div>
              <UProgress
                class="mt-1"
                :model-value="progressOf(t)"
                :color="t.status === 'failed' ? 'error' : t.status === 'completed' ? 'success' : 'primary'"
                size="sm"
              />
              <div v-if="t.sync_jobs && Object.keys(t.sync_jobs).length" class="text-[11px] text-gray-400 mt-1">
                {{ $t('images.roce_sync') }}: {{ Object.entries(t.sync_jobs).map(([k, v]) => `#${k} ${(v as any).status}`).join(' · ') }}
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
                <button class="flex items-center gap-1 font-semibold hover:text-primary" @click="toggleCompleted">
                  <span :class="showCompleted ? 'rotate-90' : ''" class="inline-block transition-transform text-xs">▶</span>
                  {{ $t('images.completed_title', { count: completedTotal }) }}
                </button>
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
    </div>
    </template>
  </UDashboardPanel>
</template>
