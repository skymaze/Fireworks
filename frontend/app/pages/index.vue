<script setup lang="ts">
const api = useApi()
const rt = useRealtime()
const { t } = useI18n()
const overview = ref<any>(null)
const loading = ref(true)
const error = ref('')

async function load() {
  try {
    overview.value = await api.get('/overview')
    error.value = ''
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

let refreshTimer: ReturnType<typeof setTimeout> | null = null

function onMetrics() {
  // 任一节点指标推送到达 -> 防抖刷新总览（节点状态/GPU 聚合实时）
  if (refreshTimer) return
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    load()
  }, 1500)
}

function onNodeStatus() {
  // 节点上线/下线推送 -> 同样防抖刷新（在线/总数卡片秒级更新）
  onMetrics()
}

onMounted(() => {
  load()
  rt.on('metrics', onMetrics)
  rt.on('node_status', onNodeStatus)
  const t = setInterval(() => {
    // WS 已连接时由推送驱动，轮询仅作降级兜底
    if (!rt.connected.value) load()
  }, 15000)
  onUnmounted(() => {
    clearInterval(t)
    rt.off('metrics', onMetrics)
    rt.off('node_status', onNodeStatus)
  })
})

const stats = computed(() => {
  const o = overview.value || {}
  return [
    { label: t('nav.nodes'), value: `${o.nodes_online ?? 0} / ${o.nodes_total ?? 0}`, sub: t('home.online_total') },
    { label: t('nav.clusters'), value: o.clusters_total ?? 0, sub: t('home.cluster_unit') },
    { label: t('nav.recipes'), value: o.recipes_total ?? 0, sub: t('home.recipe_unit') },
    { label: t('home.running_tasks'), value: o.tasks_running ?? 0, sub: '' },
  ]
})

const gpu = computed(() => {
  const g = overview.value?.gpu_aggregate || {}
  const util = g.utilization
  const memPct = g.mem_total ? Math.round((g.mem_used / g.mem_total) * 1000) / 10 : 0
  // gpu_aggregate 内存单位为字节（agent 统一），按 1024 进制格式化
  const fmt = (v: number) =>
    v >= 1024 ** 4 ? `${(v / 1024 ** 4).toFixed(1)} TB` : v >= 1024 ** 3 ? `${(v / 1024 ** 3).toFixed(1)} GB` : v >= 1024 ** 2 ? `${(v / 1024 ** 2).toFixed(0)} MB` : `${v || 0} B`
  return { util, memPct, memLabel: `${fmt(g.mem_used)} / ${fmt(g.mem_total)}` }
})
</script>

<template>
  <div>
    <div class="flex items-center justify-between mb-4">
      <h1 class="text-xl font-bold">{{ t('home.total') }}</h1>
      <UButton size="sm" variant="outline" :loading="loading" @click="load">{{ t('common.refresh') }}</UButton>
    </div>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />

    <div v-if="overview" class="grid grid-cols-2 lg:grid-cols-4 gap-4">
      <UCard v-for="s in stats" :key="s.label">
        <div class="text-sm text-gray-500 dark:text-gray-400">{{ s.label }}</div>
        <div class="text-2xl font-bold mt-1">{{ s.value }}</div>
        <div class="text-xs text-gray-400 dark:text-gray-500 mt-1">{{ s.sub }}</div>
      </UCard>
    </div>

    <UCard v-if="overview" class="mt-4">
      <div class="flex items-center justify-between mb-3">
        <div class="text-sm font-semibold">{{ t('home.gpu_aggregate') }}</div>
        <div class="text-sm text-gray-500">{{ t('home.gpu_count') }}：{{ overview.gpu_aggregate?.total ?? 0 }}</div>
      </div>
      <div class="space-y-4">
        <div>
          <div class="flex justify-between text-xs text-gray-500 mb-1">
            <span>{{ t('home.gpu_utilization') }}</span>
            <span>{{ gpu.util == null ? t('home.no_data') : gpu.util + '%' }}</span>
          </div>
          <UProgress :model-value="gpu.util || 0" color="primary" size="lg" />
        </div>
        <div>
          <div class="flex justify-between text-xs text-gray-500 mb-1">
            <span>{{ t('home.gpu_mem') }}</span>
            <span>{{ gpu.memLabel }}（{{ gpu.memPct }}%）</span>
          </div>
          <UProgress :model-value="gpu.memPct" color="success" size="lg" />
        </div>
      </div>
    </UCard>

    <UCard v-if="!overview && !loading" class="mt-4">
      <p class="text-sm text-gray-500">{{ t('home.empty_guide') }}</p>
    </UCard>
  </div>
</template>
