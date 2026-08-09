<script setup lang="ts">
import { errorMsg } from '~/composables/useApi'
const { t } = useI18n()
const api = useApi()
const toast = useToast()
const clusters = ref<any[]>([])
const nodes = ref<any[]>([])
const error = ref('')
const showAdd = ref(false)
const submitting = ref(false)
const form = reactive({
  name: '',
  description: '',
  network_type: 'roce',
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
const delActiveAlert = computed(() => t('clusters.has_active_tasks', {
  list: delActiveTasks.value.map((x: any) => `#${x.id} ${x.name} · ${statusLabel(x.status)}`).join(t('common.list_sep')),
}))
const delDoneAlert = computed(() => t('clusters.has_done_tasks', {
  count: delDoneTasks.value.length,
  list: delDoneTasks.value.map((x: any) => `#${x.id} ${x.name}`).join(t('common.list_sep')),
}))
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
      node_ids: [],
      network_cidr: '10.0.0.0/16',
      network_mtu: 9000,
    })
    await load()
  } catch (e: any) {
    const msg = errorMsg(e)
    // 网段被占用：更新为后端可用网段并提示（不通过才更新）
    if ((e as any)?.data?.detail?.code === 'cidr_conflict') {
      const free = await fetchAvailableCidr()
      if (free) {
        form.network_cidr = free
        toast.add({ title: t('clusters.cidr_auto_fixed', { msg, free }), color: 'success' })
      } else {
        error.value = t('clusters.cidr_no_available')
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
  return c ? t('clusters.node_in_cluster', { name: c.name }) : t('clusters.node_in_other_cluster')
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
    const parts = [t('clusters.deleted', { name: delTarget.value.name })]
    if (r?.cleaned_nodes?.length) parts.push(t('clusters.cleaned_nodes', { count: r.cleaned_nodes.length }))
    if (r?.warnings?.length) parts.push(t('clusters.warning_list', { warn: r.warnings.join(t('common.semi_sep')) }))
    toast.add({ title: parts.join(t('common.semi_sep')), color: 'success' })
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
  <UDashboardPanel id="clusters">
    <template #header>
      <UDashboardNavbar :toggle="false" :title="$t('clusters.title')">
        <template #right>
          <UButton color="primary" @click="showAdd = true">{{ $t('clusters.create') }}</UButton>
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
    <div>
      <UAlert v-if="error" :title="error" color="error" class="mb-4" />

      <UCard>
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
                <th class="py-2 pr-4 font-medium">{{ $t('common.name') }}</th>
                <th class="py-2 pr-4 font-medium">{{ $t('clusters.network_type') }}</th>
                <th class="py-2 pr-4 font-medium">{{ $t('clusters.node_count') }}</th>
                <th class="py-2 pr-4 font-medium">{{ $t('common.description') }}</th>
                <th class="py-2 font-medium text-right">{{ $t('common.actions') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="c in clusters" :key="c.id" class="border-b border-gray-100 dark:border-gray-800/60">
                <td class="py-2.5 pr-4">
                  <NuxtLink :to="`/clusters/${c.id}`" class="font-medium hover:underline">{{ c.name }}</NuxtLink>
                </td>
                <td class="py-2.5 pr-4"><UBadge variant="subtle">{{ c.network_type }}</UBadge></td>
                <td class="py-2.5 pr-4">{{ c.members?.length || 0 }}</td>
                <td class="py-2.5 pr-4 text-gray-500 truncate max-w-[220px]">{{ c.description || '—' }}</td>
                <td class="py-2.5 text-right whitespace-nowrap">
                  <UButton size="xs" variant="ghost" :to="`/clusters/${c.id}`">{{ $t('common.detail') }}</UButton>
                  <UButton size="xs" variant="ghost" color="error" @click="removeCluster(c)">{{ $t('common.delete') }}</UButton>
                </td>
              </tr>
              <tr v-if="!clusters.length">
                <td colspan="6" class="py-8 text-center text-gray-400">{{ $t('clusters.empty') }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </UCard>

      <UModal v-model:open="showAdd">
        <template #content>
          <UCard>
          <template #header><div class="font-semibold">{{ $t('clusters.create') }}</div></template>
          <div class="space-y-4 max-h-[60vh] overflow-y-auto pr-1">
            <UFormField :label="$t('common.name')">
              <UInput v-model="form.name" placeholder="dgx-spark-01" :disabled="submitting" />
            </UFormField>
            <UFormField :label="$t('common.description')">
              <UInput v-model="form.description" :placeholder="$t('clusters.description_optional')" :disabled="submitting" />
            </UFormField>
            <UFormField :label="$t('clusters.member_nodes')">
              <div class="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-56 overflow-y-auto border border-gray-200 dark:border-gray-800 rounded-md p-2">
                <label
                  v-for="n in nodes"
                  :key="n.id"
                  :class="[
                    'flex items-center gap-2 px-2 py-1.5 rounded-md',
                    submitting || occupiedNodeIds.has(n.id)
                      ? 'opacity-50 cursor-not-allowed'
                      : 'cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/60',
                  ]"
                >
                  <UCheckbox
                    :model-value="form.node_ids.includes(n.id)"
                    :disabled="submitting || occupiedNodeIds.has(n.id)"
                    @update:model-value="toggleNode(n.id)"
                  />
                  <span class="text-sm">{{ n.name }}</span>
                  <span class="text-xs text-gray-400">{{ n.ip }}</span>
                  <span v-if="nodeOccupied(n)" class="text-xs text-gray-400">（{{ nodeOccupied(n) }}）</span>
                </label>
                <div v-if="!nodes.length" class="col-span-2 py-4 text-center text-gray-400 text-sm">{{ $t('clusters.no_available_nodes') }}</div>
              </div>
            </UFormField>
            <div class="grid grid-cols-2 gap-4">
              <UFormField :label="$t('clusters.network_type')">
                <USelectMenu value-key="value"
                  v-model="form.network_type"
                  :disabled="submitting"
                  :items="[
                    { label: $t('clusters.net_roce'), value: 'roce' },
                    { label: 'InfiniBand', value: 'ib' },
                    { label: $t('clusters.net_ethernet'), value: 'ethernet' },
                  ]"
                />
              </UFormField>
            </div>
            <div class="grid grid-cols-2 gap-4">
              <UFormField :label="$t('clusters.cidr')">
                <UInput v-model="form.network_cidr" placeholder="10.0.0.0/16" :disabled="submitting" />
              </UFormField>
              <UFormField label="MTU">
                <UInput v-model.number="form.network_mtu" type="number" :disabled="submitting" />
              </UFormField>
            </div>
          </div>
          <template #footer>
            <div class="flex justify-end gap-2">
              <UButton variant="outline" :disabled="submitting" @click="showAdd = false">{{ $t('common.cancel') }}</UButton>
              <UButton
                color="primary"
                :loading="submitting"
                :disabled="submitting || !form.name || !form.node_ids.length"
                @click="addCluster"
              >{{ $t('clusters.create_configure') }}</UButton>
            </div>
          </template>
        </UCard>
        </template>
      </UModal>

      <UModal v-model:open="delOpen">
        <template #content>
          <UCard>
            <template #header><div class="font-semibold">{{ $t('clusters.delete_title') }}</div></template>
            <p class="text-sm">{{ $t('clusters.delete_confirm', { name: delTarget?.name }) }}</p>
            <UAlert
              v-if="delActiveTasks.length"
              :title="delActiveAlert"
              color="error"
              class="mt-3"
            />
            <UAlert
              v-else-if="delDoneTasks.length"
              :title="delDoneAlert"
              color="warning"
              class="mt-3"
            />
            <UCheckbox v-model="delCleanup" :label="$t('clusters.cleanup_network')" class="mt-3" />
            <template #footer>
              <div class="flex justify-end gap-2">
                <UButton variant="outline" @click="delTarget = null">{{ $t('common.cancel') }}</UButton>
                <UButton color="error" :loading="deleting" :disabled="delActiveTasks.length > 0" @click="confirmDelete">{{ $t('clusters.delete') }}</UButton>
              </div>
            </template>
          </UCard>
        </template>
      </UModal>
    </div>
    </template>
  </UDashboardPanel>
</template>
