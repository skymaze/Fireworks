<script setup lang="ts">
import { errorMsg } from '~/composables/useApi'
const { t } = useI18n()
const api = useApi()
const toast = useToast()
const clusters = ref<any[]>([])
const nodes = ref<any[]>([])
const showAdd = ref(false)
const submitting = ref(false)
const detectingNetwork = ref(false)
const detectedNetwork = ref<{ cidr: string, mtu: number } | null>(null)
const reconfigureNetwork = ref<{ networks: string, suggested: string } | null>(null)
const networkPreflight = ref<any>(null)
let networkDetectSeq = 0
let networkDetectTimer: ReturnType<typeof setTimeout> | null = null
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
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

async function addCluster() {
  submitting.value = true
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
    detectedNetwork.value = null
    reconfigureNetwork.value = null
    networkPreflight.value = null
    await load()
  } catch (e: any) {
    const msg = errorMsg(e)
    // 网段被占用：更新为后端可用网段并提示（不通过才更新）
    const detail = (e as any)?.data?.detail
    if (detail?.code === 'network_reconfig_cidr_conflict' && detail?.params?.suggested) {
      form.network_cidr = detail.params.suggested
      toast.add({
        title: t('clusters.reconfigure_cidr_fixed', { msg, suggested: detail.params.suggested }),
        color: 'warning',
      })
    } else if (detail?.code === 'network_ip_conflict' && detail?.params?.suggested) {
      form.network_cidr = detail.params.suggested
      toast.add({
        title: t('clusters.cidr_auto_fixed', { msg, free: detail.params.suggested }),
        color: 'warning',
      })
      void detectSelectedNetwork()
    } else if (detail?.code === 'cidr_conflict') {
      const free = await fetchAvailableCidr()
      if (free) {
        form.network_cidr = free
        toast.add({ title: t('clusters.cidr_auto_fixed', { msg, free }), color: 'success' })
      } else {
        toast.add({ title: t('clusters.cidr_no_available'), color: 'error' })
      }
    } else {
      toast.add({ title: msg, color: 'error' })
    }
  } finally {
    submitting.value = false
  }
}

async function detectSelectedNetwork() {
  const seq = ++networkDetectSeq
  detectedNetwork.value = null
  reconfigureNetwork.value = null
  networkPreflight.value = null
  if (!form.node_ids.length) return
  detectingNetwork.value = true
  try {
    const r: any = await api.post('/clusters/detect-network', {
      node_ids: [...form.node_ids],
      network_cidr: form.network_cidr,
      network_mtu: form.network_mtu,
    })
    if (seq !== networkDetectSeq) return
    if (!r?.detected && r?.suggested_cidr && r.suggested_cidr !== form.network_cidr) {
      form.network_cidr = r.suggested_cidr
    }
    networkPreflight.value = { physical: r?.physical, ipCheck: r?.ip_check, error: null }
    if (r?.detected) {
      form.network_cidr = r.cidr
      form.network_mtu = r.mtu
      detectedNetwork.value = { cidr: r.cidr, mtu: r.mtu }
    } else if (r?.mode === 'reconfigure' && r?.suggested_cidr) {
      form.network_cidr = r.suggested_cidr
      reconfigureNetwork.value = {
        networks: (r.networks || []).map((network: any) => network.cidr).join(t('common.list_sep')),
        suggested: r.suggested_cidr,
      }
    }
  } catch (e) {
    if (seq !== networkDetectSeq) return
    networkPreflight.value = { physical: null, ipCheck: null, error: errorMsg(e) }
  } finally {
    if (seq === networkDetectSeq) detectingNetwork.value = false
  }
}

function scheduleSelectedNetworkDetection() {
  if (networkDetectTimer) clearTimeout(networkDetectTimer)
  networkDetectTimer = setTimeout(() => {
    networkDetectTimer = null
    void detectSelectedNetwork()
  }, 350)
}

// 打开创建弹窗时从后端获取可用网段填入
watch(showAdd, async (open) => {
  if (open) {
    const free = await fetchAvailableCidr()
    if (free) form.network_cidr = free
  } else if (networkDetectTimer) {
    clearTimeout(networkDetectTimer)
    networkDetectTimer = null
  }
})

function toggleNode(id: number) {
  if (occupiedNodeIds.value.has(id)) return
  const i = form.node_ids.indexOf(id)
  if (i >= 0) form.node_ids.splice(i, 1)
  else form.node_ids.push(id)
  scheduleSelectedNetworkDetection()
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
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    deleting.value = false
  }
}

onMounted(load)
</script>

<template>
  <UDashboardPanel id="clusters">
    <template #header>
      <UDashboardNavbar :title="$t('clusters.title')">
        <template #leading>
          <UDashboardSidebarCollapse />
        </template>

        <template #right>
          <UButton color="primary" @click="showAdd = true">{{ $t('clusters.create') }}</UButton>
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
    <div>

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

      <UModal v-model:open="showAdd" :title="$t('clusters.create')" scrollable>
        <template #body>
          <div class="space-y-4">
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
                <UInput v-model="form.network_cidr" placeholder="10.0.0.0/16" :disabled="submitting" @change="detectSelectedNetwork" />
              </UFormField>
              <UFormField label="MTU">
                <UInput v-model.number="form.network_mtu" type="number" :disabled="submitting" @change="detectSelectedNetwork" />
              </UFormField>
              <UAlert
                v-if="detectedNetwork"
                class="col-span-2"
                color="success"
                variant="subtle"
                :title="$t('clusters.existing_network_detected', detectedNetwork)"
              />
              <UAlert
                v-else-if="reconfigureNetwork"
                class="col-span-2"
                color="warning"
                variant="subtle"
                :title="$t('clusters.mixed_networks_detected', reconfigureNetwork)"
              />
              <div v-else-if="detectingNetwork" class="col-span-2 text-xs text-gray-500">
                {{ $t('clusters.detecting_network') }}
              </div>
              <UAlert
                v-if="networkPreflight?.error"
                class="col-span-2"
                color="error"
                variant="subtle"
                :title="$t('clusters.preflight_failed', { detail: networkPreflight.error })"
              />
              <UAlert
                v-else-if="networkPreflight?.physical"
                class="col-span-2"
                :color="networkPreflight.physical.ok ? (networkPreflight.physical.status === 'verified' ? 'success' : 'warning') : 'error'"
                variant="subtle"
                :title="networkPreflight.physical.ok
                  ? (networkPreflight.physical.status === 'verified'
                    ? $t('clusters.physical_verified', { count: networkPreflight.physical.links?.length || 0 })
                    : $t('clusters.physical_partial', { detail: networkPreflight.physical.warnings?.join($t('common.semi_sep')) }))
                  : $t('clusters.physical_failed', { detail: networkPreflight.physical.issues?.join($t('common.semi_sep')) })"
              />
              <UAlert
                v-if="networkPreflight?.ipCheck"
                class="col-span-2"
                :color="networkPreflight.ipCheck.ok ? 'success' : 'error'"
                variant="subtle"
                :title="networkPreflight.ipCheck.ok
                  ? $t('clusters.ip_available', { cidr: networkPreflight.ipCheck.cidr })
                  : $t('clusters.ip_conflict', {
                    detail: networkPreflight.ipCheck.error || networkPreflight.ipCheck.conflicts?.map((x: any) => `${x.node} ${x.iface} ${x.ip}: ${x.reason}`).join($t('common.semi_sep')),
                  })"
              />
            </div>
          </div>
        </template>
        <template #footer>
          <div class="flex w-full justify-end gap-2">
            <UButton variant="outline" :disabled="submitting" @click="showAdd = false">{{ $t('common.cancel') }}</UButton>
            <UButton
              color="primary"
              :loading="submitting"
              :disabled="submitting || detectingNetwork || !form.name || !form.node_ids.length || !!networkPreflight?.error || networkPreflight?.physical?.ok === false || networkPreflight?.ipCheck?.ok === false"
              @click="addCluster"
            >{{ $t(detectedNetwork
              ? 'clusters.create_reuse'
              : reconfigureNetwork
                ? 'clusters.create_reconfigure'
                : 'clusters.create_configure') }}</UButton>
          </div>
        </template>
      </UModal>

      <UModal v-model:open="delOpen" :title="$t('clusters.delete_title')">
        <template #body>
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
        </template>
        <template #footer>
          <div class="flex w-full justify-end gap-2">
            <UButton variant="outline" @click="delTarget = null">{{ $t('common.cancel') }}</UButton>
            <UButton color="error" :loading="deleting" :disabled="delActiveTasks.length > 0" @click="confirmDelete">{{ $t('clusters.delete') }}</UButton>
          </div>
        </template>
      </UModal>
    </div>
    </template>
  </UDashboardPanel>
</template>
