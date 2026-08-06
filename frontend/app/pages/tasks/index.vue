<script setup lang="ts">
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
  const ok = await confirm.open({ title: '删除任务', description: `确认删除任务「${t.name}」？容器将被停止。` })
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
      <h1 class="text-xl font-bold">任务管理</h1>
      <UButton color="primary" to="/tasks/publish">发布任务</UButton>
    </div>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />

    <UCard>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
              <th class="py-2 pr-4 font-medium">名称</th>
              <th class="py-2 pr-4 font-medium">配方</th>
              <th class="py-2 pr-4 font-medium">集群</th>
              <th class="py-2 pr-4 font-medium">状态</th>
              <th class="py-2 pr-4 font-medium">节点</th>
              <th class="py-2 pr-4 font-medium">创建时间</th>
              <th class="py-2 font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in tasks" :key="t.id" class="border-b border-gray-100 dark:border-gray-800/60">
              <td class="py-2.5 pr-4">
                <NuxtLink :to="`/tasks/${t.id}`" class="font-medium hover:underline">{{ t.name }}</NuxtLink>
              </td>
              <td class="py-2.5 pr-4 text-gray-500">{{ recipeName(t.recipe_id) }}</td>
              <td class="py-2.5 pr-4 text-gray-500">{{ clusterName(t.cluster_id) }}</td>
              <td class="py-2.5 pr-4"><UBadge :color="statusColor[t.status] || 'neutral'" variant="subtle">{{ t.status }}</UBadge></td>
              <td class="py-2.5 pr-4">{{ t.nodes?.length || 0 }}</td>
              <td class="py-2.5 pr-4 text-gray-500">{{ fmtDateTime(t.created_at) }}</td>
              <td class="py-2.5 text-right whitespace-nowrap">
                <UButton size="xs" variant="ghost" :to="`/tasks/${t.id}`">详情</UButton>
                <UButton size="xs" variant="ghost" color="error" @click="removeTask(t)">删除</UButton>
              </td>
            </tr>
            <tr v-if="!tasks.length">
              <td colspan="7" class="py-8 text-center text-gray-400">暂无任务，点击「发布任务」</td>
            </tr>
          </tbody>
        </table>
      </div>
    </UCard>
  </div>
</template>
