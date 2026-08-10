<script setup lang="ts">
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

interface InferencePoint {
  ts: number
  task_id: number
  task_name: string
  task_status: string
  model_name: string | null
  backend: string
  tokens_per_sec: number | null
  ttft_ms: number | null
  e2e_ms: number | null
  kv_cache_percent: number | null
  preemptions: number | null
}

const api = useApi()
const rt = useRealtime()
const toast = useToast()
const { t } = useI18n()
const overview = ref<any>(null)
const loading = ref(true)
const refreshing = ref(false)
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

function recomputeInference() {
  const inference = overview.value?.inference
  if (!inference) return
  const points: InferencePoint[] = inference.series || []
  const latest = new Map<number, InferencePoint>()
  for (const point of points) latest.set(point.task_id, point)
  const freshnessCutoff = Date.now() / 1000 - (inference.freshness_seconds || 30)
  const currentPoints = [...latest.values()].filter(
    point => point.task_status === 'running' && point.ts >= freshnessCutoff,
  )
  const current = currentPoints.map(point => point.tokens_per_sec).filter((value): value is number => value != null)
  const currentKv = currentPoints.map(point => point.kv_cache_percent).filter((value): value is number => value != null)
  inference.monitored_tasks = latest.size
  inference.current_tokens_per_sec = current.length ? Math.round(current.reduce((a, b) => a + b, 0) * 10) / 10 : null
  inference.kv_cache_percent = currentKv.length ? Math.round(currentKv.reduce((a, b) => a + b, 0) / currentKv.length * 10) / 10 : null
  inference.preemptions = currentPoints.reduce((sum, point) => sum + (point.preemptions || 0), 0)
}

function onInferenceMetrics(msg: any) {
  if (!mounted || !overview.value?.inference || !msg.data) return
  const task = (overview.value.inference.series || []).find((point: InferencePoint) => point.task_id === msg.task_id)
  const point: InferencePoint = {
    ts: msg.data.ts || Date.now() / 1000,
    task_id: msg.task_id,
    task_name: msg.task_name || task?.task_name || `${t('nav.tasks')} ${msg.task_id}`,
    task_status: msg.task_status || task?.task_status || 'running',
    model_name: msg.model_name ?? task?.model_name ?? null,
    backend: msg.data.backend || 'unknown',
    tokens_per_sec: msg.data.tokens_per_sec ?? null,
    ttft_ms: msg.data.ttft_ms ?? null,
    e2e_ms: msg.data.e2e_ms ?? null,
    kv_cache_percent: msg.data.kv_cache_percent ?? null,
    preemptions: msg.data.preemptions ?? null,
  }
  overview.value.inference.series.push(point)
  const cutoff = Date.now() / 1000 - 3600
  overview.value.inference.series = overview.value.inference.series.filter((item: InferencePoint) => item.ts >= cutoff)
  overview.value.inference.sample_count = overview.value.inference.series.length
  if (point.tokens_per_sec != null && (
    overview.value.inference.peak_tokens_per_sec == null
    || point.tokens_per_sec > overview.value.inference.peak_tokens_per_sec
  )) {
    overview.value.inference.peak_tokens_per_sec = point.tokens_per_sec
    overview.value.inference.peak_at = point.ts
  }
  recomputeInference()
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
  rt.on('metrics', onMetrics)
  rt.on('node_status', onNodeStatus)
  rt.on('inference_metrics', onInferenceMetrics)
  rt.on('benchmark_result', scheduleRefresh)
  document.addEventListener('visibilitychange', onVisibilityChange)
  fallbackTimer = setInterval(() => {
    if (!rt.connected.value && document.visibilityState === 'visible') load(true)
  }, 30000)
  // 即使 WebSocket 正常但探针停止，也要让“当前吞吐”按后端定义的保鲜期自然过期。
  freshnessTimer = setInterval(() => {
    if (document.visibilityState === 'visible') recomputeInference()
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
  rt.off('inference_metrics', onInferenceMetrics)
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

const inferenceStats = computed(() => {
  const inference = overview.value?.inference || {}
  return [
    { label: t('home.inference_current'), value: inference.current_tokens_per_sec, unit: 'tok/s', sub: t('home.inference_tasks', { count: inference.monitored_tasks || 0 }) },
    { label: t('home.inference_peak'), value: inference.peak_tokens_per_sec, unit: 'tok/s', sub: inference.peak_at ? fmtDateTime(new Date(inference.peak_at * 1000).toISOString()) : t('home.no_data') },
    { label: t('home.benchmark_peak'), value: inference.benchmark_peak_tokens_per_sec, unit: 'tok/s', sub: inference.benchmark_peak_at ? fmtDateTime(new Date(inference.benchmark_peak_at * 1000).toISOString()) : t('home.no_benchmark') },
    { label: t('home.ttft_p95'), value: inference.ttft_p95_ms, unit: 'ms', sub: t('home.kv_cache_value', { value: inference.kv_cache_percent ?? '—' }) },
  ]
})

const inferenceTokenOption = computed(() => {
  const points: InferencePoint[] = overview.value?.inference?.series || []
  const tasks = new Map<number, { name: string, data: [number, number | null][] }>()
  for (const point of points) {
    if (!tasks.has(point.task_id)) tasks.set(point.task_id, { name: point.task_name, data: [] })
    tasks.get(point.task_id)!.data.push([point.ts * 1000, point.tokens_per_sec])
  }
  return {
    tooltip: { trigger: 'axis' },
    legend: { type: 'scroll', top: 0 },
    grid: { left: 52, right: 20, top: 42, bottom: 36 },
    xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
    yAxis: { type: 'value', name: 'tok/s', min: 0, scale: true },
    series: [...tasks.values()].map(task => ({
      name: task.name,
      type: 'line',
      smooth: 0.25,
      showSymbol: false,
      connectNulls: false,
      areaStyle: { opacity: tasks.size === 1 ? 0.12 : 0 },
      data: task.data,
    })),
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

          <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <div v-for="stat in inferenceStats" :key="stat.label" class="rounded-lg bg-elevated/50 p-3">
              <div class="text-xs text-muted">{{ stat.label }}</div>
              <div class="mt-1 text-xl font-semibold">
                {{ stat.value == null ? '—' : stat.value }}
                <span v-if="stat.value != null" class="text-xs font-normal text-muted">{{ stat.unit }}</span>
              </div>
              <div class="mt-1 truncate text-xs text-muted">{{ stat.sub }}</div>
            </div>
          </div>

          <ClientOnly v-if="overview.inference?.series?.length">
            <MetricChart :option="inferenceTokenOption" height="320px" />
          </ClientOnly>
          <div v-else class="flex min-h-64 flex-col items-center justify-center text-center">
            <UIcon name="lucide:activity" class="mb-3 size-9 text-muted" />
            <p class="text-sm font-medium">{{ t('home.inference_empty') }}</p>
            <p class="mt-1 max-w-lg text-xs text-muted">{{ t('home.inference_empty_hint') }}</p>
          </div>

          <template #footer>
            <div class="flex flex-wrap justify-between gap-2 text-xs text-muted">
              <span>{{ t('home.inference_samples', { count: overview.inference?.sample_count || 0 }) }}</span>
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
