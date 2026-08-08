<script setup lang="ts">
const { t } = useI18n()
const route = useRoute()
const api = useApi()
const confirm = useConfirmDialog()
const clusterId = Number(route.params.id)

const cluster = ref<any>(null)
const allNodes = ref<any[]>([])
const error = ref('')
const notice = ref('')

// 可添加节点：排除本集群成员 + 已加入其他集群的节点（cluster_id 非空即占用）
const addableNodes = computed(() => {
  const mine = new Set((cluster.value?.members || []).map((m: any) => m.node_id))
  return allNodes.value.filter((n: any) => !mine.has(n.id) && !n.cluster_id)
})
const otherOccupied = computed(() =>
  allNodes.value.filter((n: any) => n.cluster_id && !(cluster.value?.members || []).some((m: any) => m.node_id === n.id))
)

// 编辑
const editForm = reactive({ name: '', description: '', network_type: 'roce' })
const saving = ref(false)

// 添加成员
const showAddMember = ref(false)
const addForm = reactive({ node_id: 0, configure_network: true })

// 网络测试
const testForm = reactive({ from_node_id: 0, to_node_id: 0, tool: 'iperf3', duration: 10 })
const testResult = ref<any>(null)
const testing = ref(false)

// 网络规划（用于展示接口子网映射与成员 IP）
function memberIp(m: any, iface: string): string {
  const plan = cluster.value?.network_plan
  if (!plan?.iface_subnets) return '—'
  const subnet = plan.iface_subnets[iface]
  if (!subnet) return '—'
  const base = subnet.split('/')[0].split('.').slice(0, 3).join('.')
  // 高速网 IP 按成员 net_index 槽位分配（node_ips 规则：末位 = .9 + net_index）
  return `${base}.${(m.net_index ?? 0) + 9}`
}

async function load() {
  try {
    cluster.value = await api.get(`/clusters/${clusterId}`)
    Object.assign(editForm, {
      name: cluster.value.name,
      description: cluster.value.description || '',
      network_type: cluster.value.network_type,
    })
    const memberIds = new Set(cluster.value.members.map((m: any) => m.node_id))
    allNodes.value = await api.get('/nodes')
    if (!testForm.from_node_id && cluster.value.members.length) {
      testForm.from_node_id = cluster.value.members[0].node_id
      testForm.to_node_id = cluster.value.members[0].node_id
    }
    const selectable = allNodes.value.filter((n: any) => !memberIds.has(n.id))
    if (selectable.length) addForm.node_id = selectable[0].id
    error.value = ''
  } catch (e) {
    error.value = String(e)
  }
}

async function saveCluster() {
  saving.value = true
  try {
    await api.patch(`/clusters/${clusterId}`, editForm)
    notice.value = t('clusters.saved')
    await load()
  } catch (e) {
    error.value = String(e)
  } finally {
    saving.value = false
  }
}

async function addMember() {
  try {
    await api.post(`/clusters/${clusterId}/nodes`, addForm)
    showAddMember.value = false
    await load()
  } catch (e) {
    error.value = String(e)
  }
}

async function removeMember(m: any) {
  const ok = await confirm.open({ title: t('clusters.remove_title'), description: t('clusters.remove_confirm', { name: m.node?.name }) })
  if (!ok) return
  await api.del(`/clusters/${clusterId}/nodes/${m.node_id}`)
  await load()
}

async function runTest() {
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await api.post(`/clusters/${clusterId}/network-test`, {
      from_node_id: testForm.from_node_id,
      to_node_id: testForm.to_node_id,
      tool: testForm.tool,
      duration: testForm.duration,
    })
  } catch (e) {
    testResult.value = { error: String(e) }
  } finally {
    testing.value = false
  }
}

function iperfSummary(r: any) {
  const d = r.data
  if (!d) return ''
  const end = d.end
  const sum = end?.sum_received
  return t('clusters.iperf_summary', { bw: (sum?.bits_per_second / 1e9).toFixed(2), dur: end?.sum_received?.seconds?.toFixed(1), to: r.to })
}

// ---------- 集群实时监控（复用 metrics WS 事件，按成员过滤，秒级并排更新） ----------
const rt = useRealtime()
const monRange = ref(3600)
const monLive = ref<Record<number, any[]>>({})
const monHistory = ref<Record<number, any[]>>({})
const monMeta = ref<Record<number, any>>({})
const clusterOverview = ref<any>(null)

const monMemberIds = computed(() => (cluster.value?.members || []).map((m: any) => m.node_id))

async function loadClusterOverview() {
  try {
    clusterOverview.value = await api.get(`/clusters/${clusterId}/overview`)
  } catch (e) {
    // 可选功能，失败不影响主页面
  }
}

async function loadClusterMetrics() {
  if (!monMemberIds.value.length) return
  try {
    const to = Date.now() / 1000
    const data = await api.get(`/clusters/${clusterId}/metrics`, {
      from_ts: to - monRange.value, to_ts: to, limit: 1000,
    })
    const hist: Record<number, any[]> = {}
    const meta: Record<number, any> = {}
    for (const m of data.members || []) {
      hist[m.node_id] = m.series
      meta[m.node_id] = { name: m.node_name, status: m.agent_status }
    }
    monHistory.value = hist
    monMeta.value = meta
  } catch (e) {
    // 可选功能
  }
}

function onClusterMetrics(msg: any) {
  if (!monMemberIds.value.includes(msg.node_id)) return
  const d = msg.data || {}
  const row = {
    ts: d.ts || Date.now() / 1000,
    cpu: d.cpu_percent,
    mem_percent: d.memory?.percent,
    gpu_util: d.gpu?.utilization,
    gpu_mem_used: d.gpu?.mem_used,
    gpu_mem_total: d.gpu?.mem_total,
    temp: d.temperatures?.cpu,
    gpu_temp: d.gpus?.[0]?.temperature,
    net_rx: d.network?.rx_bps,
    net_tx: d.network?.tx_bps,
  }
  const arr = monLive.value[msg.node_id] || (monLive.value[msg.node_id] = [])
  arr.push(row)
  if (arr.length > 2000) monLive.value[msg.node_id] = arr.slice(-2000)
}

function mergedSeries(nodeId: number): any[] {
  const map = new Map<number, any>()
  for (const r of monHistory.value[nodeId] || []) map.set(r.ts, r)
  for (const r of monLive.value[nodeId] || []) map.set(r.ts, r)
  return [...map.values()].sort((a: any, b: any) => a.ts - b.ts)
}

function miniLine(x: string[], data: any[], yName: string, max?: number) {
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 42, right: 8, top: 18, bottom: 18 },
    xAxis: { type: 'category', data: x, show: false },
    yAxis: { type: 'value', name: yName, ...(max != null ? { max } : { scale: true }) },
    series: [{ type: 'line', smooth: true, showSymbol: false, lineStyle: { width: 1 }, data }],
  }
}

const monOptions = computed(() => {
  const opts: Record<number, any> = {}
  for (const nid of monMemberIds.value) {
    const rows = mergedSeries(nid)
    const x = rows.map((r) => fmtTime(r.ts))
    opts[nid] = {
      gpu: miniLine(x, rows.map((r) => r.gpu_util ?? null), t('clusters.mon_gpu_short'), 100),
      temp: miniLine(x, rows.map((r) => r.gpu_temp ?? r.temp ?? null), '°C'),
      mem: miniLine(x, rows.map((r) => (r.gpu_mem_used ?? 0) / 1e9), 'GB'),
      net: miniLine(x, rows.map((r) => (r.net_rx ?? 0) / 1024 / 1024), 'MB/s'),
    }
  }
  return opts
})

const hasMonData = computed(() => monMemberIds.value.some((id) => mergedSeries(id).length > 0))

onMounted(() => {
  load()
  rt.on('metrics', onClusterMetrics)
  loadClusterOverview()
  loadClusterMetrics()
  const it = setInterval(() => {
    // WS 已连接由推送驱动；断线降级轮询
    if (!rt.connected.value) loadClusterMetrics()
  }, 15000)
  onUnmounted(() => {
    clearInterval(it)
    rt.off('metrics', onClusterMetrics)
  })
})

watch(monRange, loadClusterMetrics)
// 成员增删后刷新监控数据
watch(() => cluster.value?.members?.length, () => {
  loadClusterOverview()
  loadClusterMetrics()
})
</script>

<template>
  <div>
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center gap-3">
        <UButton size="sm" variant="ghost" to="/clusters">{{ $t('common.back') }}</UButton>
        <h1 class="text-xl font-bold">{{ cluster?.name || $t('clusters.detail_title') }}</h1>
        <UBadge variant="subtle">{{ cluster?.network_type }}</UBadge>
      </div>
    </div>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />
    <UAlert v-if="notice" :title="notice" color="success" class="mb-4" />

    <UCard v-if="cluster">
      <template #header><div class="font-semibold">{{ $t('clusters.basic_info') }}</div></template>
      <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
        <UFormField :label="$t('common.name')"><UInput v-model="editForm.name" /></UFormField>
        <UFormField :label="$t('common.description')"><UInput v-model="editForm.description" /></UFormField>
        <UFormField :label="$t('clusters.network_type')">
          <USelectMenu value-key="value"
            v-model="editForm.network_type"
            :items="[
              { label: $t('clusters.net_roce'), value: 'roce' },
              { label: 'InfiniBand', value: 'ib' },
              { label: $t('clusters.net_ethernet'), value: 'ethernet' },
            ]"
          />
        </UFormField>
      </div>
      <div class="flex justify-end mt-3">
        <UButton size="sm" :loading="saving" @click="saveCluster">{{ $t('common.save') }}</UButton>
      </div>
    </UCard>

    <UCard v-if="cluster" class="mt-4">
      <template #header>
        <div class="flex items-center justify-between">
          <div class="font-semibold">{{ $t('clusters.members', { count: cluster.members.length }) }}</div>
          <UButton size="xs" color="primary" @click="showAddMember = true">{{ $t('nodes.add_node') }}</UButton>
        </div>
      </template>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
              <th class="py-2 pr-4 font-medium">{{ $t('clusters.col_node') }}</th>
              <th class="py-2 pr-4 font-medium">IP</th>
              <th class="py-2 pr-4 font-medium">{{ $t('clusters.col_net_slot') }}</th>
              <th class="py-2 pr-4 font-medium">{{ $t('clusters.col_hs_ip') }}</th>
              <th class="py-2 font-medium text-right">{{ $t('common.actions') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in cluster.members" :key="m.id" class="border-b border-gray-100 dark:border-gray-800/60">
              <td class="py-2.5 pr-4">
                <NuxtLink :to="`/nodes/${m.node_id}`" class="font-medium hover:underline">{{ m.node?.name || m.node_id }}</NuxtLink>
              </td>
              <td class="py-2.5 pr-4 text-gray-500">{{ m.node?.ip || '—' }}</td>
              <td class="py-2.5 pr-4 font-mono text-xs text-gray-500">#{{ m.net_index ?? '—' }}</td>
              <td class="py-2.5 pr-4 font-mono text-xs text-gray-500">{{ memberIp(m, 'enp1s0f0np0') }}</td>
              <td class="py-2.5 text-right">
                <UButton size="xs" variant="ghost" color="error" @click="removeMember(m)">{{ $t('clusters.remove_member') }}</UButton>
              </td>
            </tr>
            <tr v-if="!cluster.members.length">
              <td colspan="5" class="py-8 text-center text-gray-400">{{ $t('clusters.no_members') }}</td>
            </tr>
          </tbody>
        </table>
        <div class="text-xs text-gray-400 mt-2">{{ $t('clusters.role_per_task_note') }}</div>
      </div>
    </UCard>

    <UCard v-if="cluster" class="mt-4">
      <template #header>
        <div class="flex items-center justify-between">
          <div class="font-semibold">{{ $t('clusters.mon_title') }}</div>
          <USelectMenu value-key="value"
            v-model="monRange"
            :items="[{ label: '1h', value: 3600 }, { label: '6h', value: 21600 }, { label: '24h', value: 86400 }]"
            class="w-24"
          />
        </div>
      </template>

      <template v-if="clusterOverview">
        <div class="grid grid-cols-2 lg:grid-cols-5 gap-4 mb-4">
          <div class="text-center">
            <div class="text-2xl font-bold">{{ clusterOverview.nodes_online }}/{{ clusterOverview.nodes_total }}</div>
            <div class="text-xs text-gray-500">{{ $t('clusters.mon_online') }}</div>
          </div>
          <div class="text-center">
            <div class="text-2xl font-bold">{{ clusterOverview.gpu_util_avg != null ? clusterOverview.gpu_util_avg + '%' : '—' }}</div>
            <div class="text-xs text-gray-500">{{ $t('clusters.mon_gpu_util') }}</div>
          </div>
          <div class="text-center">
            <div class="text-2xl font-bold">{{ clusterOverview.cpu_temp_avg != null ? clusterOverview.cpu_temp_avg + '°C' : '—' }}</div>
            <div class="text-xs text-gray-500">{{ $t('clusters.mon_temp') }}</div>
          </div>
          <div class="text-center">
            <div class="text-2xl font-bold">{{ clusterOverview.gpu_mem_total ? fmtBytes(clusterOverview.gpu_mem_used) + ' / ' + fmtBytes(clusterOverview.gpu_mem_total) : '—' }}</div>
            <div class="text-xs text-gray-500">{{ $t('clusters.mon_mem') }}</div>
          </div>
          <div class="text-center">
            <div class="text-2xl font-bold">{{ fmtSpeed((clusterOverview.net_rx_bps || 0) + (clusterOverview.net_tx_bps || 0)) }}</div>
            <div class="text-xs text-gray-500">{{ $t('clusters.mon_net') }}</div>
          </div>
        </div>
      </template>

      <div v-if="cluster.members.length && hasMonData" class="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <div v-for="m in cluster.members" :key="m.node_id" class="border dark:border-gray-800 rounded-lg p-3">
          <div class="flex items-center justify-between mb-2">
            <NuxtLink :to="`/nodes/${m.node_id}`" class="font-medium text-sm hover:underline">{{ m.node?.name || m.node_id }}</NuxtLink>
            <div class="flex items-center gap-2">
              <UBadge :color="(monMeta[m.node_id]?.status || 'unknown') === 'online' ? 'success' : 'error'" variant="subtle">
                {{ statusLabel(monMeta[m.node_id]?.status || 'unknown') }}
              </UBadge>
            </div>
          </div>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <div>
              <div class="text-[11px] text-gray-400">{{ $t('clusters.mon_gpu_short') }} %</div>
              <ClientOnly><MetricChart :option="monOptions[m.node_id]?.gpu" height="120px" /></ClientOnly>
            </div>
            <div>
              <div class="text-[11px] text-gray-400">{{ $t('clusters.mon_temp_short') }} °C</div>
              <ClientOnly><MetricChart :option="monOptions[m.node_id]?.temp" height="120px" /></ClientOnly>
            </div>
            <div>
              <div class="text-[11px] text-gray-400">{{ $t('clusters.mon_mem_short') }} GB</div>
              <ClientOnly><MetricChart :option="monOptions[m.node_id]?.mem" height="120px" /></ClientOnly>
            </div>
            <div>
              <div class="text-[11px] text-gray-400">{{ $t('clusters.mon_net_short') }} MB/s</div>
              <ClientOnly><MetricChart :option="monOptions[m.node_id]?.net" height="120px" /></ClientOnly>
            </div>
          </div>
        </div>
      </div>
      <p v-else class="text-sm text-gray-500">{{ $t('clusters.mon_no_data') }}</p>
    </UCard>

    <UCard v-if="cluster?.network_plan" class="mt-4">
      <template #header><div class="font-semibold">{{ $t('clusters.network_plan') }}</div></template>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <div class="text-sm text-gray-500 mb-2">
            {{ $t('clusters.plan_summary', { cidr: cluster.network_plan.cidr, mtu: cluster.network_plan.mtu }) }}
          </div>
          <table class="w-full text-xs">
            <thead>
              <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
                <th class="py-1.5 pr-4 font-medium">{{ $t('clusters.col_iface') }}</th>
                <th class="py-1.5 pr-4 font-medium">{{ $t('clusters.col_subnet') }}</th>
                <th class="py-1.5 font-medium">{{ $t('clusters.col_member_ips') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(subnet, iface) in cluster.network_plan.iface_subnets"
                :key="iface"
                class="border-b border-gray-100 dark:border-gray-800/60"
              >
                <td class="py-1.5 pr-4 font-mono">{{ iface }}</td>
                <td class="py-1.5 pr-4 font-mono">{{ subnet }}</td>
                <td class="py-1.5 font-mono text-gray-500">
                  {{ cluster.members.map((m: any) => `${m.node?.name || m.node_id}: ${memberIp(m, iface as string)}`).join($t('common.list_sep')) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div>
          <div class="text-sm text-gray-500 mb-2">{{ $t('clusters.plan_notes') }}</div>
          <ul class="text-xs text-gray-500 space-y-1.5 list-disc pl-4">
            <li>{{ $t('clusters.plan_note_1', { start: '.10', reserved: '.1-.4' }) }}</li>
            <li>{{ $t('clusters.plan_note_2') }}</li>
            <li>{{ $t('clusters.plan_note_3') }}</li>
            <li>{{ $t('clusters.plan_note_4') }}</li>
          </ul>
        </div>
      </div>
    </UCard>

    <div class="mt-4">
      <UCard>
        <template #header><div class="font-semibold">{{ $t('clusters.network_test') }}</div></template>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
          <UFormField :label="$t('clusters.from_node')">
            <USelectMenu value-key="value"
              v-model="testForm.from_node_id"
              :items="cluster?.members.map((m: any) => ({ label: m.node?.name || String(m.node_id), value: m.node_id })) || []"
            />
          </UFormField>
          <UFormField :label="$t('clusters.to_node')">
            <USelectMenu value-key="value"
              v-model="testForm.to_node_id"
              :items="cluster?.members.map((m: any) => ({ label: m.node?.name || String(m.node_id), value: m.node_id })) || []"
            />
          </UFormField>
          <UFormField :label="$t('clusters.tool')">
            <USelectMenu value-key="value"
              v-model="testForm.tool"
              :items="[
                { label: 'iperf3', value: 'iperf3' },
                { label: 'ib_write_bw', value: 'ib_write_bw' },
                { label: 'ib_read_bw', value: 'ib_read_bw' },
                { label: 'ping', value: 'ping' },
              ]"
            />
          </UFormField>
          <UFormField :label="$t('clusters.duration')">
            <UInput v-model.number="testForm.duration" type="number" />
          </UFormField>
        </div>
        <div class="flex justify-end mt-3">
          <UButton size="sm" color="primary" :loading="testing" @click="runTest">{{ $t('clusters.start_test') }}</UButton>
        </div>
        <div v-if="testResult" class="mt-3">
          <div class="text-xs text-gray-500 mb-1">
            {{ testResult.from }} → {{ testResult.to }} · {{ testResult.tool }}
            <span v-if="testResult.tool === 'iperf3' && testResult.data" class="font-medium text-primary">{{ iperfSummary(testResult) }}</span>
          </div>
          <pre class="bg-gray-50 dark:bg-gray-900 rounded-md p-3 text-xs overflow-x-auto whitespace-pre max-h-72">{{ testResult.error || testResult.output || JSON.stringify(testResult, null, 2) }}</pre>
        </div>
      </UCard>
    </div>

    <UModal v-model:open="showAddMember">
      <template #content>
        <UCard>
        <template #header><div class="font-semibold">{{ $t('clusters.add_member_title') }}</div></template>
        <div class="space-y-4">
          <UFormField :label="$t('clusters.col_node')">
            <USelectMenu value-key="value"
              v-model="addForm.node_id"
              :items="addableNodes.map((n: any) => ({ label: `${n.name} (${n.ip})`, value: n.id }))"
            />
            <p v-if="otherOccupied.length" class="text-xs text-gray-400 mt-1">
              {{ $t('clusters.occupied_note', { list: otherOccupied.map((n: any) => n.name).join($t('common.list_sep')) }) }}
            </p>
          </UFormField>
          <UFormField v-if="cluster?.network_plan" :label="$t('clusters.net_config')">
            <UCheckbox
              v-model="addForm.configure_network"
              :label="$t('clusters.configure_network_label')"
            />
          </UFormField>
        </div>
        <template #footer>
          <div class="flex justify-end gap-2">
            <UButton variant="outline" @click="showAddMember = false">{{ $t('common.cancel') }}</UButton>
            <UButton color="primary" :disabled="!addForm.node_id" @click="addMember">{{ $t('clusters.add') }}</UButton>
          </div>
        </template>
      </UCard>
      </template>
    </UModal>
  </div>
</template>
