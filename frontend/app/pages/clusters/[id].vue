<script setup lang="ts">
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
    notice.value = '已保存'
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
  const ok = await confirm.open({ title: '移出节点', description: `将节点「${m.node?.name}」移出集群？` })
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
  return `带宽 ${(sum?.bits_per_second / 1e9).toFixed(2)} Gbps · ${end?.sum_received?.seconds?.toFixed(1)}s · ${r.to} 中继`
}

onMounted(load)
</script>

<template>
  <div>
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center gap-3">
        <UButton size="sm" variant="ghost" to="/clusters">返回</UButton>
        <h1 class="text-xl font-bold">{{ cluster?.name || '集群详情' }}</h1>
        <UBadge variant="subtle">{{ cluster?.network_type }}</UBadge>
      </div>
    </div>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />
    <UAlert v-if="notice" :title="notice" color="success" class="mb-4" />

    <UCard v-if="cluster">
      <template #header><div class="font-semibold">基本信息</div></template>
      <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
        <UFormField label="名称"><UInput v-model="editForm.name" /></UFormField>
        <UFormField label="描述"><UInput v-model="editForm.description" /></UFormField>
        <UFormField label="网络类型">
          <USelect
            v-model="editForm.network_type"
            :items="[
              { label: 'RoCE (高速)', value: 'roce' },
              { label: 'InfiniBand', value: 'ib' },
              { label: '以太网', value: 'ethernet' },
            ]"
          />
        </UFormField>
        <UFormField label="主端口"><UInput v-model.number="editForm.master_port" type="number" /></UFormField>
      </div>
      <div class="flex justify-end mt-3">
        <UButton size="sm" :loading="saving" @click="saveCluster">保存</UButton>
      </div>
    </UCard>

    <UCard v-if="cluster" class="mt-4">
      <template #header>
        <div class="flex items-center justify-between">
          <div class="font-semibold">集群成员（{{ cluster.members.length }}）</div>
          <UButton size="xs" color="primary" @click="showAddMember = true">添加节点</UButton>
        </div>
      </template>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
              <th class="py-2 pr-4 font-medium">节点</th>
              <th class="py-2 pr-4 font-medium">IP</th>
              <th class="py-2 pr-4 font-medium">角色</th>
              <th class="py-2 pr-4 font-medium">node_rank</th>
              <th class="py-2 pr-4 font-medium">高速网 IP</th>
              <th class="py-2 font-medium text-right">操作</th>
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
                <UButton size="xs" variant="ghost" color="error" @click="removeMember(m)">移除</UButton>
              </td>
            </tr>
            <tr v-if="!cluster.members.length">
              <td colspan="6" class="py-8 text-center text-gray-400">暂无成员</td>
            </tr>
          </tbody>
        </table>
      </div>
    </UCard>

    <UCard v-if="cluster?.network_plan" class="mt-4">
      <template #header><div class="font-semibold">高速网络规划</div></template>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <div class="text-sm text-gray-500 mb-2">
            网段 <span class="font-mono text-gray-800 dark:text-gray-200">{{ cluster.network_plan.cidr }}</span>
            · MTU <span class="font-mono text-gray-800 dark:text-gray-200">{{ cluster.network_plan.mtu }}</span>
            · 接口按官方 4×100G PCIe 通道分配独立 /24 子网
          </div>
          <table class="w-full text-xs">
            <thead>
              <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
                <th class="py-1.5 pr-4 font-medium">接口</th>
                <th class="py-1.5 pr-4 font-medium">子网</th>
                <th class="py-1.5 font-medium">各成员 IP</th>
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
                  {{ cluster.members.map((m: any) => `${m.node?.name || m.node_id}: ${memberIp(m, iface as string)}`).join('，') }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div>
          <div class="text-sm text-gray-500 mb-2">说明</div>
          <ul class="text-xs text-gray-500 space-y-1.5 list-disc pl-4">
            <li>IP 从 <span class="font-mono">.10</span> 起按 node_rank 递增分配，避开 NVIDIA 官方工具占用的 <span class="font-mono">.1-.4</span>。</li>
            <li>同一接口在所有成员上处于同一 /24 网段（同 rail），跨节点 RDMA 直连。</li>
            <li>配置写入官方 netplan（自动备份 <span class="font-mono">.fw-bak-*</span>），删除集群可选清理恢复。</li>
            <li>新加入节点按集群规划自动分配 IP（同接口同网段）并验证通过后才加入。</li>
          </ul>
        </div>
      </div>
    </UCard>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4">
      <UCard v-if="plan">
        <div class="text-sm font-semibold mb-2">自动填充变量（发布时生效）</div>
        <dl class="text-sm space-y-1.5">
          <div v-for="(v, k) in plan.cluster_vars" :key="k" class="flex justify-between">
            <dt class="text-gray-500 font-mono text-xs">{{ k }}</dt>
            <dd class="font-mono text-xs">{{ v ?? '—' }}</dd>
          </div>
        </dl>
        <div class="mt-3 text-xs text-gray-500">各节点自动变量（RoCE IP / HCA / 网卡 / GID）在发布向导中展示并可覆盖。</div>
      </UCard>

      <UCard>
        <template #header><div class="font-semibold">网络测试</div></template>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
          <UFormField label="起始节点">
            <USelect
              v-model="testForm.from_node_id"
              :items="cluster?.members.map((m: any) => ({ label: m.node?.name || String(m.node_id), value: m.node_id })) || []"
            />
          </UFormField>
          <UFormField label="目标节点">
            <USelect
              v-model="testForm.to_node_id"
              :items="cluster?.members.map((m: any) => ({ label: m.node?.name || String(m.node_id), value: m.node_id })) || []"
            />
          </UFormField>
          <UFormField label="工具">
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
          <UFormField label="时长 (s)">
            <UInput v-model.number="testForm.duration" type="number" />
          </UFormField>
        </div>
        <div class="flex justify-end mt-3">
          <UButton size="sm" color="primary" :loading="testing" @click="runTest">开始测试</UButton>
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
        <template #header><div class="font-semibold">添加节点到集群</div></template>
        <div class="space-y-4">
          <UFormField label="节点">
            <USelect
              v-model="addForm.node_id"
              :items="addableNodes.map((n: any) => ({ label: `${n.name} (${n.ip})`, value: n.id }))"
            />
            <p v-if="otherOccupied.length" class="text-xs text-gray-400 mt-1">
              以下节点已加入其他集群，一个节点只能属于一个集群，不可选择：
              {{ otherOccupied.map((n: any) => n.name).join('、') }}
            </p>
          </UFormField>
          <div class="grid grid-cols-2 gap-4">
            <UFormField label="角色">
              <USelect
                v-model="addForm.role"
                :items="[{ label: 'Head', value: 'head' }, { label: 'Worker', value: 'worker' }]"
              />
            </UFormField>
            <UFormField label="node_rank">
              <UInput v-model.number="addForm.node_rank" type="number" />
            </UFormField>
          </div>
          <UFormField v-if="cluster?.network_plan" label="高速网络配置">
            <UCheckbox
              v-model="addForm.configure_network"
              label="按集群规划配置高速网络并验证（同接口同网段，失败自动回滚）"
            />
          </UFormField>
        </div>
        <template #footer>
          <div class="flex justify-end gap-2">
            <UButton variant="outline" @click="showAddMember = false">取消</UButton>
            <UButton color="primary" :disabled="!addForm.node_id" @click="addMember">添加</UButton>
          </div>
        </template>
      </UCard>
      </template>
    </UModal>
  </div>
</template>
