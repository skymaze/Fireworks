<script setup lang="ts">
const { t } = useI18n()
const route = useRoute()
const api = useApi()
const confirm = useConfirmDialog()
const clusterId = Number(route.params.id)

const cluster = ref<any>(null)
const plan = ref<any>(null)
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
const editForm = reactive({ name: '', description: '', network_type: 'roce', master_port: 25000 })
const saving = ref(false)

// 添加成员
const showAddMember = ref(false)
const addForm = reactive({ node_id: 0, role: 'worker', node_rank: 0, configure_network: true })

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
  return `${base}.${(m.node_rank ?? 0) + 10}`
}

async function load() {
  try {
    cluster.value = await api.get(`/clusters/${clusterId}`)
    plan.value = await api.get(`/clusters/${clusterId}/plan`)
    Object.assign(editForm, {
      name: cluster.value.name,
      description: cluster.value.description || '',
      network_type: cluster.value.network_type,
      master_port: cluster.value.master_port,
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

async function updateMember(m: any, field: 'role' | 'node_rank', value: unknown) {
  await api.patch(`/clusters/${clusterId}/nodes/${m.node_id}`, { [field]: value })
  await load()
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

function nodeName(id: number) {
  return allNodes.value.find((n: any) => n.id === id)?.name ?? id
}

function iperfSummary(r: any) {
  const d = r.data
  if (!d) return ''
  const end = d.end
  const sum = end?.sum_received
  return t('clusters.iperf_summary', { bw: (sum?.bits_per_second / 1e9).toFixed(2), dur: end?.sum_received?.seconds?.toFixed(1), to: r.to })
}

onMounted(load)
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
          <USelect
            v-model="editForm.network_type"
            :items="[
              { label: $t('clusters.net_roce'), value: 'roce' },
              { label: 'InfiniBand', value: 'ib' },
              { label: $t('clusters.net_ethernet'), value: 'ethernet' },
            ]"
          />
        </UFormField>
        <UFormField :label="$t('clusters.master_port')"><UInput v-model.number="editForm.master_port" type="number" /></UFormField>
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
              <th class="py-2 pr-4 font-medium">{{ $t('clusters.col_role') }}</th>
              <th class="py-2 pr-4 font-medium">node_rank</th>
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
              <td class="py-2.5 pr-4">
                <USelect
                  :model-value="m.role"
                  :items="[{ label: 'Head', value: 'head' }, { label: 'Worker', value: 'worker' }]"
                  class="w-28"
                  @update:model-value="(v: any) => updateMember(m, 'role', v)"
                />
              </td>
              <td class="py-2.5 pr-4">
                <UInput
                  :model-value="String(m.node_rank)"
                  type="number"
                  class="w-24"
                  @update:model-value="(v: any) => updateMember(m, 'node_rank', Number(v))"
                />
              </td>
              <td class="py-2.5 pr-4 font-mono text-xs text-gray-500">{{ memberIp(m, 'enp1s0f0np0') }}</td>
              <td class="py-2.5 text-right">
                <UButton size="xs" variant="ghost" color="error" @click="removeMember(m)">{{ $t('clusters.remove_member') }}</UButton>
              </td>
            </tr>
            <tr v-if="!cluster.members.length">
              <td colspan="6" class="py-8 text-center text-gray-400">{{ $t('clusters.no_members') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
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
            <li>{{ $t('clusters.plan_note_3', { bak: '.fw-bak-*' }) }}</li>
            <li>{{ $t('clusters.plan_note_4') }}</li>
          </ul>
        </div>
      </div>
    </UCard>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4">
      <UCard v-if="plan">
        <div class="text-sm font-semibold mb-2">{{ $t('clusters.auto_vars') }}</div>
        <dl class="text-sm space-y-1.5">
          <div v-for="(v, k) in plan.cluster_vars" :key="k" class="flex justify-between">
            <dt class="text-gray-500 font-mono text-xs">{{ k }}</dt>
            <dd class="font-mono text-xs">{{ v ?? '—' }}</dd>
          </div>
        </dl>
        <div class="mt-3 text-xs text-gray-500">{{ $t('clusters.auto_vars_hint') }}</div>
      </UCard>

      <UCard>
        <template #header><div class="font-semibold">{{ $t('clusters.network_test') }}</div></template>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
          <UFormField :label="$t('clusters.from_node')">
            <USelect
              v-model="testForm.from_node_id"
              :items="cluster?.members.map((m: any) => ({ label: m.node?.name || String(m.node_id), value: m.node_id })) || []"
            />
          </UFormField>
          <UFormField :label="$t('clusters.to_node')">
            <USelect
              v-model="testForm.to_node_id"
              :items="cluster?.members.map((m: any) => ({ label: m.node?.name || String(m.node_id), value: m.node_id })) || []"
            />
          </UFormField>
          <UFormField :label="$t('clusters.tool')">
            <USelect
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
            <USelect
              v-model="addForm.node_id"
              :items="addableNodes.map((n: any) => ({ label: `${n.name} (${n.ip})`, value: n.id }))"
            />
            <p v-if="otherOccupied.length" class="text-xs text-gray-400 mt-1">
              {{ $t('clusters.occupied_note', { list: otherOccupied.map((n: any) => n.name).join($t('common.list_sep')) }) }}
            </p>
          </UFormField>
          <div class="grid grid-cols-2 gap-4">
            <UFormField :label="$t('clusters.col_role')">
              <USelect
                v-model="addForm.role"
                :items="[{ label: 'Head', value: 'head' }, { label: 'Worker', value: 'worker' }]"
              />
            </UFormField>
            <UFormField label="node_rank">
              <UInput v-model.number="addForm.node_rank" type="number" />
            </UFormField>
          </div>
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
