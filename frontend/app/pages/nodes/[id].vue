<script setup lang="ts">
const { t } = useI18n()
const route = useRoute()
const api = useApi()
const confirm = useConfirmDialog()
const nodeId = Number(route.params.id)

const node = ref<any>(null)
const smi = ref('')
const metrics = ref<any[]>([])
const nodeModels = ref<any[]>([])
const range = ref(3600) // 1h
const autoload = ref(true)
const error = ref('')

async function loadModels() {
  try {
    nodeModels.value = (await api.get(`/nodes/${nodeId}/models`)).models || []
  } catch {
    /* ignore */
  }
}

async function removeModel(repo: string) {
  const ok = await confirm.open({ title: t('nodes.delete_model_title'), description: t('nodes.delete_model_confirm', { repo }) })
  if (!ok) return
  await api.del(`/nodes/${nodeId}/models/${repo}`)
  await loadModels()
}

const timeLabels = (rows: any[]) => rows.map((r) => fmtTime(r.ts))

const tempsOption = computed(() => {
  const rows = metrics.value
  const x = timeLabels(rows)
  const series: any[] = []
  const cpu = rows.map((r) => r.data.temperatures?.cpu ?? null)
  if (cpu.some((v) => v != null)) series.push({ name: 'CPU/SoC', type: 'line', data: cpu, smooth: true })
  const gpuCount = rows[0]?.data.gpus?.length ?? 0
  for (let i = 0; i < gpuCount; i++) {
    series.push({ name: `GPU${i}`, type: 'line', data: rows.map((r) => r.data.gpus?.[i]?.temperature ?? null), smooth: true })
  }
  return {
    tooltip: { trigger: 'axis' }, legend: { data: series.map((s) => s.name), top: 0 },
    grid: { left: 40, right: 16, top: 30, bottom: 24 }, xAxis: { type: 'category', data: x },
    yAxis: { type: 'value', name: '°C', scale: true }, series,
  }
})

const cpuOption = computed(() => ({
  tooltip: { trigger: 'axis' }, legend: { data: [t('nodes.chart_cpu')], top: 0 },
  grid: { left: 40, right: 16, top: 30, bottom: 24 },
  xAxis: { type: 'category', data: timeLabels(metrics.value) },
  yAxis: { type: 'value', name: '%', max: 100 },
  series: [{ name: t('nodes.chart_cpu'), type: 'line', smooth: true, areaStyle: { opacity: 0.15 }, data: metrics.value.map((r) => r.data.cpu_percent ?? 0) }],
}))

const gpuOption = computed(() => {
  const rows = metrics.value
  const x = timeLabels(rows)
  const gpuCount = rows[0]?.data.gpus?.length ?? 0
  const series: any[] = []
  for (let i = 0; i < gpuCount; i++) {
    series.push({ name: `GPU${i}`, type: 'line', smooth: true, data: rows.map((r) => r.data.gpus?.[i]?.utilization ?? 0) })
  }
  return {
    tooltip: { trigger: 'axis' }, legend: { data: series.map((s) => s.name), top: 0 },
    grid: { left: 40, right: 16, top: 30, bottom: 24 }, xAxis: { type: 'category', data: x },
    yAxis: { type: 'value', name: '%', max: 100 }, series,
  }
})

const memOption = computed(() => {
  const rows = metrics.value
  // 后端 gpu.mem_used/mem_total 统一为字节（GB10 统一内存 = 系统内存）
  const gb = (v: number) => v / 1e9
  const series: any[] = [
    { name: t('nodes.chart_series_sys_mem'), type: 'line', smooth: true, data: rows.map((r) => r.data.memory?.percent ?? 0), yAxisIndex: 0 },
  ]
  if (rows[0]?.data.gpu?.mem_total) {
    series.push({
      name: t('nodes.chart_series_gpu_mem'), type: 'line', smooth: true,
      data: rows.map((r) => gb(r.data.gpu?.mem_used ?? 0)), yAxisIndex: 1,
    })
  }
  return {
    tooltip: { trigger: 'axis' }, legend: { data: series.map((s) => s.name), top: 0 },
    grid: { left: 40, right: 48, top: 30, bottom: 24 }, xAxis: { type: 'category', data: timeLabels(rows) },
    yAxis: [
      { type: 'value', name: '%', max: 100 },
      { type: 'value', name: 'GB', splitLine: { show: false } },
    ],
    series,
  }
})

const diskOption = computed(() => {
  const rows = metrics.value
  const mounts = rows[0]?.data.disks?.map((d: any) => d.mount) || []
  const series = mounts.slice(0, 4).map((m: string) => ({
    name: m, type: 'line', smooth: true,
    data: rows.map((r) => r.data.disks?.find((d: any) => d.mount === m)?.percent ?? 0),
  }))
  return {
    tooltip: { trigger: 'axis' }, legend: { data: series.map((s) => s.name), top: 0 },
    grid: { left: 40, right: 16, top: 30, bottom: 24 }, xAxis: { type: 'category', data: timeLabels(rows) },
    yAxis: { type: 'value', name: '%', max: 100 }, series,
  }
})

const netOption = computed(() => {
  const rows = metrics.value
  const mb = (v: number) => v / 1024 / 1024
  return {
    tooltip: { trigger: 'axis' }, legend: { data: ['rx', 'tx'], top: 0 },
    grid: { left: 40, right: 16, top: 30, bottom: 24 }, xAxis: { type: 'category', data: timeLabels(rows) },
    yAxis: { type: 'value', name: 'MB/s' },
    series: [
      { name: 'rx', type: 'line', smooth: true, areaStyle: { opacity: 0.15 }, data: rows.map((r) => mb(r.data.network?.rx_bps ?? 0)) },
      { name: 'tx', type: 'line', smooth: true, areaStyle: { opacity: 0.15 }, data: rows.map((r) => mb(r.data.network?.tx_bps ?? 0)) },
    ],
  }
})

async function loadNode() {
  try {
    node.value = await api.get(`/nodes/${nodeId}`)
  } catch (e) {
    error.value = String(e)
  }
}

async function loadMetrics() {
  try {
    const to = Date.now() / 1000
    metrics.value = await api.get(`/nodes/${nodeId}/metrics`, {
      from_ts: to - range.value,
      to_ts: to,
      limit: 1500,
    })
  } catch (e) {
    error.value = String(e)
  }
}

// 实时指标（WS 推送，5s/节点）：自动刷新开启时直接追加图表数据
const rt = useRealtime()

function onMetrics(msg: any) {
  if (msg.node_id !== nodeId || !autoload.value) return
  const ts = msg.data?.ts || Date.now() / 1000
  metrics.value.push({ ts, data: msg.data })
  // 按时间范围裁剪，避免无限增长
  const cutoff = ts - range.value
  if (metrics.value.length > 2000) metrics.value = metrics.value.slice(-2000)
  if (cutoff > 0) {
    const idx = metrics.value.findIndex((m) => m.ts >= cutoff)
    if (idx > 0) metrics.value = metrics.value.slice(idx)
  }
  if (node.value) {
    node.value.agent_status = 'online'
    node.value.last_seen = new Date().toISOString()
  }
}

async function loadSmi() {
  try {
    smi.value = (await api.get(`/nodes/${nodeId}/nvidia-smi`)).output
  } catch (e) {
    smi.value = t('nodes.smi_fail', { error: String(e) })
  }
}

const refreshing = ref(false)

async function refreshAll() {
  // 先调后端刷新：重新采集 Agent 硬件信息（与列表页 refresh 一致），再重载各视图
  refreshing.value = true
  try {
    await api.post(`/nodes/${nodeId}/refresh`)
  } catch (e) {
    error.value = String(e)
  } finally {
    refreshing.value = false
  }
  await Promise.all([loadNode(), loadMetrics(), loadSmi(), loadModels()])
}

watch(range, loadMetrics)

onMounted(() => {
  refreshAll()
  rt.on('metrics', onMetrics)
  const t = setInterval(() => {
    // WS 已连接时指标由推送实时追加，轮询仅作降级兜底
    if (autoload.value && !rt.connected.value) loadMetrics()
  }, 15000)
  onUnmounted(() => {
    clearInterval(t)
    rt.off('metrics', onMetrics)
  })
})

// 物理口标注（DGX Spark 官方布局：2×QSFP，每口 2 个 PCIe 通道）
const QSFP_KEYS: Record<string, string> = {
  enp1s0f0np0: 'nodes.qsfp_left_0',
  enP2p1s0f0np0: 'nodes.qsfp_left_1',
  enp1s0f1np1: 'nodes.qsfp_right_0',
  enP2p1s0f1np1: 'nodes.qsfp_right_1',
  enP7s7: 'nodes.mgmt_port',
  wlP9s9: 'nodes.wifi',
}
function qsfpLabel(name: string | undefined): string {
  if (!name) return '—'
  const key = QSFP_KEYS[name]
  return key ? t(key) : name
}
</script>

<template>
  <div>
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center gap-3">
        <UButton size="sm" variant="ghost" icon="i-lucide-arrow-left" to="/nodes">{{ $t('common.back') }}</UButton>
        <h1 class="text-xl font-bold">{{ node?.name || $t('nodes.detail_title') }}</h1>
        <UBadge v-if="node" :color="node.agent_status === 'online' ? 'success' : 'error'" variant="subtle">
          {{ statusLabel(node.agent_status) }}
        </UBadge>
        <span class="text-sm text-gray-500">{{ node?.ip }}:{{ node?.agent_port }}</span>
      </div>
      <div class="flex gap-2">
        <UButton size="sm" variant="outline" :loading="refreshing" @click="refreshAll">{{ $t('common.refresh') }}</UButton>
      </div>
    </div>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />

    <template v-if="node?.hardware_info">
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
        <UCard>
          <div class="text-sm font-semibold mb-2">{{ $t('nodes.sys') }}</div>
          <dl class="text-sm space-y-1.5">
            <div class="flex justify-between"><dt class="text-gray-500">{{ $t('nodes.hostname') }}</dt><dd>{{ node.hardware_info.hostname }}</dd></div>
            <div class="flex justify-between"><dt class="text-gray-500">{{ $t('nodes.os') }}</dt><dd>{{ node.hardware_info.os }}</dd></div>
            <div class="flex justify-between"><dt class="text-gray-500">{{ $t('nodes.arch') }}</dt><dd>{{ node.hardware_info.arch }}</dd></div>
            <div class="flex justify-between"><dt class="text-gray-500">{{ $t('nodes.uptime') }}</dt><dd>{{ Math.floor((node.hardware_info.uptime_seconds || 0) / 3600) }}h</dd></div>
            <div class="flex justify-between"><dt class="text-gray-500">Docker</dt><dd>{{ node.hardware_info.docker?.version || '—' }}</dd></div>
          </dl>
        </UCard>
        <UCard>
          <div class="text-sm font-semibold mb-2">{{ $t('nodes.cpu_mem') }}</div>
          <dl class="text-sm space-y-1.5">
            <div class="flex justify-between"><dt class="text-gray-500">CPU</dt><dd>{{ node.hardware_info.cpu?.model }}</dd></div>
            <div class="flex justify-between"><dt class="text-gray-500">{{ $t('nodes.cores') }}</dt><dd>{{ $t('nodes.cores_value', { physical: node.hardware_info.cpu?.physical_cores, logical: node.hardware_info.cpu?.logical_cores }) }}</dd></div>
            <div class="flex justify-between"><dt class="text-gray-500">{{ $t('nodes.system_mem') }}</dt><dd>{{ fmtBytes(node.hardware_info.memory?.total) }}</dd></div>
            <div class="flex justify-between"><dt class="text-gray-500">{{ $t('nodes.unified_mem') }}</dt><dd>{{ fmtBytes(node.hardware_info.unified_memory?.total) }}</dd></div>
          </dl>
        </UCard>
        <UCard>
          <div class="text-sm font-semibold mb-2">{{ $t('nodes.gpu_count_label', { count: node.hardware_info.gpus?.length || 0 }) }}</div>
          <div v-for="g in node.hardware_info.gpus" :key="g.index" class="text-sm mb-1.5">
            <div class="font-medium">{{ g.name }}</div>
            <div class="text-gray-500 text-xs">
              <template v-if="g.memory_total">{{ fmtBytes(g.memory_total * 1024 * 1024) }}</template>
              <template v-else>{{ $t('nodes.gpu_shared_mem', { size: fmtBytes(node.hardware_info.unified_memory?.total) }) }}</template>
              · SM{{ g.compute_cap }} · {{ g.driver_version }}
            </div>
          </div>
        </UCard>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mt-4">
        <UCard>
          <div class="text-sm font-semibold mb-2">{{ $t('nodes.disk') }}</div>
          <div v-for="d in node.hardware_info.disks" :key="d.mount" class="mb-2">
            <div class="flex justify-between text-xs text-gray-500 mb-0.5">
              <span>{{ d.mount }}</span><span>{{ d.percent }}% · {{ fmtBytes(d.used) }} / {{ fmtBytes(d.total) }}</span>
            </div>
            <UProgress
              :model-value="d.percent"
              :color="d.percent > 85 ? 'error' : 'primary'"
              size="sm"
            />
          </div>
        </UCard>
        <UCard>
          <div class="flex items-center justify-between mb-2">
            <div class="text-sm font-semibold">{{ $t('nodes.roce') }}</div>
          </div>
          <div class="text-[11px] text-gray-400 mb-1.5">
            {{ $t('nodes.roce_hint') }}
          </div>
          <div class="space-y-1 mb-1.5">
            <div v-for="r in node.hardware_info.roce" :key="r.hca" class="text-sm mb-1">
              <div class="font-medium">{{ r.hca }} <span class="text-gray-400 text-xs">{{ r.rate }}</span></div>
              <div class="text-gray-400 text-xs pl-1">
                {{ $t('nodes.roce_row', { netdev: r.netdev || '—', qsfp: qsfpLabel(r.netdev), gid_index: r.gid_index, ip: r.rocev2_ip || '—' }) }}
              </div>
            </div>
          </div>
          <div v-if="!node.hardware_info.roce?.length" class="text-sm text-gray-500">{{ $t('nodes.no_roce') }}</div>
        </UCard>
        <UCard>
          <div class="text-sm font-semibold mb-2">{{ $t('nodes.netdev') }}</div>
          <div v-for="i in node.hardware_info.interfaces" :key="i.name" class="text-xs text-gray-500 mb-1">
            <div>{{ qsfpLabel(i.name) }} <span class="font-mono">{{ i.name }}</span>
              <span v-if="i.pci" class="text-gray-400"> · PCIe {{ i.pci }}</span>
            </div>
            <div class="pl-4">{{ i.ipv4?.join(', ') || '—' }} · {{ i.speed_mbps ? `${i.speed_mbps / 1000}G` : '' }} · {{ i.up ? 'up' : 'down' }}</div>
          </div>
        </UCard>
      </div>
    </template>

    <UCard class="mt-4">
      <div class="flex items-center justify-between mb-3">
        <div class="text-sm font-semibold">{{ $t('nodes.realtime_metrics') }}</div>
        <div class="flex items-center gap-3">
          <USelectMenu value-key="value"
            v-model="range"
            :items="[{ label: $t('nodes.range_1h'), value: 3600 }, { label: $t('nodes.range_6h'), value: 21600 }, { label: $t('nodes.range_24h'), value: 86400 }]"
            class="w-36"
          />
          <label class="flex items-center gap-1.5 text-sm text-gray-500">
            <UCheckbox v-model="autoload" /> {{ $t('nodes.autorefresh') }}
          </label>
        </div>
      </div>
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <UCard><div class="text-xs text-gray-500 mb-1">{{ $t('nodes.chart_temp') }}</div><ClientOnly><MetricChart :option="tempsOption" /></ClientOnly></UCard>
        <UCard><div class="text-xs text-gray-500 mb-1">{{ $t('nodes.chart_cpu') }}</div><ClientOnly><MetricChart :option="cpuOption" /></ClientOnly></UCard>
        <UCard><div class="text-xs text-gray-500 mb-1">{{ $t('nodes.chart_gpu') }}</div><ClientOnly><MetricChart :option="gpuOption" /></ClientOnly></UCard>
        <UCard><div class="text-xs text-gray-500 mb-1">{{ $t('nodes.chart_mem') }}</div><ClientOnly><MetricChart :option="memOption" /></ClientOnly></UCard>
        <UCard><div class="text-xs text-gray-500 mb-1">{{ $t('nodes.chart_disk') }}</div><ClientOnly><MetricChart :option="diskOption" /></ClientOnly></UCard>
        <UCard><div class="text-xs text-gray-500 mb-1">{{ $t('nodes.chart_net') }}</div><ClientOnly><MetricChart :option="netOption" /></ClientOnly></UCard>
      </div>
    </UCard>

    <UCard class="mt-4">
      <div class="text-sm font-semibold mb-2">{{ $t('nodes.node_models', { count: nodeModels.length }) }}</div>
      <div v-if="!nodeModels.length" class="text-sm text-gray-400 py-2">{{ $t('nodes.no_node_models') }}</div>
      <div v-for="m in nodeModels" :key="m.repo" class="flex items-center justify-between py-1.5 border-b border-gray-100 dark:border-gray-800/60 last:border-0">
        <div>
          <div class="font-mono text-xs">{{ m.repo }}</div>
          <div class="text-xs text-gray-500">{{ fmtBytes(m.size_bytes) }}{{ m.snapshot ? ` · ${m.snapshot.slice(0, 8)}` : '' }}</div>
        </div>
        <UButton size="xs" variant="ghost" color="error" @click="removeModel(m.repo)">{{ $t('common.delete') }}</UButton>
      </div>
    </UCard>

    <UCard class="mt-4">
      <div class="flex items-center justify-between mb-2">
        <div class="text-sm font-semibold">nvidia-smi</div>
        <UButton size="xs" variant="ghost" @click="loadSmi">{{ $t('nodes.reload') }}</UButton>
      </div>
      <pre class="bg-gray-50 dark:bg-gray-900 rounded-md p-3 text-xs overflow-x-auto whitespace-pre">{{ smi }}</pre>
    </UCard>
  </div>
</template>
