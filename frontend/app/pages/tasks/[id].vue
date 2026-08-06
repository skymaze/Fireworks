<script setup lang="ts">
const route = useRoute()
const api = useApi()
const rt = useRealtime()
const taskId = Number(route.params.id)

const task = ref<any>(null)
const recipes = ref<any[]>([])
const clusters = ref<any[]>([])
const nodes = ref<any[]>([])
const logsNodeId = ref<number | null>(null)
const logs = ref('')
const error = ref('')
const acting = ref(false)
let logSubscribed = false

const statusColor: Record<string, string> = {
  running: 'success', paused: 'warning', published: 'info', stopped: 'neutral', error: 'error',
}

async function load() {
  try {
    ;[task.value, recipes.value, clusters.value, nodes.value] = await Promise.all([
      api.get(`/tasks/${taskId}`),
      api.get('/recipes'),
      api.get('/clusters'),
      api.get('/nodes'),
    ])
    if (!logsNodeId.value && task.value.nodes?.length) {
      logsNodeId.value = task.value.nodes[0].node_id
      await loadLogs()
    }
    error.value = ''
  } catch (e) {
    error.value = String(e)
  }
}

// 上一段日志是否为「原地刷新行」（update）：是则下一次 update 覆盖它，否则新起一行
let lastLogLineUpdate = false

async function loadLogs() {
  if (!logsNodeId.value) return
  lastLogLineUpdate = false
  try {
    const r = await api.get(`/tasks/${taskId}/logs`, { node_id: logsNodeId.value, tail: 300 })
    // 快照中的进度条 \r 原地刷新归一为独立行（实时流由 update 标记本行覆盖）
    logs.value = (r.logs || '').replace(/\r/g, '\n') || '(无日志)'
  } catch (e) {
    logs.value = `获取日志失败: ${e}`
  }
}

// ---------- 实时通道：日志流 + 容器/任务状态 ----------

function currentContainerName(): string | null {
  const tn = task.value?.nodes?.find((x: any) => x.node_id === logsNodeId.value)
  return tn?.container_name || null
}

function subscribeLogs() {
  if (!logsNodeId.value || logSubscribed) return
  rt.send({ type: 'log_subscribe', task_id: taskId, node_id: logsNodeId.value })
  logSubscribed = true
}

function unsubscribeLogs() {
  if (!logSubscribed) return
  rt.send({ type: 'log_unsubscribe', task_id: taskId, node_id: logsNodeId.value })
  logSubscribed = false
}

function onLog(msg: any) {
  if (msg.container && msg.container === currentContainerName()) {
    // 初始快照之后流式追加（保留尾部换行）
    if (logs.value === '(无日志)') {
      logs.value = ''
      lastLogLineUpdate = false
    }
    const line = msg.line ?? ''
    if (msg.update) {
      // 进度类本行刷新：连续 update 覆盖上一行；首个 update 新起一行
      if (lastLogLineUpdate) {
        const i = logs.value.lastIndexOf('\n')
        logs.value = logs.value.slice(0, i + 1) + line
      } else {
        if (logs.value && !logs.value.endsWith('\n')) logs.value += '\n'
        logs.value += line
      }
      lastLogLineUpdate = true
    } else {
      if (logs.value && !logs.value.endsWith('\n')) logs.value += '\n'
      logs.value += line + '\n'
      lastLogLineUpdate = false
    }
    if (logs.value.length > 500_000) logs.value = logs.value.slice(-500_000)
    scrollLogToBottom() // 推送后自动滚动到最新一行
  }
}

function onLogEnd(msg: any) {
  if (msg.container && msg.container === currentContainerName()) {
    logSubscribed = false // 流已结束（容器退出），停止退订命令避免空发
  }
}

function onContainerStatus(msg: any) {
  if (msg.task_id !== taskId || !task.value?.nodes) return
  const tn = task.value.nodes.find((x: any) => x.node_id === msg.node_id)
  if (tn) tn.container_status = msg.status
}

function onTaskStatus(msg: any) {
  if (msg.task_id === taskId && task.value) task.value.status = msg.status
}

function onTaskDeleted(msg: any) {
  // 任务被其他页面/窗口删除：跳回列表，避免停留陈旧详情
  if (msg.task_id === taskId) navigateTo('/tasks')
}

async function act(action: string, deleteModel = false) {
  acting.value = true
  try {
    await api.post(`/tasks/${taskId}/action`, { action, delete_model: deleteModel })
    if (action === 'delete') {
      // 删除成功：任务已不存在，跳回列表（避免详情页 404 卡在旧状态）
      await navigateTo('/tasks')
      return
    }
    await load()
  } catch (e) {
    if (action === 'delete') {
      // 并发删除：任务已被其他请求删除也属成功
      await navigateTo('/tasks')
      return
    }
    error.value = String(e)
  } finally {
    acting.value = false
  }
}

// 终止/删除确认（可选同时删除节点模型）
const showActionModal = ref(false)
const pendingAction = ref('')
const deleteModel = ref(false)

function openAction(action: string) {
  pendingAction.value = action
  deleteModel.value = false
  showActionModal.value = true
}

async function confirmAction() {
  showActionModal.value = false
  await act(pendingAction.value, deleteModel.value)
}

const recipeName = computed(() => recipes.value.find((r) => r.id === task.value?.recipe_id)?.name || '—')
const clusterName = computed(() => clusters.value.find((c) => c.id === task.value?.cluster_id)?.name || '—')
const nodeName = (id: number) => nodes.value.find((n) => n.id === id)?.name || `#${id}`

// 日志区自动滚动到最新一行
const logBox = ref<HTMLElement | null>(null)
let logScrollQueued = false
function scrollLogToBottom() {
  if (logScrollQueued) return
  logScrollQueued = true
  nextTick(() => {
    logScrollQueued = false
    if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
  })
}

watch(logsNodeId, async () => {
  unsubscribeLogs()
  await loadLogs()
  subscribeLogs()
  scrollLogToBottom()
})

watch(rt.connected, (v) => {
  // WS 断线重连后重新订阅日志流（后端连接状态已重置）
  if (v && logSubscribed) {
    logSubscribed = false // 重置后重发订阅（否则被 subscribeLogs 守卫挡住）
    subscribeLogs()
  }
})

async function refreshLogs() {
  // 容器退出（log_end）后重订阅，容器重启后新日志继续推送
  unsubscribeLogs()
  await loadLogs()
  subscribeLogs()
  scrollLogToBottom()
}

onMounted(() => {
  rt.on('log', onLog)
  rt.on('log_end', onLogEnd)
  rt.on('container_status', onContainerStatus)
  rt.on('task_status', onTaskStatus)
  rt.on('task_deleted', onTaskDeleted)
  load().then(() => subscribeLogs())
  const t = setInterval(() => {
    if (task.value && ['running', 'published'].includes(task.value.status)) {
      load()
    }
  }, 10000)
  onUnmounted(() => {
    clearInterval(t)
    unsubscribeLogs()
    rt.off('log', onLog)
    rt.off('log_end', onLogEnd)
    rt.off('container_status', onContainerStatus)
    rt.off('task_status', onTaskStatus)
    rt.off('task_deleted', onTaskDeleted)
  })
})
</script>

<template>
  <div>
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center gap-3">
        <UButton size="sm" variant="ghost" to="/tasks">返回</UButton>
        <h1 class="text-xl font-bold">{{ task?.name || '任务详情' }}</h1>
        <UBadge v-if="task" :color="statusColor[task.status] || 'neutral'" variant="subtle">{{ task.status }}</UBadge>
      </div>
      <div v-if="task" class="flex gap-2">
        <UButton
          v-if="task.status === 'running'"
          size="sm"
          variant="outline"
          :loading="acting"
          @click="act('pause')"
        >暂停</UButton>
        <UButton
          v-if="task.status === 'paused'"
          size="sm"
          color="primary"
          :loading="acting"
          @click="act('resume')"
        >继续</UButton>
        <UButton
          v-if="['running', 'paused', 'published', 'error'].includes(task.status)"
          size="sm"
          variant="outline"
          :loading="acting"
          @click="openAction('stop')"
        >停止</UButton>
        <UButton size="sm" variant="outline" color="error" :loading="acting" @click="openAction('delete')">删除</UButton>
      </div>
    </div>

    <UModal v-model:open="showActionModal">
      <template #content>
        <UCard>
        <template #header>
          <div class="font-semibold">{{ pendingAction === 'delete' ? '删除任务' : '停止任务' }}</div>
        </template>
        <p class="text-sm text-gray-600 dark:text-gray-300">
          确认{{ pendingAction === 'delete' ? '删除' : '停止' }}任务「{{ task?.name }}」？
          {{ pendingAction === 'delete' ? '任务记录与容器将被移除。' : '容器将被停止。' }}
        </p>
        <UFormField label="模型处理（与任务解耦）" class="mt-3">
          <UCheckbox v-model="deleteModel" label="同时删除节点上的模型文件（释放磁盘）" />
        </UFormField>
        <template #footer>
          <div class="flex justify-end gap-2">
            <UButton variant="outline" @click="showActionModal = false">取消</UButton>
            <UButton :color="pendingAction === 'delete' ? 'error' : 'primary'" :loading="acting" @click="confirmAction">
              确认{{ pendingAction === 'delete' ? '删除' : '停止' }}
            </UButton>
          </div>
        </template>
      </UCard>
      </template>
    </UModal>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />
    <UAlert v-if="task?.error" :title="task.error" color="error" class="mb-4" />

    <div v-if="task" class="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <UCard>
        <template #header><div class="font-semibold">信息</div></template>
        <dl class="text-sm space-y-1.5">
          <div class="flex justify-between"><dt class="text-gray-500">配方</dt><dd>{{ recipeName }}</dd></div>
          <div class="flex justify-between"><dt class="text-gray-500">集群</dt><dd>{{ clusterName }}</dd></div>
          <div class="flex justify-between"><dt class="text-gray-500">创建时间</dt><dd>{{ fmtDateTime(task.created_at) }}</dd></div>
        </dl>
        <div class="mt-3">
          <div class="text-xs text-gray-500 mb-1">用户变量</div>
          <pre class="bg-gray-50 dark:bg-gray-900 rounded p-2 text-[11px] overflow-x-auto">{{ JSON.stringify(task.variables, null, 2) }}</pre>
        </div>
      </UCard>

      <div class="lg:col-span-2 space-y-4">
        <UCard>
          <template #header><div class="font-semibold">节点容器（{{ task.nodes?.length }}）</div></template>
          <div class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead>
                <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
                  <th class="py-2 pr-4 font-medium">节点</th>
                  <th class="py-2 pr-4 font-medium">角色</th>
                  <th class="py-2 pr-4 font-medium">rank</th>
                  <th class="py-2 pr-4 font-medium">容器</th>
                  <th class="py-2 pr-4 font-medium">状态</th>
                  <th class="py-2 font-medium">错误</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="tn in task.nodes" :key="tn.id" class="border-b border-gray-100 dark:border-gray-800/60">
                  <td class="py-2.5 pr-4">
                    <NuxtLink :to="`/nodes/${tn.node_id}`" class="hover:underline">{{ nodeName(tn.node_id) }}</NuxtLink>
                  </td>
                  <td class="py-2.5 pr-4"><UBadge variant="subtle">{{ tn.role }}</UBadge></td>
                  <td class="py-2.5 pr-4">{{ tn.node_rank }}</td>
                  <td class="py-2.5 pr-4 font-mono text-xs text-gray-600">{{ tn.container_name || '—' }}</td>
                  <td class="py-2.5 pr-4">{{ tn.container_status || '—' }}</td>
                  <td class="py-2.5 text-xs text-red-500">{{ tn.error || '' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </UCard>

        <UCard>
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">容器日志</div>
              <div class="flex items-center gap-2">
                <USelect
                  v-model="logsNodeId"
                  :items="(task.nodes || []).map((tn: any) => ({ label: nodeName(tn.node_id), value: tn.node_id }))"
                  class="w-40"
                />
                <UButton size="xs" variant="outline" @click="refreshLogs">刷新</UButton>
              </div>
            </div>
          </template>
          <pre ref="logBox" class="bg-gray-50 dark:bg-gray-900 rounded-md p-3 text-xs overflow-x-auto overflow-y-auto whitespace-pre max-h-96">{{ logs }}</pre>
        </UCard>
      </div>
    </div>
  </div>
</template>
