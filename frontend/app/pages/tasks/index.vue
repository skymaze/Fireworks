<script setup lang="ts">
const { t } = useI18n()
const api = useApi()
const rt = useRealtime()
const tasks = ref<any[]>([])
const recipes = ref<any[]>([])
const clusters = ref<any[]>([])
const error = ref('')

const statusColor: Record<string, string> = {
  running: 'success', paused: 'warning', published: 'info', stopped: 'neutral', error: 'error',
}

async function load() {
  try {
    tasks.value = await api.get('/tasks')
    recipes.value = await api.get('/recipes')
    clusters.value = await api.get('/clusters')
    error.value = ''
  } catch (e) {
    error.value = String(e)
  }
}

// 实时：其他页面/窗口对任务的暂停/继续/停止/删除即时反映，无需刷新
function onTaskStatus(msg: any) {
  const t = tasks.value.find((x) => x.id === msg.task_id)
  if (t) t.status = msg.status
}
function onTaskDeleted(msg: any) {
  tasks.value = tasks.value.filter((x) => x.id !== msg.task_id)
}

const recipeName = (id: number) => recipes.value.find((r) => r.id === id)?.name || `#${id}`
const clusterName = (id: number) => clusters.value.find((c) => c.id === id)?.name || `#${id}`

const confirm = useConfirmDialog()

async function removeTask(t: any) {
  const ok = await confirm.open({ title: t('tasks.delete_title'), description: t('tasks.delete_confirm', { name: t.name }) })
  if (!ok) return
  await api.post(`/tasks/${t.id}/action`, { action: 'delete' })
  await load()
}

onMounted(() => {
  load()
  rt.on('task_status', onTaskStatus)
  rt.on('task_deleted', onTaskDeleted)
})
onUnmounted(() => {
  rt.off('task_status', onTaskStatus)
  rt.off('task_deleted', onTaskDeleted)
})
</script>

<template>
  <div>
    <div class="flex items-center justify-between mb-4">
      <h1 class="text-xl font-bold">{{ $t('tasks.title') }}</h1>
      <UButton color="primary" to="/tasks/publish">{{ $t('tasks.publish') }}</UButton>
    </div>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />

    <UCard>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
              <th class="py-2 pr-4 font-medium">{{ $t('common.name') }}</th>
              <th class="py-2 pr-4 font-medium">{{ $t('tasks.col_recipe') }}</th>
              <th class="py-2 pr-4 font-medium">{{ $t('tasks.col_cluster') }}</th>
              <th class="py-2 pr-4 font-medium">{{ $t('tasks.col_status') }}</th>
              <th class="py-2 pr-4 font-medium">{{ $t('tasks.col_nodes') }}</th>
              <th class="py-2 pr-4 font-medium">{{ $t('tasks.col_created') }}</th>
              <th class="py-2 font-medium text-right">{{ $t('common.actions') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in tasks" :key="t.id" class="border-b border-gray-100 dark:border-gray-800/60">
              <td class="py-2.5 pr-4">
                <NuxtLink :to="`/tasks/${t.id}`" class="font-medium hover:underline">{{ t.name }}</NuxtLink>
              </td>
              <td class="py-2.5 pr-4 text-gray-500">{{ recipeName(t.recipe_id) }}</td>
              <td class="py-2.5 pr-4 text-gray-500">{{ clusterName(t.cluster_id) }}</td>
              <td class="py-2.5 pr-4"><UBadge :color="statusColor[t.status] || 'neutral'" variant="subtle">{{ statusLabel(t.status) }}</UBadge></td>
              <td class="py-2.5 pr-4">{{ t.nodes?.length || 0 }}</td>
              <td class="py-2.5 pr-4 text-gray-500">{{ fmtDateTime(t.created_at) }}</td>
              <td class="py-2.5 text-right whitespace-nowrap">
                <UButton size="xs" variant="ghost" :to="`/tasks/${t.id}`">{{ $t('common.detail') }}</UButton>
                <UButton size="xs" variant="ghost" color="error" @click="removeTask(t)">{{ $t('common.delete') }}</UButton>
              </td>
            </tr>
            <tr v-if="!tasks.length">
              <td colspan="7" class="py-8 text-center text-gray-400">{{ $t('tasks.empty') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </UCard>
  </div>
</template>
