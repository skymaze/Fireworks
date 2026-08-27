<script setup lang="ts">
import {
  fetchInferenceMetrics,
  emptyInferenceMetrics,
} from '../../composables/useInferenceStats'

const { t } = useI18n()
const NO_LOGS = computed(() => t('tasks.no_logs'))
const route = useRoute()
const api = useApi()
const rt = useRealtime()
const toast = useToast()
const taskId = Number(route.params.id)

const task = ref<any>(null)
const recipes = ref<any[]>([])
const clusters = ref<any[]>([])
const nodes = ref<any[]>([])
const logsNodeId = ref<number | null>(null)
const logs = ref('')
const acting = ref(false)
let logSubscribed = false
let subscribedNodeId: number | null = null
const LOG_REPLAY_TAIL = 1000
const LOG_BUFFER_MAX = 2_000_000

const statusColor: Record<string, 'primary' | 'secondary' | 'success' | 'info' | 'warning' | 'error' | 'neutral'> = {
  running: 'success', paused: 'warning', published: 'info', stopped: 'neutral', error: 'error',
}

async function load() {
  try {
    ;[task.value, recipes.value, clusters.value, nodes.value] = await Promise.all([
      api.get(`/tasks/${taskId}`),
      api.get('/recipes'),
      api.get('/clusters'),
      api.get('/nodes'),
    ])
    if (!logsNodeId.value && task.value.nodes?.length) {
      logsNodeId.value = task.value.nodes[0].node_id
    }
    loadBenchmarks()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

// 上一段日志是否为「原地刷新行」（update）：是则下一次 update 覆盖它，否则新起一行
let lastLogLineUpdate = false

// ---------- 实时通道：日志流 + 容器/任务状态 ----------

function currentContainerName(): string | null {
  const tn = task.value?.nodes?.find((x: any) => x.node_id === logsNodeId.value)
  return tn?.container_name || null
}

function subscribeLogs(reset = true) {
  if (!logsNodeId.value || logSubscribed) return
  if (reset) {
    logs.value = NO_LOGS.value
    lastLogLineUpdate = false
  }
  rt.send({
    type: 'log_subscribe', task_id: taskId,
    node_id: logsNodeId.value, tail: LOG_REPLAY_TAIL,
  })
  subscribedNodeId = logsNodeId.value
  logSubscribed = true
}

function unsubscribeLogs() {
  if (!logSubscribed || !subscribedNodeId) return
  rt.send({ type: 'log_unsubscribe', task_id: taskId, node_id: subscribedNodeId })
  logSubscribed = false
  subscribedNodeId = null
}

function onLog(msg: any) {
  if (msg.container && msg.container === currentContainerName()) {
    // 同一条流先回放历史、再实时追加（保留尾部换行）。
    if (logs.value === NO_LOGS.value) {
      logs.value = ''
      lastLogLineUpdate = false
    }
    const line = msg.line ?? ''
    if (msg.update) {
      // 进度类本行刷新：连续 update 覆盖上一行；首个 update 新起一行
      if (lastLogLineUpdate) {
        const i = logs.value.lastIndexOf('\n')
        logs.value = logs.value.slice(0, i + 1) + line
      } else {
        if (logs.value && !logs.value.endsWith('\n')) logs.value += '\n'
        logs.value += line
      }
      lastLogLineUpdate = true
    } else {
      if (logs.value && !logs.value.endsWith('\n')) logs.value += '\n'
      logs.value += line + '\n'
      lastLogLineUpdate = false
    }
    if (logs.value.length > LOG_BUFFER_MAX) {
      const tail = logs.value.slice(-LOG_BUFFER_MAX)
      const firstLine = tail.indexOf('\n')
      logs.value = firstLine >= 0 ? tail.slice(firstLine + 1) : tail
    }
    scrollLogToBottom() // 推送后自动滚动到最新一行
  }
}

function onLogReset(msg: any) {
  if (msg.container && msg.container === currentContainerName()) {
    logs.value = NO_LOGS.value
    lastLogLineUpdate = false
  }
}

function onLogEnd(msg: any) {
  if (msg.container && msg.container === currentContainerName()) {
    logSubscribed = false // 流已结束（容器退出），停止退订命令避免空发
    subscribedNodeId = null
  }
}

function onContainerStatus(msg: any) {
  if (msg.task_id !== taskId || !task.value?.nodes) return
  const tn = task.value.nodes.find((x: any) => x.node_id === msg.node_id)
  if (tn) tn.container_status = msg.status
}

function onTaskStatus(msg: any) {
  if (msg.task_id === taskId && task.value) task.value.status = msg.status
}

function onTaskDeleted(msg: any) {
  // 任务被其他页面/窗口删除：跳回列表，避免停留陈旧详情
  if (msg.task_id === taskId) navigateTo('/tasks')
}

// ---------- 推理性能（服务端完整差分 + 时间桶聚合 → 吞吐 / 请求 / 延迟图） ----------

const inferenceMetrics = ref(emptyInferenceMetrics())
const inferenceWindow = ref(3600)
let inferenceRequestInFlight = false
let inferenceReloadPending = false
let lastInferenceLoadAt = 0

// 后端完整差分窗口源数据；摘要不降采样，图表按 1h/24h 分辨率聚合。
async function loadInferenceMetrics(background = false) {
  if (!hasInferenceEndpoint.value) {
    inferenceMetrics.value = emptyInferenceMetrics()
    return
  }
  if (inferenceRequestInFlight) {
    inferenceReloadPending = true
    return
  }
  inferenceRequestInFlight = true
  const requestedWindow = inferenceWindow.value
  try {
    const to = Date.now() / 1000
    inferenceMetrics.value = await fetchInferenceMetrics(api, {
      from: to - requestedWindow,
      to,
      taskId,
      maxPoints: requestedWindow === 86400 ? 288 : 360,
    })
    lastInferenceLoadAt = Date.now()
  } catch (e) {
    if (!background) toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    inferenceRequestInFlight = false
    if (inferenceReloadPending || inferenceWindow.value !== requestedWindow) {
      inferenceReloadPending = false
      loadInferenceMetrics(true)
    }
  }
}

watch(inferenceWindow, () => loadInferenceMetrics())

// 与后端 service_endpoint 判定一致：仅推理类任务（head + VLLM_PORT）展示面板
const hasInferenceEndpoint = computed(() =>
  Object.values(task.value?.rendered?.nodes || {}).some((payload: any) =>
    payload?.role === 'head' && payload?.env?.VLLM_PORT))

const inferencePoints = computed(() => inferenceMetrics.value.points)
const inferenceSummary = computed(() => inferenceMetrics.value.summary)

// Token 吞吐图：所有系列统一为 tok/s；区间 token 数量仅用于窗口合计。
const inferenceTokOption = computed(() => {
  const ts = inferencePoints.value.map((p) => fmtTime(p.ts))
  return {
    tooltip: { trigger: 'axis' },
    legend: {
      data: [t('tasks.inference_tok'), t('tasks.inference_prompt_rate')],
      top: 0,
    },
    grid: { left: 52, right: 48, top: 30, bottom: 24 },
    xAxis: { type: 'category', data: ts },
    yAxis: { type: 'value', name: 'tok/s', min: 0, scale: true },
    series: [
      { name: t('tasks.inference_tok'), type: 'bar', barMaxWidth: 20, itemStyle: { borderRadius: [3, 3, 0, 0] }, data: inferencePoints.value.map((p) => p.tokens_per_sec), yAxisIndex: 0 },
      { name: t('tasks.inference_prompt_rate'), type: 'bar', barMaxWidth: 20, itemStyle: { borderRadius: [3, 3, 0, 0] }, data: inferencePoints.value.map((p) => p.prompt_tokens_per_sec), yAxisIndex: 0 },
    ],
  }
})

const inferenceRequestOption = computed(() => {
  const ts = inferencePoints.value.map((p) => fmtTime(p.ts))
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: [t('tasks.inference_requests_chart')], top: 0 },
    grid: { left: 52, right: 24, top: 30, bottom: 24 },
    xAxis: { type: 'category', data: ts },
    yAxis: { type: 'value', name: t('tasks.inference_requests'), min: 0, minInterval: 1 },
    series: [{
      name: t('tasks.inference_requests_chart'),
      type: 'bar',
      barMaxWidth: 28,
      itemStyle: { borderRadius: [3, 3, 0, 0] },
      data: inferencePoints.value.map((p) => p.requests),
    }],
  }
})

const inferenceLatOption = computed(() => {
  const ts = inferencePoints.value.map((p) => fmtTime(p.ts))
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: [t('tasks.inference_ttft'), t('tasks.inference_e2e'), t('tasks.inference_kv')], top: 0 },
    grid: { left: 40, right: 48, top: 30, bottom: 24 },
    xAxis: { type: 'category', data: ts },
    yAxis: [
      { type: 'value', name: 'ms', scale: true },
      { type: 'value', name: '%', max: 100, splitLine: { show: false } },
    ],
    series: [
      { name: t('tasks.inference_ttft'), type: 'bar', barMaxWidth: 24, itemStyle: { borderRadius: [3, 3, 0, 0] }, data: inferencePoints.value.map((p) => p.ttft_ms), yAxisIndex: 0 },
      { name: t('tasks.inference_e2e'), type: 'bar', barMaxWidth: 24, itemStyle: { borderRadius: [3, 3, 0, 0] }, data: inferencePoints.value.map((p) => p.e2e_ms), yAxisIndex: 0 },
      { name: t('tasks.inference_kv'), type: 'line', showSymbol: false, connectNulls: false, data: inferencePoints.value.map((p) => p.kv_cache_percent), yAxisIndex: 1 },
    ],
  }
})

// ---------- 基准测试（并发 decode 吞吐压测 + 分布直方图） ----------

const benchmarks = ref<any[]>([])
const benchmarkSel = ref<number | null>(null)
const runningBenchmark = ref(false)
const benchForm = reactive({ concurrency: 8, num_requests: 32, max_tokens: 64 })

const selectedBenchmark = computed(() =>
  benchmarks.value.find((b) => b.ts === benchmarkSel.value) || benchmarks.value[0] || null)

const benchmarkResult = computed(() => selectedBenchmark.value?.result || null)

async function loadBenchmarks() {
  try {
    benchmarks.value = await api.get(`/tasks/${taskId}/benchmarks`, { limit: 5 })
    if (benchmarkSel.value == null && benchmarks.value.length) {
      benchmarkSel.value = benchmarks.value[0].ts
    }
  } catch (e) {
    // 可选功能，失败不影响主页面
  }
}

async function runBenchmark() {
  runningBenchmark.value = true
  try {
    await api.post(`/tasks/${taskId}/benchmark`, {
      concurrency: benchForm.concurrency,
      num_requests: benchForm.num_requests,
      max_tokens: benchForm.max_tokens,
    })
    benchmarkSel.value = null
    await loadBenchmarks()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    runningBenchmark.value = false
  }
}

function onBenchmarkResult(msg: any) {
  // 其他窗口/页面也刷新历史（与本窗口 runBenchmark 后的 loadBenchmarks 去重）
  if (msg.task_id === taskId) loadBenchmarks()
}

// per-request 时延 -> 等宽直方图（BarChart）
function histogram(values: number[], bins = 8): { label: string; count: number }[] {
  if (!values.length) return []
  const hi = Math.max(...values)
  if (hi <= 0) return []
  const width = hi / bins
  const counts = new Array(bins).fill(0)
  for (const v of values) {
    const idx = Math.min(bins - 1, Math.floor(v / width))
    counts[idx]++
  }
  return counts.map((c, i) => ({
    label: `${(i * width).toFixed(0)}-${((i + 1) * width).toFixed(0)}ms`,
    count: c,
  }))
}

function histOption(values: number[], yName: string) {
  const h = histogram(values)
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 16, top: 24, bottom: 28 },
    xAxis: { type: 'category', data: h.map((b) => b.label) },
    yAxis: { type: 'value', name: yName, minInterval: 1 },
    series: [{ type: 'bar', data: h.map((b) => b.count), itemStyle: { borderRadius: [3, 3, 0, 0] } }],
  }
}

const ttftHistOption = computed(() =>
  histOption((benchmarkResult.value?.per_request || []).map((p: any) => p.ttft_ms ?? 0), 'req'))
const e2eHistOption = computed(() =>
  histOption((benchmarkResult.value?.per_request || []).map((p: any) => p.e2e_ms ?? 0), 'req'))

async function act(action: string, deleteModel = false) {
  acting.value = true
  try {
    await api.post(`/tasks/${taskId}/action`, { action, delete_model: deleteModel })
    if (action === 'delete') {
      // 删除成功：任务已不存在，跳回列表（避免详情页 404 卡在旧状态）
      await navigateTo('/tasks')
      return
    }
    await load()
  } catch (e) {
    if (action === 'delete') {
      // 并发删除：任务已被其他请求删除也属成功
      await navigateTo('/tasks')
      return
    }
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    acting.value = false
  }
}

// 终止/删除确认（可选同时删除节点模型）
const showActionModal = ref(false)
const pendingAction = ref('')
const deleteModel = ref(false)

function openAction(action: string) {
  pendingAction.value = action
  deleteModel.value = false
  showActionModal.value = true
}

async function confirmAction() {
  showActionModal.value = false
  await act(pendingAction.value, deleteModel.value)
}

const recipeName = computed(() => recipes.value.find((r) => r.id === task.value?.recipe_id)?.name || '—')
const clusterName = computed(() => clusters.value.find((c) => c.id === task.value?.cluster_id)?.name || '—')
const nodeName = (id: number) => nodes.value.find((n) => n.id === id)?.name || `#${id}`

// 日志区自动滚动到最新一行
const logBox = ref<HTMLElement | null>(null)
let logScrollQueued = false
function scrollLogToBottom() {
  if (logScrollQueued) return
  logScrollQueued = true
  nextTick(() => {
    logScrollQueued = false
    if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
  })
}

watch(logsNodeId, () => {
  unsubscribeLogs()
  subscribeLogs()
  scrollLogToBottom()
})

// 任务自身错误字段（后端写入）：出现/变化时弹一次 error toast
watch(() => task.value?.error, (v) => {
  if (v) toast.add({ title: String(v), color: 'error' })
}, { immediate: true })

watch(rt.connected, (v) => {
  // WS 断线重连后重新订阅日志流（后端连接状态已重置）
  if (v && logSubscribed) {
    logSubscribed = false // 重置后重发订阅（否则被 subscribeLogs 守卫挡住）
    subscribedNodeId = null
    subscribeLogs()
  }
})

function refreshLogs() {
  // 容器退出（log_end）后重订阅，容器重启后新日志继续推送
  unsubscribeLogs()
  subscribeLogs()
  scrollLogToBottom()
}

let taskRefreshTimer: ReturnType<typeof setInterval> | null = null
let inferenceMetricsTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  rt.on('log', onLog)
  rt.on('log_reset', onLogReset)
  rt.on('log_end', onLogEnd)
  rt.on('container_status', onContainerStatus)
  rt.on('task_status', onTaskStatus)
  rt.on('task_deleted', onTaskDeleted)
  rt.on('benchmark_result', onBenchmarkResult)
  load().then(() => {
    subscribeLogs()
    loadInferenceMetrics(true)
  })
  taskRefreshTimer = setInterval(() => {
    if (task.value && ['running', 'published'].includes(task.value.status)) {
      load()
    }
  }, 10000)
  // 1h 每 5s 更新；24h 聚合视图每 30s 更新。
  inferenceMetricsTimer = setInterval(() => {
    const refreshMs = inferenceWindow.value === 86400 ? 30000 : 5000
    if (task.value?.status === 'running' && document.visibilityState === 'visible'
      && Date.now() - lastInferenceLoadAt >= refreshMs) {
      loadInferenceMetrics(true)
    }
  }, 5000)
})

onUnmounted(() => {
  if (taskRefreshTimer) clearInterval(taskRefreshTimer)
  taskRefreshTimer = null
  if (inferenceMetricsTimer) clearInterval(inferenceMetricsTimer)
  inferenceMetricsTimer = null
  unsubscribeLogs()
  rt.off('log', onLog)
  rt.off('log_reset', onLogReset)
  rt.off('log_end', onLogEnd)
  rt.off('container_status', onContainerStatus)
  rt.off('task_status', onTaskStatus)
  rt.off('task_deleted', onTaskDeleted)
  rt.off('benchmark_result', onBenchmarkResult)
})
</script>

<template>
  <UDashboardPanel id="task-detail">
    <template #header>
      <UDashboardNavbar>
        <template #leading>
          <UDashboardSidebarCollapse />
          <UButton size="sm" variant="ghost" to="/tasks">{{ $t('common.back') }}</UButton>
        </template>
        <template #title>
          <div class="flex items-center gap-2">
            <span>{{ task?.name || $t('tasks.detail_title') }}</span>
            <UBadge v-if="task" :color="statusColor[task.status] || 'neutral'" variant="subtle">{{ statusLabel(task.status) }}</UBadge>
          </div>
        </template>
        <template #right>
          <div v-if="task" class="flex gap-2">
            <UButton
              v-if="task.status === 'running'"
              size="sm"
              variant="outline"
              :loading="acting"
              @click="act('pause')"
            >{{ $t('tasks.pause') }}</UButton>
            <UButton
              v-if="task.status === 'running'"
              size="sm"
              variant="outline"
              :loading="acting"
              @click="act('restart')"
            >{{ $t('tasks.restart') }}</UButton>
            <UButton
              v-if="task.status === 'paused'"
              size="sm"
              color="primary"
              :loading="acting"
              @click="act('resume')"
            >{{ $t('tasks.resume') }}</UButton>
            <UButton
              v-if="['stopped', 'error'].includes(task.status)"
              size="sm"
              color="primary"
              :loading="acting"
              @click="act('start')"
            >{{ $t('tasks.start') }}</UButton>
            <UButton
              v-if="['running', 'paused', 'published', 'error'].includes(task.status)"
              size="sm"
              variant="outline"
              :loading="acting"
              @click="openAction('stop')"
            >{{ $t('tasks.stop') }}</UButton>
            <UButton size="sm" variant="outline" color="error" :loading="acting" @click="openAction('delete')">{{ $t('common.delete') }}</UButton>
          </div>
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
    <div>
      <UModal v-model:open="showActionModal" :title="pendingAction === 'delete' ? $t('tasks.delete_title') : $t('tasks.stop_title')">
        <template #body>
          <p class="text-sm text-gray-600 dark:text-gray-300">
            {{ $t('tasks.confirm_action', { action: pendingAction === 'delete' ? $t('common.delete') : $t('tasks.stop'), name: task?.name }) }}
            {{ pendingAction === 'delete' ? $t('tasks.delete_effect') : $t('tasks.stop_effect') }}
          </p>
          <UFormField :label="$t('tasks.model_handling_label')" class="mt-3">
            <UCheckbox v-model="deleteModel" :label="$t('tasks.delete_model_label')" />
          </UFormField>
        </template>
        <template #footer>
          <div class="flex w-full justify-end gap-2">
            <UButton variant="outline" @click="showActionModal = false">{{ $t('common.cancel') }}</UButton>
            <UButton :color="pendingAction === 'delete' ? 'error' : 'primary'" :loading="acting" @click="confirmAction">
              {{ $t('tasks.confirm_btn', { action: pendingAction === 'delete' ? $t('common.delete') : $t('tasks.stop') }) }}
            </UButton>
          </div>
        </template>
      </UModal>

      <div v-if="task" class="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <UCard>
          <template #header><div class="font-semibold">{{ $t('tasks.info_title') }}</div></template>
          <dl class="text-sm space-y-1.5">
            <div class="flex justify-between"><dt class="text-gray-500">{{ $t('tasks.col_recipe') }}</dt><dd>{{ recipeName }}</dd></div>
            <div class="flex justify-between"><dt class="text-gray-500">{{ $t('tasks.col_cluster') }}</dt><dd>{{ clusterName }}</dd></div>
            <div class="flex justify-between"><dt class="text-gray-500">{{ $t('tasks.col_created') }}</dt><dd>{{ fmtDateTime(task.created_at) }}</dd></div>
          </dl>
          <div class="mt-3">
            <div class="text-xs text-gray-500 mb-1">{{ $t('tasks.user_vars') }}</div>
            <pre class="bg-gray-50 dark:bg-gray-900 rounded p-2 text-[11px] overflow-x-auto">{{ JSON.stringify(task.variables, null, 2) }}</pre>
          </div>
        </UCard>

        <div class="lg:col-span-2 space-y-4">
          <UCard>
            <template #header><div class="font-semibold">{{ $t('tasks.node_containers', { count: task.nodes?.length }) }}</div></template>
            <div class="overflow-x-auto">
              <table class="w-full text-sm">
                <thead>
                  <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
                    <th class="py-2 pr-4 font-medium">{{ $t('tasks.col_node') }}</th>
                    <th class="py-2 pr-4 font-medium">{{ $t('tasks.col_role') }}</th>
                    <th class="py-2 pr-4 font-medium">rank</th>
                    <th class="py-2 pr-4 font-medium">{{ $t('tasks.col_container') }}</th>
                    <th class="py-2 pr-4 font-medium">{{ $t('tasks.col_status') }}</th>
                    <th class="py-2 font-medium">{{ $t('tasks.col_error') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="tn in task.nodes" :key="tn.id" class="border-b border-gray-100 dark:border-gray-800/60">
                    <td class="py-2.5 pr-4">
                      <NuxtLink :to="`/nodes/${tn.node_id}`" class="hover:underline">{{ nodeName(tn.node_id) }}</NuxtLink>
                    </td>
                    <td class="py-2.5 pr-4"><UBadge variant="subtle">{{ statusLabel(tn.role) }}</UBadge></td>
                    <td class="py-2.5 pr-4">{{ tn.node_rank }}</td>
                    <td class="py-2.5 pr-4 font-mono text-xs text-gray-600">{{ tn.container_name || '—' }}</td>
                    <td class="py-2.5 pr-4">{{ statusLabel(tn.container_status) || '—' }}</td>
                    <td class="py-2.5 text-xs text-red-500">{{ tn.error || '' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </UCard>

          <UCard v-if="hasInferenceEndpoint">
            <template #header>
              <div class="flex flex-wrap items-center justify-between gap-3">
                <div class="font-semibold">{{ $t('tasks.inference_title') }}</div>
                <div class="flex items-center gap-1 rounded-lg bg-elevated p-1">
                  <UButton size="xs" :variant="inferenceWindow === 3600 ? 'solid' : 'ghost'" @click="inferenceWindow = 3600">{{ $t('home.last_hour') }}</UButton>
                  <UButton size="xs" :variant="inferenceWindow === 86400 ? 'solid' : 'ghost'" @click="inferenceWindow = 86400">{{ $t('home.last_day') }}</UButton>
                </div>
              </div>
            </template>
            <div class="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-3 xl:grid-cols-6">
              <div class="rounded-lg bg-elevated/50 p-3">
                <div class="text-xs text-muted">{{ $t('home.inference_avg') }}</div>
                <div class="mt-1 text-lg font-semibold">{{ inferenceSummary.decode_average_tokens_per_sec ?? '—' }} <span class="text-xs font-normal text-muted">tok/s</span></div>
                <div class="mt-1 text-xs text-muted">{{ $t('home.prefill_average_value', { value: inferenceSummary.prefill_average_tokens_per_sec ?? '—' }) }}</div>
              </div>
              <div class="rounded-lg bg-elevated/50 p-3">
                <div class="text-xs text-muted">{{ $t('home.decode_peak') }}</div>
                <div class="mt-1 text-lg font-semibold">{{ inferenceSummary.decode_peak_tokens_per_sec ?? '—' }} <span class="text-xs font-normal text-muted">tok/s</span></div>
              </div>
              <div class="rounded-lg bg-elevated/50 p-3">
                <div class="text-xs text-muted">{{ $t('home.prefill_peak') }}</div>
                <div class="mt-1 text-lg font-semibold">{{ inferenceSummary.prefill_peak_tokens_per_sec ?? '—' }} <span class="text-xs font-normal text-muted">tok/s</span></div>
              </div>
              <div class="rounded-lg bg-elevated/50 p-3">
                <div class="text-xs text-muted">{{ $t('home.request_peak') }}</div>
                <div class="mt-1 text-lg font-semibold">{{ inferenceSummary.request_peak_per_sec ?? '—' }} <span class="text-xs font-normal text-muted">req/s</span></div>
                <div class="mt-1 text-xs text-muted">{{ $t('home.window_request_total', { requests: inferenceSummary.window_requests }) }}</div>
              </div>
              <div class="rounded-lg bg-elevated/50 p-3">
                <div class="text-xs text-muted">{{ $t('home.ttft_p95') }}</div>
                <div class="mt-1 text-lg font-semibold">{{ inferenceSummary.ttft_p95_ms ?? '—' }} <span class="text-xs font-normal text-muted">ms</span></div>
                <div class="mt-1 text-xs text-muted">{{ $t('home.kv_cache_peak_value', { value: inferenceSummary.kv_cache_peak_percent ?? '—' }) }}</div>
              </div>
              <div class="rounded-lg bg-elevated/50 p-3">
                <div class="text-xs text-muted">{{ $t('home.inference_window_label') }}</div>
                <div class="mt-1 text-lg font-semibold">{{ inferenceSummary.window_generated_tokens }} <span class="text-xs font-normal text-muted">tok</span></div>
                <div class="mt-1 text-xs text-muted">{{ $t('home.prefill_window_total', { tokens: inferenceSummary.window_prompt_tokens }) }}</div>
              </div>
            </div>
            <div v-if="inferencePoints.length" class="grid grid-cols-1 xl:grid-cols-2 gap-4">
              <div>
                <ClientOnly><MetricChart :option="inferenceTokOption" /></ClientOnly>
              </div>
              <div>
                <ClientOnly><MetricChart :option="inferenceRequestOption" /></ClientOnly>
              </div>
              <div class="xl:col-span-2">
                <ClientOnly><MetricChart :option="inferenceLatOption" /></ClientOnly>
              </div>
            </div>
            <p v-else class="text-sm text-gray-500">{{ $t('tasks.inference_empty', { status: statusLabel(task.status) }) }}</p>
          </UCard>

          <UCard v-if="hasInferenceEndpoint">
            <template #header>
              <div class="flex items-center justify-between">
                <div class="font-semibold">{{ $t('tasks.benchmark_title') }}</div>
                <div class="flex items-center gap-2">
                  <UInput v-model.number="benchForm.concurrency" type="number" class="w-24" :placeholder="$t('tasks.benchmark_concurrency')" />
                  <UInput v-model.number="benchForm.num_requests" type="number" class="w-24" :placeholder="$t('tasks.benchmark_requests')" />
                  <UInput v-model.number="benchForm.max_tokens" type="number" class="w-24" :placeholder="$t('tasks.benchmark_max_tokens')" />
                  <UButton size="sm" color="primary" :loading="runningBenchmark" :disabled="task.status !== 'running'" @click="runBenchmark">
                    {{ $t('tasks.benchmark_run') }}
                  </UButton>
                </div>
              </div>
            </template>

            <template v-if="benchmarkResult">
              <div class="flex flex-wrap gap-x-6 gap-y-1 text-xs text-gray-500 mb-3">
                <span>{{ $t('tasks.benchmark_tok') }}：<b class="text-gray-800 dark:text-gray-100">{{ benchmarkResult.tokens_per_sec ?? '—' }}</b> tok/s（并发 {{ benchmarkResult.concurrency ?? '—' }}）</span>
                <span>{{ $t('tasks.benchmark_ttft') }}：<b class="text-gray-800 dark:text-gray-100">{{ benchmarkResult.ttft_p50_ms ?? '—' }} / {{ benchmarkResult.ttft_p95_ms ?? '—' }}</b> ms</span>
                <span>{{ $t('tasks.benchmark_e2e') }}：<b class="text-gray-800 dark:text-gray-100">{{ benchmarkResult.e2e_p50_ms ?? '—' }} / {{ benchmarkResult.e2e_p95_ms ?? '—' }}</b> ms</span>
                <span>{{ $t('tasks.benchmark_itl') }}：<b class="text-gray-800 dark:text-gray-100">{{ benchmarkResult.itl_p50_ms ?? '—' }} / {{ benchmarkResult.itl_p95_ms ?? '—' }}</b> ms</span>
                <span>{{ $t('tasks.benchmark_success') }}：<b class="text-gray-800 dark:text-gray-100">{{ benchmarkResult.succeeded ?? 0 }} / {{ benchmarkResult.failed ?? 0 }}</b></span>
              </div>
              <p v-if="benchmarkResult.ok === false" class="text-xs text-red-500 mb-2">{{ benchmarkResult.error }}</p>
              <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">
                <div>
                  <div class="text-xs text-gray-500 mb-1">{{ $t('tasks.benchmark_hist_ttft') }}</div>
                  <ClientOnly><MetricChart :option="ttftHistOption" height="220px" /></ClientOnly>
                </div>
                <div>
                  <div class="text-xs text-gray-500 mb-1">{{ $t('tasks.benchmark_hist_e2e') }}</div>
                  <ClientOnly><MetricChart :option="e2eHistOption" height="220px" /></ClientOnly>
                </div>
              </div>
            </template>
            <p v-else class="text-sm text-gray-500">{{ $t('tasks.benchmark_empty') }}</p>
          </UCard>

          <UCard>
            <template #header>
              <div class="flex items-center justify-between">
                <div class="font-semibold">{{ $t('tasks.container_logs') }}</div>
                <div class="flex items-center gap-2">
                  <USelectMenu value-key="value"
                    v-model="logsNodeId"
                    :items="(task.nodes || []).map((tn: any) => ({ label: nodeName(tn.node_id), value: tn.node_id }))"
                    class="w-40"
                  />
                  <UButton size="xs" variant="outline" @click="refreshLogs">{{ $t('common.refresh') }}</UButton>
                </div>
              </div>
            </template>
            <pre ref="logBox" class="bg-gray-50 dark:bg-gray-900 rounded-md p-3 text-xs overflow-x-auto overflow-y-auto whitespace-pre max-h-96">{{ logs }}</pre>
          </UCard>
        </div>
      </div>
    </div>
    </template>
  </UDashboardPanel>
</template>
