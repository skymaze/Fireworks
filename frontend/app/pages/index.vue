<script setup lang="ts">
import {
  fetchRawSamples,
  mergeSamples,
  lastSampleTs,
  deriveSeries,
  computeWindowStats,
  type RawInferenceSample,
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
// 推理：浏览器侧持有原始累计快照，拉取/差分/绘图（单接口 /api/inference/samples）
const rawSamples = ref<RawInferenceSample[]>([])
const INFERENCE_WINDOW = 3600
const FRESHNESS = 30
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
    overview.value = await api.get('/overview', { window: 3600 })
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

// 拉取原始样本：首屏全量（过去 1h），之后按上次最后 ts 增量；合并去重 + 窗口裁剪。
async function loadInference(background = false, incremental = false) {
  try {
    const from = incremental
      ? Math.max(lastSampleTs(rawSamples.value) - 0.001, 0)
      : Date.now() / 1000 - INFERENCE_WINDOW
    const incoming = await fetchRawSamples(api, { from })
    rawSamples.value = mergeSamples(rawSamples.value, incoming, INFERENCE_WINDOW)
  } catch (e) {
    if (!background) toast.add({ title: errorMsg(e), color: 'error' })
  }
}

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
  // 推理原始样本：每 5s 按时间增量拉取（纯 pull），差分由浏览器侧完成。
  freshnessTimer = setInterval(() => {
    if (document.visibilityState === 'visible') loadInference(true, true)
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

const derivedInferencePoints = computed(() => deriveSeries(rawSamples.value))

const inference = computed(() =>
  computeWindowStats(rawSamples.value, INFERENCE_WINDOW, FRESHNESS, derivedInferencePoints.value))

const inferenceStats = computed(() => {
  const s = inference.value
  const bench = overview.value?.benchmark_peak_tokens_per_sec
  const benchAt = overview.value?.benchmark_peak_at
  return [
    { label: t('home.inference_current'), value: s.currentTokensPerSec, unit: 'tok/s', sub: t('home.inference_tasks', { count: s.monitoredTasks }) },
    { label: t('home.inference_avg'), value: s.averageTokensPerSec, unit: 'tok/s', sub: t('home.last_hour') },
    { label: t('home.inference_peak'), value: s.peakTokensPerSec, unit: 'tok/s', sub: s.peakAt ? fmtDateTime(new Date(s.peakAt * 1000).toISOString()) : t('home.no_traffic') },
    { label: t('home.ttft_p95'), value: s.ttftP95Ms, unit: 'ms', sub: t('home.kv_cache_value', { value: s.kvCachePercent ?? '—' }) },
    { label: t('home.benchmark_peak'), value: bench, unit: 'tok/s', sub: benchAt ? fmtDateTime(new Date(benchAt * 1000).toISOString()) : t('home.no_benchmark') },
    { label: t('home.inference_window_label'), value: s.windowGeneratedTokens, unit: 'tok', sub: t('home.inference_window_totals', { tokens: s.windowGeneratedTokens, requests: s.windowRequests }) },
  ]
})

// 合成趋势图：每个任务贡献 tok/s + 生成 + 提示（y0）与 请求数（y1），同一时间轴
const inferenceTokenOption = computed(() => {
  const tokLabel = t('tasks.inference_tok')
  const genLabel = t('tasks.inference_gen')
  const promptLabel = t('tasks.inference_prompt')
  const reqLabel = t('tasks.inference_requests')
  const byTask = new Map<number, InferencePoint[]>()
  for (const p of derivedInferencePoints.value) {
    if (!byTask.has(p.task_id)) byTask.set(p.task_id, [])
    byTask.get(p.task_id)!.push(p)
  }
  const series: any[] = []
  for (const [taskId, points] of byTask) {
    const name = points[0].task_name || `${t('nav.tasks')} ${taskId}`
    const lines = [
      { name: `${name} · ${tokLabel}`, y: 0, data: [] as [number, number | null][] },
      { name: `${name} · ${genLabel}`, y: 0, data: [] as [number, number | null][] },
      { name: `${name} · ${promptLabel}`, y: 0, data: [] as [number, number | null][] },
      { name: `${name} · ${reqLabel}`, y: 1, data: [] as [number, number | null][] },
    ]
    for (const p of points) {
      lines[0].data.push([p.ts * 1000, p.tokens_per_sec])
      lines[1].data.push([p.ts * 1000, p.output_tokens])
      lines[2].data.push([p.ts * 1000, p.prompt_tokens])
      lines[3].data.push([p.ts * 1000, p.requests])
    }
    for (const line of lines) {
      series.push({ name: line.name, type: 'line', smooth: 0.25, showSymbol: false, connectNulls: false, yAxisIndex: line.y, data: line.data })
    }
  }
  return {
    tooltip: { trigger: 'axis' },
    legend: { type: 'scroll', top: 0 },
    grid: { left: 52, right: 52, top: 42, bottom: 40 },
    xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
    yAxis: [
      { type: 'value', name: 'tok/s', min: 0, scale: true },
      { type: 'value', name: reqLabel, min: 0, scale: true },
    ],
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
              <UBadge color="info" variant="subtle">{{ t('home.last_hour') }}</UBadge>
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
            <MetricChart :option="inferenceTokenOption" height="320px" />
          </ClientOnly>
          <div v-else class="flex min-h-64 flex-col items-center justify-center text-center">
            <UIcon name="lucide:activity" class="mb-3 size-9 text-muted" />
            <p class="text-sm font-medium">{{ t('home.inference_empty') }}</p>
            <p class="mt-1 max-w-lg text-xs text-muted">{{ t('home.inference_empty_hint') }}</p>
          </div>

          <template #footer>
            <div class="flex flex-wrap justify-between gap-2 text-xs text-muted">
              <span>{{ t('home.inference_samples', { count: derivedInferencePoints.length }) }}</span>
              <span v-if="derivedInferencePoints.length">{{ t('home.inference_window_totals', { tokens: inference.windowGeneratedTokens, requests: inference.windowRequests }) }}</span>
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
