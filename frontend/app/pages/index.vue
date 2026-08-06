<script setup lang="ts">
const api = useApi()
const rt = useRealtime()
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

onMounted(() => {
  load()
  rt.on('metrics', onMetrics)
  const t = setInterval(() => {
    // WS 已连接时由推送驱动，轮询仅作降级兜底
    if (!rt.connected.value) load()
  }, 15000)
  onUnmounted(() => {
    clearInterval(t)
    rt.off('metrics', onMetrics)
  })
})

const stats = computed(() => {
  const o = overview.value || {}
  return [
    { label: '节点', value: `${o.nodes_online ?? 0} / ${o.nodes_total ?? 0}`, sub: '在线 / 总数' },
    { label: '集群', value: o.clusters_total ?? 0, sub: '个集群' },
    { label: '配方', value: o.recipes_total ?? 0, sub: '套配置方案' },
    { label: '运行中任务', value: o.tasks_running ?? 0, sub: `暂停 ${o.tasks_paused ?? 0} · 共 ${o.tasks_total ?? 0}` },
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
      <h1 class="text-xl font-bold">总览</h1>
      <UButton size="sm" variant="outline" :loading="loading" @click="load">刷新</UButton>
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
        <div class="text-sm font-semibold">GPU 聚合（在线节点）</div>
        <div class="text-sm text-gray-500">共 {{ overview.gpu_aggregate?.total ?? 0 }} 张 GPU</div>
      </div>
      <div class="space-y-4">
        <div>
          <div class="flex justify-between text-xs text-gray-500 mb-1">
            <span>GPU 利用率</span>
            <span>{{ gpu.util == null ? '暂无数据' : gpu.util + '%' }}</span>
          </div>
          <UProgress :model-value="gpu.util || 0" color="primary" size="lg" />
        </div>
        <div>
          <div class="flex justify-between text-xs text-gray-500 mb-1">
            <span>统一内存占用</span>
            <span>{{ gpu.memLabel }}（{{ gpu.memPct }}%）</span>
          </div>
          <UProgress :model-value="gpu.memPct" color="success" size="lg" />
        </div>
      </div>
    </UCard>

    <UCard v-if="!overview && !loading" class="mt-4">
      <p class="text-sm text-gray-500">暂无数据。请先到「节点」页面添加并部署 Agent。</p>
    </UCard>
  </div>
</template>
