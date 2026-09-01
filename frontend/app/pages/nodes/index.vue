<script setup lang="ts">
const { t } = useI18n()
const api = useApi()
const toast = useToast()
const confirm = useConfirmDialog()
const nodes = ref<any[]>([])
const loading = ref(false)
const deployingIds = ref(new Set<number>())

// 多选：勾选目标节点后，通过批量操作栏统一重装/升级 Agent 或执行初始优化
const selected = ref(new Set<number>())
const batchBusy = ref<'deploy' | 'optimize' | null>(null)
const showBatchResult = ref(false)
const batchResults = ref<any[]>([])

const showAdd = ref(false)
const form = reactive({
  name: '',
  ip: '',
  ssh_port: 22,
  ssh_username: 'root',
  ssh_auth_type: 'password',
  ssh_password: '',
  ssh_key: '',
  agent_port: 9000,
  optimize_on_add: true,
})
const submitting = ref(false)

async function load() {
  loading.value = true
  try {
    nodes.value = await api.get('/nodes')
    // 清理已不存在节点的勾选（批量操作期间节点可能被删除/刷新）
    const alive = new Set(nodes.value.map((n: any) => n.id))
    const keep = [...selected.value].filter((id) => alive.has(id))
    if (keep.length !== selected.value.size) selected.value = new Set(keep)
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    loading.value = false
  }
}

async function addNode() {
  submitting.value = true
  try {
    // 添加节点即安装 Agent（后端原子操作：安装/验证失败会报错并回滚）；
    // 默认同时执行初始优化（关闭无线/GUI、授予 docker、关闭 swap，失败仅警告）。
    const n = await api.post('/nodes', form)
    showAdd.value = false
    const opt = n?.optimize_result
    toast.add({
      title: t('nodes.deploy_success', { version: n?.hardware_info?.agent_version || '?' }) +
        (opt?.summary ? ` · ${opt.summary}` : ''),
      description: optimizeWarnings(opt),
      color: 'success',
    })
    Object.assign(form, {
      name: '', ip: '', ssh_port: 22, ssh_username: 'root',
      ssh_auth_type: 'password', ssh_password: '', ssh_key: '',
      agent_port: 9000, optimize_on_add: true,
    })
    await load()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    submitting.value = false
  }
}

// 初始优化：手动对（含旧节点）补跑，best-effort；结果落 optimize_result。
const optimizingIds = ref(new Set<number>())

// 优化结果的警告文案（有失败项/提示时展示，否则 undefined）
function optimizeWarnings(opt: any): string | undefined {
  if (!opt) return undefined
  const failed = (opt.steps || []).filter((s: any) => !s.ok)
  const notes: string[] = []
  if (failed.length) {
    notes.push(failed.map((s: any) => `${s.detail || s.key}`).join('；'))
  }
  if ((opt.warnings || []).length) notes.push(...opt.warnings)
  return notes.length ? notes.join('；') : undefined
}

async function optimizeNode(n: any) {
  // 已优化节点不允许重复执行（按钮已禁用，此处为防御性拦截）
  if (optimizeState(n) === 'ok') return
  if (optimizingIds.value.has(n.id)) return
  const ok = await confirm.open({
    title: t('nodes.optimize'),
    description: t('nodes.optimize_confirm', { name: n.name }),
  })
  if (!ok) return
  optimizingIds.value.add(n.id)
  try {
    const r = await api.post(`/nodes/${n.id}/optimize`)
    toast.add({
      title: r.ok ? t('nodes.optimize_done', { name: n.name, summary: r.summary || '' })
        : t('nodes.optimize_fail', { name: n.name, error: r.summary || r.error || t('common.unknown_error') }),
      description: optimizeWarnings(r),
      color: r.ok ? 'success' : 'warning',
    })
    await load()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    optimizingIds.value.delete(n.id)
  }
}

// Agent 安装始终可用，当前版本与目标版本的关系只决定按钮文案。
async function installAgent(n: any) {
  const action = agentDeployAction(n)
  if (deployingIds.value.has(n.id)) return
  const ok = await confirm.open({
    title: t(agentDeployLabelKey(action)),
    description: t('nodes.reinstall_agent_confirm', { name: n.name }),
  })
  if (!ok) return
  deployingIds.value.add(n.id)
  try {
    const r = await api.post(`/nodes/${n.id}/deploy-agent`)
    toast.add({
      title: r.ok
        ? (r.warning ? t('nodes.deploy_done_warning', { warning: r.warning }) : t('nodes.deploy_success', { version: r.hardware_info?.agent_version || '?' }))
        : t('nodes.deploy_fail', { error: r.error || t('common.unknown_error') }),
      color: r.ok ? 'success' : 'error',
    })
    await load()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    deployingIds.value.delete(n.id)
  }
}

// ---- 多选 / 全选 ----
// 表头三态：全选=checked、部分=indeterminate、未选=false（UCheckbox 原生支持 indeterminate）
const headerChecked = computed<'indeterminate' | boolean>(() => {
  if (!nodes.value.length) return false
  if (nodes.value.every((n: any) => selected.value.has(n.id))) return true
  if (selected.value.size > 0) return 'indeterminate'
  return false
})

function setAllSelected(on: boolean) {
  const next = new Set<number>()
  if (on) nodes.value.forEach((n: any) => next.add(n.id))
  selected.value = next
}

function toggleSelected(id: number, on: boolean) {
  const next = new Set(selected.value)
  if (on) next.add(id)
  else next.delete(id)
  selected.value = next
}

// ---- 批量操作（并行执行，后端逐节点返回结果，互不影响）----
function nodeById(id: number): any {
  return nodes.value.find((n: any) => n.id === id)
}

function openBatchResult(action: 'deploy' | 'optimize', r: any, extra: any[] = []) {
  const results = [...extra, ...(r?.results || [])]
  batchResults.value = results
  showBatchResult.value = true
  const ok = results.filter((x: any) => x.ok).length
  const failed = results.length - ok
  toast.add({
    title: `${t(action === 'deploy' ? 'nodes.batch_deploy' : 'nodes.batch_optimize')} · ${t('nodes.batch_result_summary', { ok, failed })}`,
    color: failed > 0 ? 'warning' : 'success',
  })
}

async function batchInstallAgent() {
  const ids = [...selected.value]
  if (!ids.length || batchBusy.value) return
  const ok = await confirm.open({
    title: t('nodes.batch_deploy'),
    description: t('nodes.batch_deploy_confirm', { count: ids.length }),
  })
  if (!ok) return
  batchBusy.value = 'deploy'
  try {
    const r = await api.post('/nodes/batch/deploy-agent', { node_ids: ids })
    selected.value = new Set()
    openBatchResult('deploy', r)
    await load()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    batchBusy.value = null
  }
}

async function batchOptimize() {
  // 语义与单节点按钮一致：已优化（ok）的节点自动跳过，不重复重启。
  const pending = [...selected.value].filter((id) => optimizeState(nodeById(id)) !== 'ok')
  const skipped = [...selected.value].filter((id) => !pending.includes(id))
  if (!pending.length) {
    toast.add({ title: t('nodes.batch_optimize_nothing'), color: 'neutral' })
    return
  }
  if (batchBusy.value) return
  const ok = await confirm.open({
    title: t('nodes.batch_optimize'),
    description: skipped.length
      ? t('nodes.batch_optimize_confirm_skip', { count: pending.length, skipped: skipped.length })
      : t('nodes.batch_optimize_confirm', { count: pending.length }),
  })
  if (!ok) return
  batchBusy.value = 'optimize'
  const skippedRows = skipped.map((id) => {
    const n = nodeById(id)
    return { node_id: id, node_name: n?.name || `#${id}`, ok: true, skipped: true, summary: t('nodes.optimize_skipped') }
  })
  try {
    const r = await api.post('/nodes/batch/optimize', { node_ids: pending })
    selected.value = new Set()
    openBatchResult('optimize', r, skippedRows)
    await load()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    batchBusy.value = null
  }
}

async function refreshNode(n: any) {
  try {
    await api.post(`/nodes/${n.id}/refresh`)
    toast.add({ title: t('nodes.refreshed', { name: n.name }), color: 'success' })
    await load()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
}

async function removeNode(n: any) {
  Object.assign(delForm, { agent: true, network: true, models: false, images: false })
  pendingDelete.value = n
  showDelete.value = true
}

// 删除节点（可选清理：Agent / 高速网络 / 模型 / 镜像）
const pendingDelete = ref<any>(null)
const showDelete = ref(false)
const delForm = reactive({ agent: true, network: true, models: false, images: false })
const deleting = ref(false)

async function confirmDeleteNode() {
  const n = pendingDelete.value
  if (!n) return
  pendingDelete.value = null
  showDelete.value = false
  deleting.value = true
  try {
    const r = await api.del(`/nodes/${n.id}`, {
      cleanup_agent: delForm.agent,
      cleanup_network: delForm.network,
      cleanup_models: delForm.models,
      cleanup_images: delForm.images,
    })
    toast.add({
      title: t('nodes.deleted_notice', { name: n.name }) +
        ((r?.warnings || []).length ? `（${(r.warnings as string[]).join('；')}）` : ''),
      color: 'success',
    })
    await load()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    deleting.value = false
  }
}

function gpuCount(n: any): number {
  return n.hardware_info?.gpus?.length ?? 0
}

// 初始优化状态：全部成功=ok / 曾失败或不完整=partial / 从未优化=none
function optimizeState(n: any): 'ok' | 'partial' | 'none' {
  const opt = n?.optimize_result
  if (!opt) return 'none'
  const steps = Array.isArray(opt.steps) ? opt.steps : []
  return (opt.ok && steps.every((s: any) => s.ok)) ? 'ok' : 'partial'
}
function optimizeBadgeColor(s: string): 'success' | 'warning' | 'neutral' {
  return s === 'ok' ? 'success' : s === 'partial' ? 'warning' : 'neutral'
}
function optimizeRanAt(n: any): string | undefined {
  const ran = n?.optimize_result?.ran_at
  return ran ? fmtDateTime(ran) : undefined
}

// Agent 版本徽标颜色：过旧=warning / 正常=success / 未知=neutral
function agentBadge(n: any): 'success' | 'warning' | 'neutral' {
  if (agentVersionMismatch(n)) return 'warning'
  if (n.agent_version) return 'success'
  return 'neutral'
}

function agentActionColor(n: any): 'warning' | 'neutral' {
  const action = agentDeployAction(n)
  return action === 'reinstall' ? 'neutral' : 'warning'
}

const statusColorMap: Record<string, 'primary' | 'secondary' | 'success' | 'info' | 'warning' | 'error' | 'neutral'> = {
  online: 'success', offline: 'error', unknown: 'neutral', error: 'error',
}
function statusColor(s: string): 'primary' | 'secondary' | 'success' | 'info' | 'warning' | 'error' | 'neutral' {
  return statusColorMap[s] ?? 'neutral'
}

// 实时节点上下线（WS 连接 + 心跳看门狗秒级判定）：就地更新列表，无需轮询
const rt = useRealtime()
function onNodeStatus(msg: any) {
  const n = nodes.value.find((x) => x.id === msg.node_id)
  if (!n) return
  n.agent_status = msg.status
  if (msg.last_seen) n.last_seen = msg.last_seen
}

onMounted(() => {
  load()
  rt.on('node_status', onNodeStatus)
})
onUnmounted(() => {
  rt.off('node_status', onNodeStatus)
})
</script>

<template>
  <UDashboardPanel id="nodes">
    <template #header>
      <UDashboardNavbar :title="$t('nodes.title')">
        <template #leading>
          <UDashboardSidebarCollapse />
        </template>

        <template #right>
          <UButton color="primary" @click="showAdd = true">{{ $t('nodes.add_node') }}</UButton>
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
    <div>

      <UCard>
        <!-- 批量操作栏：勾选任意节点后出现 -->
        <div
          v-if="selected.size > 0"
          class="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-gray-200 dark:border-gray-800 px-3 py-2"
        >
          <span class="text-sm text-gray-600 dark:text-gray-400">{{ $t('nodes.selected_count', { count: selected.size }) }}</span>
          <div class="flex-1" />
          <UButton
            size="xs"
            color="primary"
            :loading="batchBusy === 'deploy'"
            :disabled="batchBusy !== null"
            @click="batchInstallAgent"
          >{{ $t('nodes.batch_deploy') }}</UButton>
          <UButton
            size="xs"
            variant="ghost"
            :loading="batchBusy === 'optimize'"
            :disabled="batchBusy !== null"
            @click="batchOptimize"
          >{{ $t('nodes.batch_optimize') }}</UButton>
          <UButton size="xs" variant="ghost" :disabled="batchBusy !== null" @click="selected = new Set()">
            {{ $t('nodes.clear_selection') }}
          </UButton>
        </div>

        <div class="overflow-x-auto">
          <!-- 9 列较宽：窄视口下不再挤压换行，改为容器横向滚动 -->
          <table class="w-full text-sm whitespace-nowrap min-w-[960px]">
            <thead>
              <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
                <th class="py-2 pr-3">
                  <UCheckbox :model-value="headerChecked" :aria-label="$t('nodes.select_all')" @update:model-value="setAllSelected" />
                </th>
                <th class="py-2 pr-4 font-medium">{{ $t('common.name') }}</th>
                <th class="py-2 pr-4 font-medium">IP</th>
                <th class="py-2 pr-4 font-medium">{{ $t('nodes.agent_status') }}</th>
                <th class="py-2 pr-4 font-medium">GPU</th>
                <th class="py-2 pr-4 font-medium">{{ $t('nodes.agent_version') }}</th>
                <th class="py-2 pr-4 font-medium">{{ $t('nodes.last_online') }}</th>
                <th class="py-2 pr-4 font-medium">{{ $t('nodes.optimize_status') }}</th>
                <th class="py-2 font-medium text-right">{{ $t('common.actions') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="n in nodes"
                :key="n.id"
                class="border-b border-gray-100 dark:border-gray-800/60"
                :class="selected.has(n.id) ? 'bg-elevated/50' : ''"
              >
                <td class="py-2.5 pr-3">
                  <UCheckbox
                    :model-value="selected.has(n.id)"
                    :aria-label="n.name"
                    @update:model-value="(v: any) => toggleSelected(n.id, !!v)"
                  />
                </td>
                <td class="py-2.5 pr-4">
                  <NuxtLink :to="`/nodes/${n.id}`" class="font-medium hover:underline">{{ n.name }}</NuxtLink>
                </td>
                <td class="py-2.5 pr-4 text-gray-600 dark:text-gray-400">{{ n.ip }}:{{ n.agent_port }}</td>
                <td class="py-2.5 pr-4"><UBadge :color="statusColor(n.agent_status)" variant="subtle">{{ statusLabel(n.agent_status) }}</UBadge></td>
                <td class="py-2.5 pr-4">{{ gpuCount(n) }}</td>
                <td class="py-2.5 pr-4"><UBadge :color="agentBadge(n)" variant="subtle">{{ n.agent_version ? 'v' + n.agent_version : '—' }}</UBadge></td>
                <td class="py-2.5 pr-4 text-gray-500">
                  {{ fmtDateTime(n.last_seen) }}
                </td>
                <td class="py-2.5 pr-4">
                  <UBadge :color="optimizeBadgeColor(optimizeState(n))" variant="subtle" :title="optimizeRanAt(n)">
                    {{ $t('nodes.optimize_status_' + optimizeState(n)) }}
                  </UBadge>
                </td>
                <td class="py-2.5 text-right whitespace-nowrap">
                  <UButton size="xs" color="primary" :to="`/nodes/${n.id}`">{{ $t('common.detail') }}</UButton>
                  <UButton
                    size="xs"
                    variant="ghost"
                    :color="agentActionColor(n)"
                    :loading="deployingIds.has(n.id)"
                    @click="installAgent(n)"
                  >{{ $t(agentDeployLabelKey(agentDeployAction(n))) }}</UButton>
                  <UButton
                    size="xs"
                    variant="ghost"
                    :disabled="optimizeState(n) === 'ok'"
                    :loading="optimizingIds.has(n.id)"
                    @click="optimizeNode(n)"
                  >{{ $t('nodes.optimize') }}</UButton>
                  <UButton size="xs" variant="ghost" @click="refreshNode(n)">{{ $t('common.refresh') }}</UButton>
                  <UButton size="xs" variant="ghost" color="error" @click="removeNode(n)">{{ $t('common.delete') }}</UButton>
                </td>
              </tr>
              <tr v-if="!nodes.length">
                <td colspan="9" class="py-8 text-center text-gray-400">{{ $t('nodes.empty') }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </UCard>

      <UModal v-model:open="showAdd" :title="$t('nodes.add_node')" :ui="{ content: 'sm:max-w-xl' }">
        <template #body>
          <div class="space-y-5">
            <div>
              <div class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">{{ $t('nodes.basic_info') }}</div>
              <div class="grid grid-cols-2 gap-4">
                <UFormField :label="$t('common.name')" required>
                  <UInput v-model="form.name" placeholder="node-01" />
                </UFormField>
                <UFormField :label="$t('nodes.ip_address')" required>
                  <UInput v-model="form.ip" placeholder="192.168.1.10" />
                </UFormField>
              </div>
            </div>

            <div>
              <div class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">{{ $t('nodes.ssh_connection') }}</div>
              <div class="grid grid-cols-3 gap-4">
                <UFormField :label="$t('nodes.ssh_user')">
                  <UInput v-model="form.ssh_username" placeholder="root / spark" />
                </UFormField>
                <UFormField :label="$t('nodes.ssh_port')">
                  <UInput v-model.number="form.ssh_port" type="number" />
                </UFormField>
                <UFormField :label="$t('nodes.agent_port')">
                  <UInput v-model.number="form.agent_port" type="number" />
                </UFormField>
              </div>
            </div>

            <div>
              <div class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">{{ $t('nodes.ssh_auth') }}</div>
              <UFormField :label="$t('nodes.auth_method')">
                <USelectMenu value-key="value"
                  v-model="form.ssh_auth_type"
                  :items="[{ label: $t('nodes.auth_password'), value: 'password' }, { label: $t('nodes.auth_key'), value: 'key' }]"
                />
              </UFormField>
              <div class="mt-3">
                <UFormField v-if="form.ssh_auth_type === 'password'" :label="$t('nodes.ssh_password')">
                  <UInput v-model="form.ssh_password" type="password" :placeholder="$t('nodes.ssh_password_placeholder')" />
                </UFormField>
                <UFormField v-else :label="$t('nodes.ssh_key_content')">
                  <UTextarea v-model="form.ssh_key" :rows="6" class="font-mono text-xs w-full" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" />
                </UFormField>
              </div>
            </div>

            <div class="rounded-lg border border-gray-200 dark:border-gray-800 p-3">
              <UCheckbox v-model="form.optimize_on_add" :label="$t('nodes.optimize_on_add_label')" />
              <p class="mt-1.5 text-xs text-gray-500 dark:text-gray-400">{{ $t('nodes.optimize_on_add_hint') }}</p>
            </div>
          </div>
        </template>
        <template #footer>
          <div class="flex w-full flex-col gap-2">
            <p v-if="submitting" class="text-xs text-primary">{{ $t('nodes.add_node_deploying') }}</p>
            <div class="flex justify-end gap-2">
              <UButton variant="outline" :disabled="submitting" @click="showAdd = false">{{ $t('common.cancel') }}</UButton>
              <UButton color="primary" :loading="submitting" :disabled="!form.name || !form.ip" @click="addNode">
                {{ $t('common.save') }}
              </UButton>
            </div>
          </div>
        </template>
      </UModal>

      <UModal v-model:open="showDelete" :title="$t('nodes.delete_options')">
        <template #body>
          <p class="text-sm text-gray-600 dark:text-gray-300">{{ $t('nodes.delete_options_hint') }}</p>
          <div class="mt-3 space-y-2.5">
            <UCheckbox v-model="delForm.agent" :label="$t('nodes.cleanup_agent_label')" />
            <UCheckbox v-model="delForm.network" :label="$t('nodes.cleanup_network_label')" />
            <UCheckbox v-model="delForm.models" :label="$t('nodes.cleanup_models_label')" />
            <UCheckbox v-model="delForm.images" :label="$t('nodes.cleanup_images_label')" />
          </div>
        </template>
        <template #footer>
          <div class="flex w-full justify-end gap-2">
            <UButton variant="outline" @click="showDelete = false">{{ $t('common.cancel') }}</UButton>
            <UButton color="error" :loading="deleting" @click="confirmDeleteNode">{{ $t('common.delete') }}</UButton>
          </div>
        </template>
      </UModal>

      <UModal v-model:open="showBatchResult" :title="$t('nodes.batch_result_title')">
        <template #body>
          <ul class="max-h-[55vh] overflow-y-auto divide-y divide-gray-100 dark:divide-gray-800">
            <li v-for="r in batchResults" :key="r.node_id" class="flex items-start gap-3 py-2.5">
              <UIcon
                :name="r.ok ? (r.skipped ? 'i-heroicons-minus-circle' : 'i-heroicons-check-circle') : 'i-heroicons-x-circle'"
                :class="r.ok ? (r.skipped ? 'text-gray-400' : 'text-green-500') : 'text-red-500'"
                class="mt-0.5 size-5 shrink-0"
              />
              <div class="min-w-0">
                <div class="text-sm font-medium">{{ r.node_name }}</div>
                <div class="text-xs text-gray-500 dark:text-gray-400">
                  {{ r.ok ? (r.skipped ? $t('nodes.optimize_skipped') : (r.summary || r.warning || $t('nodes.batch_result_ok'))) : (r.error || $t('common.unknown_error')) }}
                </div>
              </div>
            </li>
          </ul>
        </template>
      </UModal>
    </div>
    </template>
  </UDashboardPanel>
</template>
