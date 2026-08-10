<script setup lang="ts">
const { t } = useI18n()
const NO_LOGS = computed(() => t('tasks.no_logs'))
const route = useRoute()
const api = useApi()
const rt = useRealtime()
const taskId = Number(route.params.id)

const task = ref<any>(null)
const recipes = ref<any[]>([])
const clusters = ref<any[]>([])
const nodes = ref<any[]>([])
const logsNodeId = ref<number | null>(null)
const logs = ref('')
const error = ref('')
const acting = ref(false)
let logSubscribed = false

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
      await loadLogs()
    }
    loadInferenceMetrics()
    loadBenchmarks()
    error.value = ''
  } catch (e) {
    error.value = errorMsg(e)
  }
}

// 上一段日志是否为「原地刷新行」（update）：是则下一次 update 覆盖它，否则新起一行
let lastLogLineUpdate = false

async function loadLogs() {
  if (!logsNodeId.value) return
  lastLogLineUpdate = false
  try {
    const r = await api.get(`/tasks/${taskId}/logs`, { node_id: logsNodeId.value, tail: 300 })
    // 快照中的进度条 \r 原地刷新归一为独立行（实时流由 update 标记本行覆盖）
    logs.value = (r.logs || '').replace(/\r/g, '\n') || NO_LOGS.value
  } catch (e) {
    logs.value = t('tasks.log_fetch_fail', { error: String(e) })
  }
}

// ---------- 实时通道：日志流 + 容器/任务状态 ----------

function currentContainerName(): string | null {
  const tn = task.value?.nodes?.find((x: any) => x.node_id === logsNodeId.value)
  return tn?.container_name || null
}

function subscribeLogs() {
  if (!logsNodeId.value || logSubscribed) return
  rt.send({ type: 'log_subscribe', task_id: taskId, node_id: logsNodeId.value })
  logSubscribed = true
}

function unsubscribeLogs() {
  if (!logSubscribed) return
  rt.send({ type: 'log_unsubscribe', task_id: taskId, node_id: logsNodeId.value })
  logSubscribed = false
}

function onLog(msg: any) {
  if (msg.container && msg.container === currentContainerName()) {
    // 初始快照之后流式追加（保留尾部换行）
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
    if (logs.value.length > 500_000) logs.value = logs.value.slice(-500_000)
    scrollLogToBottom() // 推送后自动滚动到最新一行
  }
}

function onLogEnd(msg: any) {
  if (msg.container && msg.container === currentContainerName()) {
    logSubscribed = false // 流已结束（容器退出），停止退订命令避免空发
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

// ---------- 推理性能（LLM 探针实时曲线：tok/s、TTFT/E2E、KV cache） ----------

const inferenceMetrics = ref<any[]>([])
const INFERENCE_WINDOW = 3600
const INFERENCE_MAX = 2000

async function loadInferenceMetrics() {
  try {
    const to = Date.now() / 1000
    inferenceMetrics.value = await api.get(`/tasks/${taskId}/inference-metrics`, {
      from_ts: to - INFERENCE_WINDOW, to_ts: to, limit: 1500,
    })
  } catch (e) {
    // 探针/接口不可用不影响主页面
  }
}

function onInferenceMetrics(msg: any) {
  if (msg.task_id !== taskId) return
  const ts = msg.data?.ts || Date.now() / 1000
  inferenceMetrics.value.push({ ts, node_id: msg.node_id, data: msg.data })
  if (inferenceMetrics.value.length > INFERENCE_MAX) {
    inferenceMetrics.value = inferenceMetrics.value.slice(-INFERENCE_MAX)
  }
}

// 与后端 service_endpoint 判定一致：仅推理类任务（head + VLLM_PORT）展示面板
const hasInferenceEndpoint = computed(() =>
  (task.value?.nodes || []).some((tn: any) => tn.role === 'head'))

const latestInference = computed(() =>
  inferenceMetrics.value.length
    ? inferenceMetrics.value[inferenceMetrics.value.length - 1].data
    : {})

const inferenceTokOption = computed(() => ({
  tooltip: { trigger: 'axis' }, legend: { data: [t('tasks.inference_tok')], top: 0 },
  grid: { left: 40, right: 16, top: 30, bottom: 24 },
  xAxis: { type: 'category', data: inferenceMetrics.value.map((r) => fmtTime(r.ts)) },
  yAxis: { type: 'value', name: 'tok/s', scale: true },
  series: [{
    name: t('tasks.inference_tok'), type: 'line', smooth: true, areaStyle: { opacity: 0.15 },
    data: inferenceMetrics.value.map((r) => r.data.tokens_per_sec ?? null),
  }],
}))

const inferenceLatOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  legend: { data: [t('tasks.inference_ttft'), t('tasks.inference_e2e'), t('tasks.inference_kv')], top: 0 },
  grid: { left: 40, right: 48, top: 30, bottom: 24 },
  xAxis: { type: 'category', data: inferenceMetrics.value.map((r) => fmtTime(r.ts)) },
  yAxis: [
    { type: 'value', name: 'ms', scale: true },
    { type: 'value', name: '%', max: 100, splitLine: { show: false } },
  ],
  series: [
    { name: t('tasks.inference_ttft'), type: 'line', smooth: true, data: inferenceMetrics.value.map((r) => r.data.ttft_ms ?? null), yAxisIndex: 0 },
    { name: t('tasks.inference_e2e'), type: 'line', smooth: true, data: inferenceMetrics.value.map((r) => r.data.e2e_ms ?? null), yAxisIndex: 0 },
    { name: t('tasks.inference_kv'), type: 'line', smooth: true, data: inferenceMetrics.value.map((r) => r.data.kv_cache_percent ?? null), yAxisIndex: 1 },
  ],
}))

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
    error.value = errorMsg(e)
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
    error.value = errorMsg(e)
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

watch(logsNodeId, async () => {
  unsubscribeLogs()
  await loadLogs()
  subscribeLogs()
  scrollLogToBottom()
})

watch(rt.connected, (v) => {
  // WS 断线重连后重新订阅日志流（后端连接状态已重置）
  if (v && logSubscribed) {
    logSubscribed = false // 重置后重发订阅（否则被 subscribeLogs 守卫挡住）
    subscribeLogs()
  }
})

async function refreshLogs() {
  // 容器退出（log_end）后重订阅，容器重启后新日志继续推送
  unsubscribeLogs()
  await loadLogs()
  subscribeLogs()
  scrollLogToBottom()
}

onMounted(() => {
  rt.on('log', onLog)
  rt.on('log_end', onLogEnd)
  rt.on('container_status', onContainerStatus)
  rt.on('task_status', onTaskStatus)
  rt.on('task_deleted', onTaskDeleted)
  rt.on('inference_metrics', onInferenceMetrics)
  rt.on('benchmark_result', onBenchmarkResult)
  load().then(() => subscribeLogs())
  const t = setInterval(() => {
    if (task.value && ['running', 'published'].includes(task.value.status)) {
      load()
    }
  }, 10000)
  onUnmounted(() => {
    clearInterval(t)
    unsubscribeLogs()
    rt.off('log', onLog)
    rt.off('log_end', onLogEnd)
    rt.off('container_status', onContainerStatus)
    rt.off('task_status', onTaskStatus)
    rt.off('task_deleted', onTaskDeleted)
    rt.off('inference_metrics', onInferenceMetrics)
    rt.off('benchmark_result', onBenchmarkResult)
  })
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
              v-if="task.status === 'paused'"
              size="sm"
              color="primary"
              :loading="acting"
              @click="act('resume')"
            >{{ $t('tasks.resume') }}</UButton>
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
      <UModal v-model:open="showActionModal">
        <template #content>
          <UCard>
          <template #header>
            <div class="font-semibold">{{ pendingAction === 'delete' ? $t('tasks.delete_title') : $t('tasks.stop_title') }}</div>
          </template>
          <p class="text-sm text-gray-600 dark:text-gray-300">
            {{ $t('tasks.confirm_action', { action: pendingAction === 'delete' ? $t('common.delete') : $t('tasks.stop'), name: task?.name }) }}
            {{ pendingAction === 'delete' ? $t('tasks.delete_effect') : $t('tasks.stop_effect') }}
          </p>
          <UFormField :label="$t('tasks.model_handling_label')" class="mt-3">
            <UCheckbox v-model="deleteModel" :label="$t('tasks.delete_model_label')" />
          </UFormField>
          <template #footer>
            <div class="flex justify-end gap-2">
              <UButton variant="outline" @click="showActionModal = false">{{ $t('common.cancel') }}</UButton>
              <UButton :color="pendingAction === 'delete' ? 'error' : 'primary'" :loading="acting" @click="confirmAction">
                {{ $t('tasks.confirm_btn', { action: pendingAction === 'delete' ? $t('common.delete') : $t('tasks.stop') }) }}
              </UButton>
            </div>
          </template>
        </UCard>
        </template>
      </UModal>

      <ErrorBanner :error="error" />
      <ErrorBanner :error="task?.error" />

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
              <div class="flex items-center justify-between">
                <div class="font-semibold">{{ $t('tasks.inference_title') }}</div>
                <div class="flex items-center gap-3 text-xs text-gray-500">
                  <UBadge variant="subtle">{{ latestInference.backend || '—' }}</UBadge>
                  <span>{{ $t('tasks.inference_tok') }}：<b class="text-gray-800 dark:text-gray-100">{{ latestInference.tokens_per_sec ?? '—' }}</b></span>
                  <span>TTFT：<b class="text-gray-800 dark:text-gray-100">{{ latestInference.ttft_ms != null ? latestInference.ttft_ms + 'ms' : '—' }}</b></span>
                  <span>KV cache：<b class="text-gray-800 dark:text-gray-100">{{ latestInference.kv_cache_percent != null ? latestInference.kv_cache_percent + '%' : '—' }}</b></span>
                </div>
              </div>
            </template>
            <div v-if="inferenceMetrics.length" class="grid grid-cols-1 xl:grid-cols-2 gap-4">
              <div>
                <ClientOnly><MetricChart :option="inferenceTokOption" /></ClientOnly>
              </div>
              <div>
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
