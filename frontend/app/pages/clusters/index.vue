<script setup lang="ts">
import { errorMsg } from '~/composables/useApi'
const api = useApi()
const clusters = ref<any[]>([])
const nodes = ref<any[]>([])
const error = ref('')
const notice = ref('')
const showAdd = ref(false)
const submitting = ref(false)
const form = reactive({
  name: '',
  description: '',
  network_type: 'roce',
  master_port: 25000,
  node_ids: [] as number[],
  network_cidr: '10.0.0.0/16',
  network_mtu: 9000,
})

// 删除确认（含任务校验 + 节点网络清理选项）
const delTarget = ref<any>(null)
const delTasks = ref<any[]>([])
const delCleanup = ref(false)
const deleting = ref(false)
// 未停止（published/running/paused）任务：须先停止才能删集群；已结束任务可 force
const ACTIVE_STATUSES = ['published', 'running', 'paused']
const delActiveTasks = computed(() => delTasks.value.filter((t: any) => ACTIVE_STATUSES.includes(t.status)))
const delDoneTasks = computed(() => delTasks.value.filter((t: any) => !ACTIVE_STATUSES.includes(t.status)))
const delOpen = computed({
  get: () => delTarget.value != null,
  set: (v: boolean) => { if (!v) delTarget.value = null },
})

// 已加入集群的节点（一节点一集群：cluster_id 非空即占用，来自 nodes API）
const occupiedNodeIds = computed(() => {
  const ids = new Set<number>()
  for (const n of nodes.value) if (n.cluster_id) ids.add(n.id)
  return ids
})

// 从后端获取当前可用高速网网段（10.0.0.0/16 起自增；无可用时抛错）
async function fetchAvailableCidr(): Promise<string | null> {
  try {
    const r = await api.get('/clusters/available-cidr')
    return r?.cidr || null
  } catch {
    return null
  }
}

async function load() {
  try {
    clusters.value = await api.get('/clusters')
    nodes.value = await api.get('/nodes')
    error.value = ''
  } catch (e) {
    error.value = String(e)
  }
}

async function addCluster() {
  submitting.value = true
  error.value = ''
  try {
    await api.post('/clusters', form)
    showAdd.value = false
    Object.assign(form, {
      name: '',
      description: '',
      network_type: 'roce',
      master_port: 25000,
      node_ids: [],
      network_cidr: '10.0.0.0/16',
      network_mtu: 9000,
    })
    await load()
  } catch (e) {
    const msg = errorMsg(e)
    // 网段被占用：更新为后端可用网段并提示（不通过才更新）
    if (msg.includes('网段') && msg.includes('占用')) {
      const free = await fetchAvailableCidr()
      if (free) {
        form.network_cidr = free
        notice.value = `网段被占用：${msg}；已自动更新为可用网段 ${free}，请重新提交`
      } else {
        error.value = '无可用高速网网段（10.x 均被占用），请手动设置网段'
      }
    } else {
      error.value = msg
    }
  } finally {
    submitting.value = false
  }
}

// 打开创建弹窗时从后端获取可用网段填入
watch(showAdd, async (open) => {
  if (open) {
    error.value = ''
    notice.value = ''
    const free = await fetchAvailableCidr()
    if (free) form.network_cidr = free
  }
})

function toggleNode(id: number) {
  if (occupiedNodeIds.value.has(id)) return
  const i = form.node_ids.indexOf(id)
  if (i >= 0) form.node_ids.splice(i, 1)
  else form.node_ids.push(id)
}

function nodeOccupied(n: any): string | null {
  if (!n.cluster_id) return null
  const c = clusters.value.find((c: any) => c.id === n.cluster_id)
  return c ? `已在集群「${c.name}」` : '已在其他集群'
}

async function removeCluster(c: any) {
  delTarget.value = c
  delCleanup.value = false
  delTasks.value = []
  try {
    const tasks = await api.get('/tasks')
    delTasks.value = (tasks || []).filter((t: any) => t.cluster_id === c.id)
  } catch { /* ignore */ }
}

async function confirmDelete() {
  if (!delTarget.value) return
  deleting.value = true
  error.value = ''
  try {
    // 有任务时经确认即视为 force（任务将失去集群引用）
    const force = delTasks.value.length ? 1 : 0
    const r = await api.del(`/clusters/${delTarget.value.id}?force=${force}&cleanup_network=${delCleanup.value ? 1 : 0}`)
    const parts = [`已删除集群「${delTarget.value.name}」`]
    if (r?.cleaned_nodes?.length) parts.push(`${r.cleaned_nodes.length} 个节点高速网络配置已清理`)
    if (r?.warnings?.length) parts.push(`警告：${r.warnings.join('；')}`)
    notice.value = parts.join('；')
    delTarget.value = null
    await load()
  } catch (e) {
    error.value = errorMsg(e)
  } finally {
    deleting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="flex items-center justify-between mb-4">
      <h1 class="text-xl font-bold">集群管理</h1>
      <UButton color="primary" @click="showAdd = true">创建集群</UButton>
    </div>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />
    <UAlert v-if="notice" :title="notice" color="success" class="mb-4" />

    <UCard>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
              <th class="py-2 pr-4 font-medium">名称</th>
              <th class="py-2 pr-4 font-medium">网络类型</th>
              <th class="py-2 pr-4 font-medium">主端口</th>
              <th class="py-2 pr-4 font-medium">节点数</th>
              <th class="py-2 pr-4 font-medium">描述</th>
              <th class="py-2 font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in clusters" :key="c.id" class="border-b border-gray-100 dark:border-gray-800/60">
              <td class="py-2.5 pr-4">
                <NuxtLink :to="`/clusters/${c.id}`" class="font-medium hover:underline">{{ c.name }}</NuxtLink>
              </td>
              <td class="py-2.5 pr-4"><UBadge variant="subtle">{{ c.network_type }}</UBadge></td>
              <td class="py-2.5 pr-4">{{ c.master_port }}</td>
              <td class="py-2.5 pr-4">{{ c.members?.length || 0 }}</td>
              <td class="py-2.5 pr-4 text-gray-500 truncate max-w-[220px]">{{ c.description || '—' }}</td>
              <td class="py-2.5 text-right whitespace-nowrap">
                <UButton size="xs" variant="ghost" :to="`/clusters/${c.id}`">详情</UButton>
                <UButton size="xs" variant="ghost" color="error" @click="removeCluster(c)">删除</UButton>
              </td>
            </tr>
            <tr v-if="!clusters.length">
              <td colspan="6" class="py-8 text-center text-gray-400">暂无集群</td>
            </tr>
          </tbody>
        </table>
      </div>
    </UCard>

    <UModal v-model:open="showAdd">
      <template #content>
        <UCard>
        <template #header><div class="font-semibold">创建集群</div></template>
        <div class="space-y-4 max-h-[60vh] overflow-y-auto pr-1">
          <UFormField label="名称">
            <UInput v-model="form.name" placeholder="dgx-spark-01" />
          </UFormField>
          <UFormField label="描述">
            <UInput v-model="form.description" placeholder="可选" />
          </UFormField>
          <UFormField label="成员节点">
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-56 overflow-y-auto border border-gray-200 dark:border-gray-800 rounded-md p-2">
              <label
                v-for="n in nodes"
                :key="n.id"
                :class="[
                  'flex items-center gap-2 px-2 py-1.5 rounded-md cursor-pointer',
                  occupiedNodeIds.has(n.id)
                    ? 'opacity-50 cursor-not-allowed hover:bg-transparent'
                    : 'hover:bg-gray-50 dark:hover:bg-gray-800/60',
                ]"
              >
                <UCheckbox
                  :model-value="form.node_ids.includes(n.id)"
                  :disabled="occupiedNodeIds.has(n.id)"
                  @update:model-value="toggleNode(n.id)"
                />
                <span class="text-sm">{{ n.name }}</span>
                <span class="text-xs text-gray-400">{{ n.ip }}</span>
                <span v-if="nodeOccupied(n)" class="text-xs text-gray-400">（{{ nodeOccupied(n) }}）</span>
              </label>
              <div v-if="!nodes.length" class="col-span-2 py-4 text-center text-gray-400 text-sm">暂无可用节点</div>
            </div>
          </UFormField>
          <div class="grid grid-cols-2 gap-4">
            <UFormField label="网络类型">
              <USelect
                v-model="form.network_type"
                :items="[
                  { label: 'RoCE (高速)', value: 'roce' },
                  { label: 'InfiniBand', value: 'ib' },
                  { label: '以太网', value: 'ethernet' },
                ]"
              />
            </UFormField>
            <UFormField label="分布式主端口">
              <UInput v-model.number="form.master_port" type="number" />
            </UFormField>
          </div>
          <div class="grid grid-cols-2 gap-4">
            <UFormField label="高速网网段（CIDR）">
              <UInput v-model="form.network_cidr" placeholder="10.0.0.0/16" />
            </UFormField>
            <UFormField label="MTU">
              <UInput v-model.number="form.network_mtu" type="number" />
            </UFormField>
          </div>
        </div>
        <template #footer>
          <div class="flex justify-end gap-2">
            <UButton variant="outline" @click="showAdd = false">取消</UButton>
            <UButton
              color="primary"
              :loading="submitting"
              :disabled="!form.name || !form.node_ids.length"
              @click="addCluster"
            >创建并配置网络</UButton>
          </div>
        </template>
      </UCard>
      </template>
    </UModal>

    <UModal v-model:open="delOpen">
      <template #content>
        <UCard>
          <template #header><div class="font-semibold">删除集群</div></template>
          <p class="text-sm">确认删除集群「{{ delTarget?.name }}」？</p>
          <UAlert
            v-if="delActiveTasks.length"
            :title="`集群下存在未停止的任务（${delActiveTasks.map((t: any) => `#${t.id} ${t.name}（${t.status}）`).join('、')}）。请先在任务详情停止这些任务后再删除集群。`"
            color="error"
            class="mt-3"
          />
          <UAlert
            v-else-if="delDoneTasks.length"
            :title="`集群下存在 ${delDoneTasks.length} 个已结束任务（${delDoneTasks.map((t: any) => `#${t.id} ${t.name}`).join('、')}）。确认删除后这些任务将失去集群引用。`"
            color="warning"
            class="mt-3"
          />
          <UCheckbox v-model="delCleanup" label="同时清理节点高速网络配置（10.100.x RoCE 接口；SSH + sudo，netplan 自动备份 .bak-*）" class="mt-3" />
          <template #footer>
            <div class="flex justify-end gap-2">
              <UButton variant="outline" @click="delTarget = null">取消</UButton>
              <UButton color="error" :loading="deleting" :disabled="delActiveTasks.length > 0" @click="confirmDelete">确认删除</UButton>
            </div>
          </template>
        </UCard>
      </template>
    </UModal>
  </div>
</template>
