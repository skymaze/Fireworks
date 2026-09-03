<script setup lang="ts">
import {
  fetchInferenceMetrics,
  emptyInferenceMetrics,
  type InferencePoint,
} from '../composables/useInferenceStats'

interface TopologyNode {
  id: number
  name: string
  ip: string
  status: string
  cluster_id: number | null
  cluster_name: string | null
  gpu_count: number
  gpu_utilization: number | null
  gpu_mem_used: number
  gpu_mem_total: number
}

const api = useApi()
const rt = useRealtime()
const toast = useToast()
const { t } = useI18n()
const overview = ref<any>(null)
const loading = ref(true)
const refreshing = ref(false)
// 推理：后端完整差分窗口源数据，摘要不降采样；图表按时间桶聚合。
const inferenceMetrics = ref(emptyInferenceMetrics())
const inferenceWindow = ref(3600)
let inferenceRequestInFlight = false
let inferenceReloadPending = false
let lastInferenceLoadAt = 0
let mounted = false
let requestInFlight = false
let fallbackTimer: ReturnType<typeof setInterval> | null = null
let freshnessTimer: ReturnType<typeof setInterval> | null = null
let eventRefreshTimer: ReturnType<typeof setTimeout> | null = null

async function load(background = false) {
  if (requestInFlight) return
  requestInFlight = true
  if (background) refreshing.value = true
  try {
    overview.value = await api.get('/overview')
  } catch (e) {
    if (!background || !overview.value) {
      toast.add({ title: errorMsg(e), color: 'error' })
    }
  } finally {
    requestInFlight = false
    loading.value = false
    refreshing.value = false
  }
}

async function loadInference(background = false) {
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
      // 1h=10 秒桶，24h=5 分钟桶；仅影响图表分辨率，不影响摘要准确度。
      maxPoints: requestedWindow === 86400 ? 288 : 360,
    })
    lastInferenceLoadAt = Date.now()
  } catch (e) {
    if (!background) toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    inferenceRequestInFlight = false
    if (inferenceReloadPending || inferenceWindow.value !== requestedWindow) {
      inferenceReloadPending = false
      loadInference(true)
    }
  }
}

watch(inferenceWindow, () => loadInference())

function refreshGpuAggregate() {
  if (!overview.value) return
  const online = (overview.value.topology_nodes || []).filter((node: TopologyNode) => node.status === 'online')
  const util = online.map((node: TopologyNode) => node.gpu_utilization).filter((v: number | null) => v != null)
  overview.value.gpu_aggregate.total = online.reduce(
    (sum: number, node: TopologyNode) => sum + (node.gpu_count || 0), 0)
  overview.value.gpu_aggregate.utilization = util.length
    ? Math.round(util.reduce((sum: number, value: number) => sum + value, 0) / util.length * 10) / 10
    : null
  overview.value.gpu_aggregate.mem_used = online.reduce((sum: number, node: TopologyNode) => sum + (node.gpu_mem_used || 0), 0)
  overview.value.gpu_aggregate.mem_total = online.reduce((sum: number, node: TopologyNode) => sum + (node.gpu_mem_total || 0), 0)
}

function onMetrics(msg: any) {
  if (!mounted || !overview.value) return
  const node = (overview.value.topology_nodes || []).find((item: TopologyNode) => item.id === msg.node_id)
  if (!node) {
    scheduleRefresh()
    return
  }
  const gpu = msg.data?.gpu || {}
  node.status = 'online'
  node.gpu_utilization = gpu.utilization ?? null
  node.gpu_mem_used = gpu.mem_used ?? 0
  node.gpu_mem_total = gpu.mem_total ?? 0
  overview.value.nodes_online = (overview.value.topology_nodes || [])
    .filter((item: TopologyNode) => item.status === 'online').length
  refreshGpuAggregate()
}

function scheduleRefresh() {
  if (!mounted || eventRefreshTimer || document.visibilityState !== 'visible') return
  eventRefreshTimer = setTimeout(() => {
    eventRefreshTimer = null
    load(true)
  }, 1200)
}

function onNodeStatus(msg: any) {
  if (overview.value) {
    const node = (overview.value.topology_nodes || []).find((item: TopologyNode) => item.id === msg.node_id)
    if (node && msg.status) {
      node.status = msg.status
      overview.value.nodes_online = (overview.value.topology_nodes || [])
        .filter((item: TopologyNode) => item.status === 'online').length
      refreshGpuAggregate()
      return
    }
  }
  scheduleRefresh()
}

function onVisibilityChange() {
  if (document.visibilityState === 'visible' && overview.value
    && Date.now() / 1000 - overview.value.snapshot_at > 30) {
    load(true)
  }
}

onMounted(() => {
  mounted = true
  load()
  loadInference()
  rt.on('metrics', onMetrics)
  rt.on('node_status', onNodeStatus)
  rt.on('benchmark_result', scheduleRefresh)
  document.addEventListener('visibilitychange', onVisibilityChange)
  fallbackTimer = setInterval(() => {
    if (!rt.connected.value && document.visibilityState === 'visible') load(true)
  }, 30000)
  // 1h 视图每 5s 更新；24h 聚合较重，每 30s 更新。
  freshnessTimer = setInterval(() => {
    const refreshMs = inferenceWindow.value === 86400 ? 30000 : 5000
    if (document.visibilityState === 'visible' && Date.now() - lastInferenceLoadAt >= refreshMs) {
      loadInference(true)
    }
  }, 5000)
})

onUnmounted(() => {
  mounted = false
  if (fallbackTimer) clearInterval(fallbackTimer)
  if (freshnessTimer) clearInterval(freshnessTimer)
  if (eventRefreshTimer) clearTimeout(eventRefreshTimer)
  fallbackTimer = null
  freshnessTimer = null
  eventRefreshTimer = null
  rt.off('metrics', onMetrics)
  rt.off('node_status', onNodeStatus)
  rt.off('benchmark_result', scheduleRefresh)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

const stats = computed(() => {
  const o = overview.value || {}
  return [
    { label: t('nav.nodes'), value: `${o.nodes_online ?? 0} / ${o.nodes_total ?? 0}`, sub: t('home.online_total'), icon: 'lucide:server' },
    { label: t('nav.clusters'), value: o.clusters_total ?? 0, sub: t('home.cluster_unit'), icon: 'lucide:boxes' },
    { label: t('home.running_tasks'), value: o.tasks_running ?? 0, sub: t('home.tasks_total', { count: o.tasks_total ?? 0 }), icon: 'lucide:rocket' },
    { label: t('nav.recipes'), value: o.recipes_total ?? 0, sub: t('home.recipe_unit'), icon: 'lucide:store' },
  ]
})

function fmtBytes(value: number) {
  const v = value || 0
  if (v >= 1024 ** 4) return `${(v / 1024 ** 4).toFixed(1)} TB`
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(1)} GB`
  if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(0)} MB`
  return `${v} B`
}

const gpu = computed(() => {
  const aggregate = overview.value?.gpu_aggregate || {}
  const memPct = aggregate.mem_total ? Math.round(aggregate.mem_used / aggregate.mem_total * 1000) / 10 : 0
  return {
    util: aggregate.utilization,
    memPct,
    memLabel: `${fmtBytes(aggregate.mem_used)} / ${fmtBytes(aggregate.mem_total)}`,
  }
})

const derivedInferencePoints = computed(() => inferenceMetrics.value.points)

const inference = computed(() => inferenceMetrics.value.summary)

const inferenceStats = computed(() => {
  const s = inference.value
  return [
    { label: t('home.inference_avg'), value: s.decode_average_tokens_per_sec, unit: 'tok/s', sub: t('home.prefill_average_value', { value: s.prefill_average_tokens_per_sec ?? '—' }) },
    { label: t('home.decode_peak'), value: s.decode_peak_tokens_per_sec, unit: 'tok/s', sub: s.decode_peak_at ? fmtDateTime(new Date(s.decode_peak_at * 1000).toISOString()) : t('home.no_traffic') },
    { label: t('home.prefill_peak'), value: s.prefill_peak_tokens_per_sec, unit: 'tok/s', sub: `${s.prefill_peak_at ? fmtDateTime(new Date(s.prefill_peak_at * 1000).toISOString()) : t('home.no_traffic')} · ${t('home.prefill_peak_note')}` },
    { label: t('home.request_peak'), value: s.request_peak_per_sec, unit: 'req/s', sub: t('home.window_request_total', { requests: s.window_requests }) },
    { label: t('home.ttft_p95'), value: s.ttft_p95_ms, unit: 'ms', sub: t('home.kv_cache_peak_value', { value: s.kv_cache_peak_percent ?? '—' }) },
    { label: t('home.inference_window_label'), value: s.window_generated_tokens, unit: 'tok', sub: t('home.prefill_window_total', { tokens: s.window_prompt_tokens }) },
  ]
})

// Token 吞吐图：decode 按模型/任务堆叠柱状——空白时段无柱、各模型占比可直接比较。
const inferenceTokenOption = computed(() => {
  const tokLabel = t('tasks.inference_tok')
  const byTask = new Map<number, InferencePoint[]>()
  for (const p of derivedInferencePoints.value) {
    if (!byTask.has(p.task_id)) byTask.set(p.task_id, [])
    byTask.get(p.task_id)!.push(p)
  }
  const series = [...byTask.entries()].map(([taskId, points]) => ({
    name: `${points[0].task_name || `${t('nav.tasks')} ${taskId}`} · ${tokLabel}`,
    type: 'bar',
    stack: 'decode',
    barMaxWidth: 24,
    data: points.map((point) => [point.ts * 1000, point.tokens_per_sec]),
    itemStyle: { borderRadius: [3, 3, 0, 0] },
  }))
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { type: 'scroll', top: 0 },
    grid: { left: 52, right: 24, top: 42, bottom: 40 },
    xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
    yAxis: { type: 'value', name: 'tok/s', min: 0, scale: true },
    series,
  }
})

// 输入 token 体量：prefill 速率受 prefix cache 影响，不作为吞吐指标，仅展示输入侧负载体量。
const inferenceInputOption = computed(() => {
  const inputLabel = t('tasks.inference_input_tokens')
  const byTask = new Map<number, InferencePoint[]>()
  for (const p of derivedInferencePoints.value) {
    if (!byTask.has(p.task_id)) byTask.set(p.task_id, [])
    byTask.get(p.task_id)!.push(p)
  }
  const series = [...byTask.entries()].map(([taskId, points]) => ({
    name: `${points[0].task_name || `${t('nav.tasks')} ${taskId}`} · ${inputLabel}`,
    type: 'bar',
    stack: 'input',
    barMaxWidth: 24,
    data: points.map((point) => [point.ts * 1000, point.prompt_tokens]),
    itemStyle: { borderRadius: [3, 3, 0, 0] },
  }))
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { type: 'scroll', top: 0 },
    grid: { left: 52, right: 24, top: 42, bottom: 40 },
    xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
    yAxis: { type: 'value', name: 'tok', min: 0, scale: true },
    series,
  }
})

const inferenceRequestOption = computed(() => {
  const byTask = new Map<number, InferencePoint[]>()
  for (const point of derivedInferencePoints.value) {
    if (!byTask.has(point.task_id)) byTask.set(point.task_id, [])
    byTask.get(point.task_id)!.push(point)
  }
  const series = [...byTask.entries()].map(([taskId, points]) => ({
    name: points[0].task_name || `${t('nav.tasks')} ${taskId}`,
    type: 'bar',
    barMaxWidth: 24,
    data: points.map((point) => [point.ts * 1000, point.requests]),
    itemStyle: { borderRadius: [3, 3, 0, 0] },
  }))
  return {
    tooltip: { trigger: 'axis' },
    legend: { type: 'scroll', top: 0 },
    grid: { left: 52, right: 24, top: 42, bottom: 40 },
    xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
    yAxis: { type: 'value', name: t('tasks.inference_requests_chart'), min: 0, minInterval: 1 },
    series,
  }
})

const topologyOption = computed(() => {
  const nodes: TopologyNode[] = overview.value?.topology_nodes || []
  const clusters = overview.value?.topology_clusters || []
  const graphNodes: any[] = clusters.map((cluster: any) => ({
    id: `cluster-${cluster.id}`,
    name: cluster.name,
    value: `${cluster.network_type.toUpperCase()} · ${cluster.network_cidr || t('home.network_unconfigured')}`,
    symbol: 'roundRect',
    symbolSize: [116, 46],
    category: 0,
    itemStyle: { color: '#2563eb' },
    label: { color: '#fff' },
  }))
  const links: any[] = []
  for (const node of nodes) {
    graphNodes.push({
      id: `node-${node.id}`,
      name: node.name,
      value: `${node.ip} · ${node.gpu_count} GPU`,
      symbolSize: 58,
      category: node.status === 'online' ? 1 : 2,
      itemStyle: { color: node.status === 'online' ? '#16a34a' : node.status === 'offline' ? '#dc2626' : '#64748b' },
      label: { position: 'bottom', distance: 7 },
    })
    if (node.cluster_id != null) links.push({ source: `cluster-${node.cluster_id}`, target: `node-${node.id}` })
  }
  return {
    tooltip: { formatter: '{b}<br/>{c}' },
    animationDurationUpdate: 400,
    series: [{
      type: 'graph',
      layout: 'force',
      roam: true,
      draggable: true,
      data: graphNodes,
      links,
      categories: [{ name: t('nav.clusters') }, { name: t('home.online_nodes') }, { name: t('home.offline_nodes') }],
      label: { show: true, fontSize: 11 },
      lineStyle: { color: '#94a3b8', width: 2, curveness: 0.08 },
      force: { repulsion: 260, edgeLength: [90, 150], gravity: 0.08 },
      emphasis: { focus: 'adjacency', lineStyle: { width: 4 } },
    }],
  }
})
</script>

<template>
  <UDashboardPanel id="overview">
    <template #header>
      <UDashboardNavbar :title="t('home.total')">
        <template #leading><UDashboardSidebarCollapse /></template>
        <template #right>
          <span v-if="overview" class="hidden text-xs text-muted sm:inline">
            {{ t('home.updated_at', { time: fmtTime(overview.snapshot_at) }) }}
          </span>
          <UButton icon="lucide:refresh-cw" size="sm" variant="outline" :loading="refreshing" @click="load(true)">
            {{ t('common.refresh') }}
          </UButton>
        </template>
      </UDashboardNavbar>
    </template>

    <template #body>
      <div v-if="loading" class="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <USkeleton v-for="i in 4" :key="i" class="h-32 rounded-lg" />
      </div>

      <div v-else-if="overview" class="space-y-4">
        <div class="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <UCard v-for="stat in stats" :key="stat.label">
            <template #header>
              <div class="flex items-center justify-between text-sm text-muted">
                <span>{{ stat.label }}</span><UIcon :name="stat.icon" class="size-4" />
              </div>
            </template>
            <div class="text-3xl font-semibold tracking-tight">{{ stat.value }}</div>
            <template #footer><div class="text-xs text-muted">{{ stat.sub }}</div></template>
          </UCard>
        </div>

        <div class="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <UCard class="xl:col-span-2">
            <template #header>
              <div class="flex items-center justify-between gap-3">
                <div>
                  <div class="font-semibold">{{ t('home.topology_title') }}</div>
                  <div class="text-xs text-muted">{{ t('home.topology_hint') }}</div>
                </div>
                <UBadge variant="subtle">{{ t('home.topology_nodes', { count: overview.nodes_total }) }}</UBadge>
              </div>
            </template>
            <ClientOnly v-if="overview.topology_nodes.length || overview.topology_clusters.length">
              <MetricChart :option="topologyOption" height="360px" />
            </ClientOnly>
            <p v-else class="py-16 text-center text-sm text-muted">{{ t('home.empty_guide') }}</p>
            <template #footer>
              <div class="flex flex-wrap gap-2 text-xs text-muted">
                <span class="inline-flex items-center gap-1"><span class="size-2 rounded-full bg-green-500" />{{ t('home.online_nodes') }} {{ overview.nodes_online }}</span>
                <span class="inline-flex items-center gap-1"><span class="size-2 rounded-full bg-red-500" />{{ t('home.offline_nodes') }} {{ overview.nodes_total - overview.nodes_online }}</span>
                <span>{{ t('home.topology_interaction') }}</span>
              </div>
            </template>
          </UCard>

          <UCard>
            <template #header>
              <div class="flex items-center justify-between">
                <div>
                  <div class="font-semibold">{{ t('home.gpu_aggregate') }}</div>
                  <div class="text-xs text-muted">{{ t('home.online_only') }}</div>
                </div>
                <UBadge variant="subtle">{{ overview.gpu_aggregate?.total ?? 0 }} GPU</UBadge>
              </div>
            </template>
            <div class="space-y-6 py-2">
              <div>
                <div class="mb-2 flex justify-between text-sm">
                  <span class="text-muted">{{ t('home.gpu_utilization') }}</span>
                  <span class="font-medium">{{ gpu.util == null ? t('home.no_data') : `${gpu.util}%` }}</span>
                </div>
                <UProgress :model-value="gpu.util || 0" color="primary" size="lg" />
              </div>
              <div>
                <div class="mb-2 flex justify-between text-sm">
                  <span class="text-muted">{{ t('home.gpu_mem') }}</span>
                  <span class="font-medium">{{ gpu.memPct }}%</span>
                </div>
                <UProgress :model-value="gpu.memPct" color="success" size="lg" />
                <div class="mt-2 text-right text-xs text-muted">{{ gpu.memLabel }}</div>
              </div>
            </div>
            <template #footer><div class="text-xs text-muted">{{ t('home.gpu_realtime_hint') }}</div></template>
          </UCard>
        </div>

        <UCard>
          <template #header>
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div class="font-semibold">{{ t('home.inference_title') }}</div>
                <div class="text-xs text-muted">{{ t('home.inference_hint') }}</div>
              </div>
              <div class="flex items-center gap-1 rounded-lg bg-elevated p-1">
                <UButton size="xs" :variant="inferenceWindow === 3600 ? 'solid' : 'ghost'" @click="inferenceWindow = 3600">{{ t('home.last_hour') }}</UButton>
                <UButton size="xs" :variant="inferenceWindow === 86400 ? 'solid' : 'ghost'" @click="inferenceWindow = 86400">{{ t('home.last_day') }}</UButton>
              </div>
            </div>
          </template>

          <div class="grid grid-cols-2 gap-3 lg:grid-cols-3">
            <div v-for="stat in inferenceStats" :key="stat.label" class="rounded-lg bg-elevated/50 p-3">
              <div class="text-xs text-muted">{{ stat.label }}</div>
              <div class="mt-1 text-xl font-semibold">
                {{ stat.value == null ? '—' : stat.value }}
                <span v-if="stat.value != null" class="text-xs font-normal text-muted">{{ stat.unit }}</span>
              </div>
              <div class="mt-1 truncate text-xs text-muted">{{ stat.sub }}</div>
            </div>
          </div>

          <ClientOnly v-if="derivedInferencePoints.length">
            <div class="grid grid-cols-1 gap-4 xl:grid-cols-2">
              <div class="xl:col-span-2"><MetricChart :option="inferenceTokenOption" height="320px" /></div>
              <MetricChart :option="inferenceInputOption" height="320px" />
              <MetricChart :option="inferenceRequestOption" height="320px" />
            </div>
          </ClientOnly>
          <div v-else class="flex min-h-64 flex-col items-center justify-center text-center">
            <UIcon name="lucide:activity" class="mb-3 size-9 text-muted" />
            <p class="text-sm font-medium">{{ t('home.inference_empty') }}</p>
            <p class="mt-1 max-w-lg text-xs text-muted">{{ t('home.inference_empty_hint') }}</p>
          </div>

          <template #footer>
            <div class="flex flex-wrap justify-between gap-2 text-xs text-muted">
              <span>{{ t('home.inference_samples', { source: inferenceMetrics.source_intervals, count: derivedInferencePoints.length, seconds: Math.round(inferenceMetrics.bucket_seconds) }) }}</span>
              <span>{{ t('home.inference_note') }}</span>
            </div>
          </template>
        </UCard>
      </div>

      <UCard v-else>
        <p class="text-sm text-muted">{{ t('home.empty_guide') }}</p>
      </UCard>
    </template>
  </UDashboardPanel>
</template>
