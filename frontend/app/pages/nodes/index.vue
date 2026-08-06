<script setup lang="ts">
const { t } = useI18n()
const api = useApi()
const confirm = useConfirmDialog()
const nodes = ref<any[]>([])
const loading = ref(false)
const error = ref('')
const notice = ref('')
const deployingId = ref<number | null>(null)

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
    error.value = ''
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

async function addNode() {
  submitting.value = true
  error.value = ''
  try {
    await api.post('/nodes', form)
    showAdd.value = false
    Object.assign(form, {
      name: '', ip: '', ssh_port: 22, ssh_username: 'root',
      ssh_auth_type: 'password', ssh_password: '', ssh_key: '', agent_port: 9000,
    })
    await load()
  } catch (e) {
    error.value = String(e)
  } finally {
    submitting.value = false
  }
}

async function deployAgent(n: any) {
  deployingId.value = n.id
  notice.value = ''
  try {
    const r = await api.post(`/nodes/${n.id}/deploy-agent`)
    notice.value = r.ok
      ? (r.warning ? t('nodes.deploy_done_warning', { warning: r.warning }) : t('nodes.deploy_success', { version: r.hardware_info?.agent_version || '?' }))
      : t('nodes.deploy_fail', { error: r.error || t('common.unknown_error') })
    await load()
  } catch (e) {
    error.value = String(e)
  } finally {
    deployingId.value = null
  }
}

async function refreshNode(n: any) {
  notice.value = ''
  try {
    await api.post(`/nodes/${n.id}/refresh`)
    notice.value = t('nodes.refreshed', { name: n.name })
    await load()
  } catch (e) {
    error.value = String(e)
  }
}

async function removeNode(n: any) {
  const ok = await confirm.open({ title: t('nodes.delete_title'), description: t('nodes.delete_confirm', { name: n.name }) })
  if (!ok) return
  await api.del(`/nodes/${n.id}`)
  await load()
}

function gpuCount(n: any): number {
  return n.hardware_info?.gpus?.length ?? 0
}

function statusColor(s: string) {
  return { online: 'success', offline: 'error', unknown: 'neutral', error: 'error' }[s] || 'neutral'
}

onMounted(load)
</script>

<template>
  <div>
    <div class="flex items-center justify-between mb-4">
      <h1 class="text-xl font-bold">{{ $t('nodes.title') }}</h1>
      <UButton color="primary" @click="showAdd = true">{{ $t('nodes.add_node') }}</UButton>
    </div>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />
    <UAlert v-if="notice" :title="notice" color="success" class="mb-4" />

    <UCard>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
              <th class="py-2 pr-4 font-medium">{{ $t('common.name') }}</th>
              <th class="py-2 pr-4 font-medium">IP</th>
              <th class="py-2 pr-4 font-medium">{{ $t('nodes.agent_status') }}</th>
              <th class="py-2 pr-4 font-medium">GPU</th>
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
              <td class="py-2.5 pr-4 text-gray-500">
                {{ fmtDateTime(n.last_seen) }}
              </td>
              <td class="py-2.5 text-right whitespace-nowrap">
                <UButton size="xs" variant="ghost" :to="`/nodes/${n.id}`">{{ $t('common.detail') }}</UButton>
                <UButton size="xs" variant="ghost" :loading="deployingId === n.id" @click="deployAgent(n)">{{ $t('nodes.deploy_agent') }}</UButton>
                <UButton size="xs" variant="ghost" @click="refreshNode(n)">{{ $t('common.refresh') }}</UButton>
                <UButton size="xs" variant="ghost" color="error" @click="removeNode(n)">{{ $t('common.delete') }}</UButton>
              </td>
            </tr>
            <tr v-if="!nodes.length">
              <td colspan="6" class="py-8 text-center text-gray-400">{{ $t('nodes.empty') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </UCard>

    <UModal v-model:open="showAdd" :ui="{ width: 'sm:max-w-xl' }">
      <template #content>
        <UCard>
        <template #header>
          <div class="font-semibold">{{ $t('nodes.add_node') }}</div>
        </template>
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
              <USelect
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
        <template #footer>
          <div class="flex justify-end gap-2">
            <UButton variant="outline" @click="showAdd = false">{{ $t('common.cancel') }}</UButton>
            <UButton color="primary" :loading="submitting" :disabled="!form.name || !form.ip" @click="addNode">
              {{ $t('common.save') }}
            </UButton>
          </div>
        </template>
      </UCard>
      </template>
    </UModal>
  </div>
</template>
