<script setup lang="ts">
const { t } = useI18n()
const api = useApi()
const toast = useToast()
const confirm = useConfirmDialog()
const nodes = ref<any[]>([])
const loading = ref(false)
const deployingIds = ref(new Set<number>())

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
})
const submitting = ref(false)

async function load() {
  loading.value = true
  try {
    nodes.value = await api.get('/nodes')
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    loading.value = false
  }
}

async function addNode() {
  submitting.value = true
  try {
    // 添加节点即安装 Agent（后端原子操作：安装/验证失败会报错并回滚）
    const n = await api.post('/nodes', form)
    showAdd.value = false
    toast.add({
      title: t('nodes.deploy_success', { version: n?.hardware_info?.agent_version || '?' }),
      color: 'success',
    })
    Object.assign(form, {
      name: '', ip: '', ssh_port: 22, ssh_username: 'root',
      ssh_auth_type: 'password', ssh_password: '', ssh_key: '', agent_port: 9000,
    })
    await load()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    submitting.value = false
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
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
                <th class="py-2 pr-4 font-medium">{{ $t('common.name') }}</th>
                <th class="py-2 pr-4 font-medium">IP</th>
                <th class="py-2 pr-4 font-medium">{{ $t('nodes.agent_status') }}</th>
                <th class="py-2 pr-4 font-medium">GPU</th>
                <th class="py-2 pr-4 font-medium">{{ $t('nodes.agent_version') }}</th>
                <th class="py-2 pr-4 font-medium">{{ $t('nodes.last_online') }}</th>
                <th class="py-2 font-medium text-right">{{ $t('common.actions') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="n in nodes" :key="n.id" class="border-b border-gray-100 dark:border-gray-800/60">
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
                <td class="py-2.5 text-right whitespace-nowrap">
                  <UButton size="xs" variant="ghost" :to="`/nodes/${n.id}`">{{ $t('common.detail') }}</UButton>
                  <UButton
                    size="xs"
                    variant="ghost"
                    :color="agentActionColor(n)"
                    :loading="deployingIds.has(n.id)"
                    @click="installAgent(n)"
                  >{{ $t(agentDeployLabelKey(agentDeployAction(n))) }}</UButton>
                  <UButton size="xs" variant="ghost" @click="refreshNode(n)">{{ $t('common.refresh') }}</UButton>
                  <UButton size="xs" variant="ghost" color="error" @click="removeNode(n)">{{ $t('common.delete') }}</UButton>
                </td>
              </tr>
              <tr v-if="!nodes.length">
                <td colspan="7" class="py-8 text-center text-gray-400">{{ $t('nodes.empty') }}</td>
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
    </div>
    </template>
  </UDashboardPanel>
</template>
