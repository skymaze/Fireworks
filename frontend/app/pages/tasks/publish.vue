<script setup lang="ts">
const api = useApi()
const router = useRouter()

const recipes = ref<any[]>([])
const clusters = ref<any[]>([])
const recipeId = ref<number | null>(null)
const clusterId = ref<number | null>(null)
const plan = ref<any>(null)
// 集群 plan 返回的配置警告（如 head 非 rank0），页面顶部展示但不禁用发布
const planWarnings = ref<string[]>([])
const taskName = ref('')
const headNodeId = ref<number | null>(null)
const workerIds = ref<number[]>([])
const sendModel = ref(true)  // 发布前确保模型发送到节点（缺失则自动管理传输）
const sendImage = ref(true)  // 发布前确保镜像发送到节点（缺失则自动管理传输）
const varValues = reactive<Record<string, string>>({})
const preview = ref<any>(null)
const previewing = ref(false)
const publishing = ref(false)
const error = ref('')

const recipe = computed(() => recipes.value.find((r) => r.id === recipeId.value))
const userVars = computed(() => (recipe.value?.variables || []).filter((v: any) => v.source === 'user'))
const clusterVars = computed(() => (recipe.value?.variables || []).filter((v: any) => v.source === 'cluster'))

// 快速选择：已下载模型 / 已拉取镜像
const pickerOpen = ref(false)
const pickerVar = ref<any>(null)
const pickerItems = ref<any[]>([])
const pickerLoading = ref(false)

const fmt = (v: number) =>
  v >= 1024 ** 3 ? `${(v / 1024 ** 3).toFixed(1)} GB` : v >= 1024 ** 2 ? `${(v / 1024 ** 2).toFixed(0)} MB` : `${(v / 1024).toFixed(0)} KB`

async function openPicker(v: any) {
  pickerVar.value = v
  pickerItems.value = []
  pickerOpen.value = true
  pickerLoading.value = true
  try {
    if (v.picker === 'model') {
      const r = await api.get('/models/local')
      // 只允许选择已下载完成的模型；未完成（下载中/失败/残留）显示状态并禁用
      const labels: Record<string, string> = {
        complete: '', downloading: '下载中', failed: '下载失败', partial: '未完成',
      }
      pickerItems.value = (r.models || []).map((m: any) => ({
        name: m.repo,
        size: m.size_bytes,
        complete: !!m.complete,
        statusLabel: labels[m.status] || '',
      }))
    } else if (v.picker === 'image') {
      const r = await api.get('/images/local')
      // 只显示已完整拉取的归档（无关联镜像名的孤儿归档不展示）
      pickerItems.value = (r.archives || [])
        .filter((a: any) => a.image)
        .map((a: any) => ({ name: a.image, size: a.size_bytes, complete: true, statusLabel: '' }))
    }
  } catch (e) {
    error.value = errorMsg(e)
  } finally {
    pickerLoading.value = false
  }
}

function pickItem(item: any) {
  if (!item.complete) return // 未下载完成的模型不可选
  if (pickerVar.value) varValues[pickerVar.value.key] = item.name
  pickerOpen.value = false
}

// 模型缓存状态
const modelStatus = ref<Record<string, any>>({})
const modelChecking = ref(false)
const transferring = ref(false)
const transferJob = ref<any>(null)
let transferTimer: ReturnType<typeof setInterval> | null = null

// 本次发布选中的节点（head + 勾选的 worker）；状态检查/传输仅针对这些节点
const selectedNodes = computed(() => {
  if (!plan.value) return []
  return plan.value.nodes.filter(
    (n: any) => n.node_id === headNodeId.value || workerIds.value.includes(n.node_id),
  )
})

const modelRepo = computed(() => {
  const v = userVars.value.find((x: any) => x.key === 'DSPARK_MODEL')
  return v ? (varValues[v.key] || v.default) : null
})

// 全部选中节点模型完整（发布按钮前置条件）
const allComplete = computed(() => {
  if (!modelRepo.value || !selectedNodes.value.length) return false
  return selectedNodes.value.every((n: any) => modelStatus.value[n.node_id]?.complete)
})
// 模型未完整（可点击"发送模型"）
const modelIncomplete = computed(() => !!modelRepo.value && !!selectedNodes.value.length && !allComplete.value)

// 自动重查：本次查询后仍有节点未就绪时，延迟 4s 自动重查一次（缓解"分发刚完成、查询过早"窗口）
let modelAutoRetried = false
let modelRetryTimer: ReturnType<typeof setTimeout> | null = null

async function checkModel() {
  if (!modelRepo.value || !plan.value) return
  if (modelRetryTimer) clearTimeout(modelRetryTimer)
  modelChecking.value = true
  modelStatus.value = {}
  try {
    for (const n of selectedNodes.value) {
      try {
        const st = await api.get(`/models/cached/${modelRepo.value}`, { node_id: n.node_id })
        modelStatus.value[n.node_id] = st
      } catch {
        // 节点 agent 不可达：标记出来，避免误判为"未缓存"而触发重复传输
        modelStatus.value[n.node_id] = { complete: false, cached: false, error: '节点不可达' }
      }
    }
  } finally {
    modelChecking.value = false
  }
  if (!modelAutoRetried && modelIncomplete.value && selectedNodes.value.length) {
    modelAutoRetried = true
    modelRetryTimer = setTimeout(() => { modelRetryTimer = null; checkModel() }, 4000)
  }
}

// 在本页发起模型传输：控制平面下载 -> 发送 head -> RoCE 同步 worker，完成后发布按钮解锁
async function startModelTransfer() {
  if (!modelRepo.value || !plan.value) return
  transferring.value = true
  error.value = ''
  transferJob.value = null
  try {
    const head = plan.value.nodes.find((n: any) => n.role === 'head')?.node_id || plan.value.nodes[0]?.node_id
    const workers = plan.value.nodes.filter((n: any) => n.node_id !== head).map((n: any) => n.node_id)
    const job = await api.post('/models/download', {
      repo: modelRepo.value,
      head_node_id: head,
      sync_node_ids: workers,
    })
    transferJob.value = job
    if (transferTimer) clearInterval(transferTimer)
    transferTimer = setInterval(async () => {
      try {
        const list = await api.get('/models/downloads', { status: 'active' })
        const cur = list.find((x: any) => x.id === job.id)
        if (cur) {
          transferJob.value = cur
          if (cur.status === 'failed') {
            clearInterval(transferTimer!)
            transferTimer = null
            transferring.value = false
            error.value = `模型传输失败：${cur.error || '未知错误'}`
          } else if (cur.status === 'cancelled' || cur.status === 'paused') {
            // 用户在其他页面暂停/取消了传输：停止轮询，保持未就绪状态
            clearInterval(transferTimer!)
            transferTimer = null
            transferring.value = false
            transferJob.value = null
            if (cur.status === 'cancelled') error.value = '模型传输已取消，请重新发起'
            await checkModel()
          }
          return
        }
        // 不在 active 列表 = 任务已完成（active 列表不包含 completed）
        clearInterval(transferTimer!)
        transferTimer = null
        transferring.value = false
        transferJob.value = null
        await checkModel()  // 刷新节点缓存状态 -> 发布按钮解锁
      } catch { /* ignore */ }
    }, 5000)
  } catch (e) {
    transferring.value = false
    error.value = errorMsg(e)
  }
}

onUnmounted(() => {
  if (transferTimer) clearInterval(transferTimer)
  if (imageTransferTimer) clearInterval(imageTransferTimer)
  if (modelRetryTimer) clearTimeout(modelRetryTimer)
  if (imageRetryTimer) clearTimeout(imageRetryTimer)
})

// 镜像节点状态（发布前置条件：镜像已分发到节点）
const imageStatus = ref<Record<string, any>>({})
const imageChecking = ref(false)
const imageTransferring = ref(false)
const imageTransferJob = ref<any>(null)
let imageTransferTimer: ReturnType<typeof setInterval> | null = null

// 取第一个标记为镜像快速选择的变量（如 DSPARK_VLLM_IMAGE）
const imageRepo = computed(() => {
  const v = userVars.value.find((x: any) => x.picker === 'image')
  return v ? (varValues[v.key] || v.default) : null
})

// 全部选中节点镜像就绪（发布按钮前置条件）
const allImageReady = computed(() => {
  if (!imageRepo.value || !selectedNodes.value.length) return false
  return selectedNodes.value.every((n: any) => imageStatus.value[n.node_id]?.present)
})
const imageIncomplete = computed(() => !!imageRepo.value && !!selectedNodes.value.length && !allImageReady.value)

let imageAutoRetried = false
let imageRetryTimer: ReturnType<typeof setTimeout> | null = null

async function checkImage() {
  if (!imageRepo.value || !plan.value) return
  if (imageRetryTimer) clearTimeout(imageRetryTimer)
  imageChecking.value = true
  imageStatus.value = {}
  try {
    await Promise.all(selectedNodes.value.map(async (n: any) => {
      try {
        const st = await api.get('/images/node-status', { image: imageRepo.value, node_id: n.node_id })
        imageStatus.value[n.node_id] = st
      } catch {
        imageStatus.value[n.node_id] = { present: false, error: '节点不可达' }
      }
    }))
  } finally {
    imageChecking.value = false
  }
  if (!imageAutoRetried && !allImageReady.value && selectedNodes.value.length) {
    imageAutoRetried = true
    imageRetryTimer = setTimeout(() => { imageRetryTimer = null; checkImage() }, 4000)
  }
}

// 在本页发起镜像传输：控制平面归档 -> 发送 head -> RoCE 同步 worker -> 各节点 docker load
async function startImageTransfer() {
  if (!imageRepo.value || !plan.value) return
  imageTransferring.value = true
  error.value = ''
  imageTransferJob.value = null
  try {
    const head = plan.value.nodes.find((n: any) => n.role === 'head')?.node_id || plan.value.nodes[0]?.node_id
    const workers = plan.value.nodes.filter((n: any) => n.node_id !== head).map((n: any) => n.node_id)
    const job = await api.post('/images/transfer', {
      image: imageRepo.value,
      head_node_id: head,
      sync_node_ids: workers,
    })
    imageTransferJob.value = job
    if (imageTransferTimer) clearInterval(imageTransferTimer)
    imageTransferTimer = setInterval(async () => {
      try {
        const list = await api.get('/images/transfers', { status: 'active' })
        const cur = list.find((x: any) => x.id === job.id)
        if (cur) {
          imageTransferJob.value = cur
          if (cur.status === 'failed') {
            clearInterval(imageTransferTimer!)
            imageTransferTimer = null
            imageTransferring.value = false
            error.value = `镜像传输失败：${cur.error || '未知错误'}`
          } else if (cur.status === 'cancelled' || cur.status === 'paused') {
            // 用户在其他页面暂停/取消了传输：停止轮询，保持未就绪状态
            clearInterval(imageTransferTimer!)
            imageTransferTimer = null
            imageTransferring.value = false
            imageTransferJob.value = null
            if (cur.status === 'cancelled') error.value = '镜像传输已取消，请重新发起'
            await checkImage()
          }
          return
        }
        // 不在 active 列表 = 任务已完成（active 已含 failed，不会漏失败）
        clearInterval(imageTransferTimer!)
        imageTransferTimer = null
        imageTransferring.value = false
        imageTransferJob.value = null
        await checkImage()  // 刷新节点镜像状态 -> 发布按钮解锁
      } catch { /* ignore */ }
    }, 5000)
  } catch (e) {
    imageTransferring.value = false
    error.value = errorMsg(e)
  }
}

// 配方（模型/镜像）或集群（plan 节点）/节点选择变化时都刷新缓存状态。
// workerIds 为数组（勾选时原地修改），需 deep 监听，否则勾选 worker 不触发检查
watch([modelRepo, plan, headNodeId, workerIds], checkModel, { deep: true })
watch([imageRepo, plan, headNodeId, workerIds], checkImage, { deep: true })

async function loadBase() {
  try {
    ;[recipes.value, clusters.value] = await Promise.all([api.get('/recipes'), api.get('/clusters')])
  } catch (e) {
    error.value = String(e)
  }
}

watch(clusterId, async (id) => {
  plan.value = null
  planWarnings.value = []
  preview.value = null
  headNodeId.value = null
  workerIds.value = []
  if (!id) return
  try {
    plan.value = await api.get(`/clusters/${id}/plan`)
    planWarnings.value = plan.value?.warnings || []
    const head = plan.value.nodes.find((n: any) => n.role === 'head')
    headNodeId.value = head?.node_id || plan.value.nodes[0]?.node_id || null
  } catch (e) {
    error.value = String(e)
  }
})

watch(recipeId, () => {
  preview.value = null
  // 预填变量默认值
  for (const k of Object.keys(varValues)) delete varValues[k]
  for (const v of userVars.value) if (v.default != null) varValues[v.key] = String(v.default)
})

function toggleWorker(id: number) {
  const i = workerIds.value.indexOf(id)
  if (i >= 0) workerIds.value.splice(i, 1)
  else workerIds.value.push(id)
}

// 配方要求的节点数（NODES_TOTAL 变量的 min 元数据；选中节点不足时禁止发布）
const nodeMin = computed(() => {
  const v = clusterVars.value.find((x: any) => x.key === 'NODES_TOTAL')
  return v?.min ? Number(v.min) : 0
})
const nodeCountOk = computed(() => !nodeMin.value || selectedNodes.value.length >= nodeMin.value)

function clusterVarValue(v: any): string {
  const auto = v.auto
  if (auto && plan.value?.cluster_vars?.[auto] != null) return String(plan.value.cluster_vars[auto])
  return v.default || ''
}

async function doPreview() {
  previewing.value = true
  error.value = ''
  try {
    preview.value = await api.post(`/recipes/${recipeId.value}/preview`, {
      cluster_id: clusterId.value,
      head_node_id: headNodeId.value,
      worker_node_ids: workerIds.value,
      variables: { ...varValues },
    })
  } catch (e) {
    error.value = String(e)
    preview.value = null
  } finally {
    previewing.value = false
  }
}

async function publish() {
  publishing.value = true
  error.value = ''
  try {
    const task = await api.post('/tasks', {
      name: taskName.value,
      recipe_id: recipeId.value,
      cluster_id: clusterId.value,
      head_node_id: headNodeId.value,
      worker_node_ids: workerIds.value,
      variables: { ...varValues },
      send_model: sendModel.value,
      send_image: sendImage.value,
    })
    router.push(`/tasks/${task.id}`)
  } catch (e) {
    error.value = String(e)
  } finally {
    publishing.value = false
  }
}

onMounted(loadBase)
</script>

<template>
  <div>
    <div class="flex items-center gap-3 mb-4">
      <UButton size="sm" variant="ghost" to="/tasks">返回</UButton>
      <h1 class="text-xl font-bold">发布任务</h1>
    </div>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />
    <UAlert v-if="planWarnings.length" :title="planWarnings.join('；')" color="warning" class="mb-4" />

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <div class="lg:col-span-2 space-y-4">
        <UCard>
          <template #header><div class="font-semibold">1. 选择配方与集群</div></template>
          <div class="grid grid-cols-2 gap-4">
            <UFormField label="配方" required>
              <USelect
                v-model="recipeId"
                :items="recipes.map((r) => ({ label: r.name, value: r.id }))"
                placeholder="选择配方"
              />
            </UFormField>
            <UFormField label="集群" required>
              <USelect
                v-model="clusterId"
                :items="clusters.map((c) => ({ label: `${c.name} (${c.members?.length || 0} 节点)`, value: c.id }))"
                placeholder="选择集群"
              />
            </UFormField>
          </div>
          <div class="mt-3 text-xs text-gray-500">{{ recipe?.description || '选择配方后展示说明' }}</div>
        </UCard>

        <UCard v-if="plan">
          <template #header><div class="font-semibold">2. 选择 Head / Worker</div></template>
          <UFormField label="Head 节点" required>
            <USelect
              v-model="headNodeId"
              :items="plan.nodes.map((n: any) => ({ label: `${n.name} (${n.ip}) · rank ${n.node_rank}`, value: n.node_id }))"
            />
          </UFormField>
          <div class="mt-3">
            <div class="text-sm text-gray-600 mb-2">Worker 节点（可多选，参与分布式服务）</div>
            <div class="grid grid-cols-2 gap-2">
              <label
                v-for="n in plan.nodes.filter((x: any) => x.node_id !== headNodeId)"
                :key="n.node_id"
                class="flex items-center gap-2 p-2 rounded-md border border-gray-200 dark:border-gray-700 cursor-pointer"
                :class="{ 'border-primary': workerIds.includes(n.node_id) }"
              >
                <UCheckbox :model-value="workerIds.includes(n.node_id)" @update:model-value="() => toggleWorker(n.node_id)" />
                <div class="text-sm">
                  <div>{{ n.name }} <span class="text-gray-400 text-xs">{{ n.ip }}</span></div>
                  <div class="text-[11px] text-gray-400">
                    rank {{ n.node_rank }} · {{ n.auto_vars.node_roce_ip || '无RoCE' }} · {{ n.auto_vars.hca || '—' }}
                  </div>
                </div>
              </label>
            </div>
            <div class="text-xs mt-2" :class="nodeCountOk ? 'text-gray-400' : 'text-warning'">
              已选 {{ selectedNodes.length }} 个节点
              <template v-if="nodeMin">（配方要求 ≥ {{ nodeMin }}：head + 至少 {{ nodeMin - 1 }} 个 worker）</template>
            </div>
          </div>
        </UCard>

        <UCard v-if="recipe && userVars.length">
          <template #header><div class="font-semibold">3. 配方变量</div></template>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <UFormField v-for="v in userVars" :key="v.key" :label="v.label || v.key" :hint="v.help">
              <div v-if="v.picker" class="flex gap-2">
                <UInput v-model="varValues[v.key]" :placeholder="v.default || ''" class="flex-1" />
                <UButton size="sm" variant="outline" @click="openPicker(v)">
                  {{ v.picker === 'model' ? '选择模型' : '选择镜像' }}
                </UButton>
              </div>
              <USelect
                v-else-if="v.type === 'select'"
                v-model="varValues[v.key]"
                :items="(v.options || []).map((o: string) => ({ label: o, value: o }))"
              />
              <UCheckbox
                v-else-if="v.type === 'bool'"
                v-model="varValues[v.key]"
                :label="varValues[v.key] === 'true' ? 'true' : 'false'"
              />
              <UInput v-else v-model="varValues[v.key]" :placeholder="v.default || ''" />
            </UFormField>
          </div>
        </UCard>

        <UModal v-model:open="pickerOpen">
          <template #content>
            <UCard>
              <template #header>
                <div class="font-semibold">{{ pickerVar?.picker === 'model' ? '已下载模型' : '已拉取镜像' }}</div>
              </template>
              <div v-if="pickerLoading" class="py-6 text-center text-sm text-gray-400">加载中…</div>
              <div v-else-if="!pickerItems.length" class="py-6 text-center text-sm text-gray-400">
                暂无{{ pickerVar?.picker === 'model' ? '已下载模型' : '已拉取镜像' }}，请先在「模型」/「镜像」页下载或拉取
              </div>
              <div v-else class="divide-y divide-gray-100 dark:divide-gray-800 -mx-3">
                <button
                  v-for="item in pickerItems"
                  :key="item.name"
                  :disabled="!item.complete"
                  class="w-full flex items-center justify-between gap-2 px-3 py-2.5 text-left hover:bg-gray-50 dark:hover:bg-gray-800/60 disabled:cursor-not-allowed disabled:opacity-50"
                  @click="pickItem(item)"
                >
                  <span class="font-mono text-sm break-all min-w-0">{{ item.name }}</span>
                  <span class="flex items-center gap-2 shrink-0">
                    <UBadge
                      v-if="item.statusLabel"
                      size="xs"
                      :color="item.statusLabel === '下载失败' ? 'error' : 'warning'"
                      variant="subtle"
                    >{{ item.statusLabel }}</UBadge>
                    <span class="text-xs text-gray-400">{{ fmt(item.size) }}</span>
                  </span>
                </button>
              </div>
            </UCard>
          </template>
        </UModal>

        <UCard v-if="modelRepo && plan">
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">模型缓存状态（{{ modelRepo }}）</div>
              <div class="flex items-center gap-2">
                <UButton
                  v-if="modelIncomplete && !transferring"
                  size="xs"
                  color="primary"
                  variant="soft"
                  @click="startModelTransfer"
                >
                  发送模型
                </UButton>
                <UButton size="xs" variant="ghost" :loading="modelChecking" @click="checkModel">刷新</UButton>
              </div>
            </div>
          </template>
          <div v-if="Object.keys(modelStatus).length" class="space-y-1.5 text-sm">
            <div v-for="n in selectedNodes" :key="n.node_id" class="flex items-center justify-between">
              <span>{{ n.name }}</span>
              <UBadge
                :color="modelStatus[n.node_id]?.complete ? 'success' : modelStatus[n.node_id]?.cached ? 'warning' : 'error'"
                variant="subtle"
              >
                {{ modelStatus[n.node_id]?.complete ? '已就绪' : modelStatus[n.node_id]?.cached ? '部分缓存' : modelStatus[n.node_id]?.error || '未缓存' }}
              </UBadge>
            </div>
            <div v-if="transferring" class="text-xs text-primary pt-1">
              模型传输中
              <template v-if="transferJob">
                （{{ ['downloading', 'sending', 'syncing'][['downloading', 'sending', 'syncing'].indexOf(transferJob.status)] || transferJob.status }}
                <span v-if="transferJob.total_bytes">· {{ ((transferJob.downloaded_bytes || 0) / transferJob.total_bytes * 100).toFixed(0) }}%</span>）
              </template>
              ，完成后发布按钮自动解锁
            </div>
            <div class="text-xs text-gray-400 pt-1">
              模型未完整就绪时，发布会自动启动管理传输：控制平面下载（仅一份）→ 管理网发送 head → RoCE 同步 worker，不会各节点同时联网下载。
            </div>
          </div>
        </UCard>

        <UCard v-if="imageRepo && plan">
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">镜像状态（{{ imageRepo }}）</div>
              <div class="flex items-center gap-2">
                <UButton
                  v-if="imageIncomplete && !imageTransferring"
                  size="xs"
                  color="primary"
                  variant="soft"
                  @click="startImageTransfer"
                >
                  发送镜像
                </UButton>
                <UButton size="xs" variant="ghost" :loading="imageChecking" @click="checkImage">刷新</UButton>
              </div>
            </div>
          </template>
          <div v-if="Object.keys(imageStatus).length" class="space-y-1.5 text-sm">
            <div v-for="n in selectedNodes" :key="n.node_id" class="flex items-center justify-between">
              <span>{{ n.name }}</span>
              <UBadge
                :color="imageStatus[n.node_id]?.present ? 'success' : 'error'"
                variant="subtle"
              >
                {{ imageStatus[n.node_id]?.present ? '已就绪' : imageStatus[n.node_id]?.error || '未缓存' }}
              </UBadge>
            </div>
            <div v-if="imageTransferring" class="text-xs text-primary pt-1">
              镜像传输中
              <template v-if="imageTransferJob">
                （{{ imageTransferJob.status || '...' }}
                <span v-if="imageTransferJob.sent_bytes || imageTransferJob.downloaded_bytes">
                  · {{ ((imageTransferJob.sent_bytes || 0) / (imageTransferJob.size_bytes || imageTransferJob.downloaded_bytes || 1) * 100).toFixed(0) }}%</span>）
              </template>
              ，完成后发布按钮自动解锁
            </div>
            <div class="text-xs text-gray-400 pt-1">
              镜像未就绪时，发布会自动启动管理传输：控制平面归档 → 管理网发送 head → RoCE 同步 worker → 各节点 docker load，不会各节点同时联网拉取。
            </div>
          </div>
        </UCard>

        <UCard v-if="recipe && clusterVars.length && plan">
          <template #header><div class="font-semibold">集群自动变量（自动填充，可修改）</div></template>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <UFormField v-for="v in clusterVars" :key="v.key" :label="`${v.label || v.key} (${v.key})`">
              <UInput :model-value="clusterVarValue(v)" disabled />
            </UFormField>
          </div>
          <div class="text-xs text-gray-400 mt-2">
            节点级变量（NODE_RANK / VLLM_HOST_IP / NCCL_IB_HCA / 网卡 / GID index）已按各节点自动填充，详见节点勾选列表。
          </div>
        </UCard>
      </div>

      <div class="space-y-4">
        <UCard>
          <template #header><div class="font-semibold">4. 任务信息</div></template>
          <UFormField label="任务名" required>
            <UInput v-model="taskName" placeholder="如 deepseek-2x-01（小写字母数字）" />
          </UFormField>
          <UFormField label="模型发送（与任务解耦）" class="mt-3">
            <UCheckbox v-model="sendModel" label="发布前确保模型已发送到节点（缺失时自动：控制平面下载 → 管理网发送 head → RoCE 同步 worker）" />
          </UFormField>
          <UFormField label="镜像发送（与任务解耦）" class="mt-3">
            <UCheckbox v-model="sendImage" label="发布前确保镜像已发送到节点（缺失时自动：控制平面归档 → head → RoCE 同步 → 节点 docker load）" />
          </UFormField>
          <div class="mt-3 space-y-2">
            <UButton block color="primary" :loading="previewing" :disabled="!recipeId || !clusterId || !headNodeId" @click="doPreview">
              预览渲染
            </UButton>
            <UButton
              block
              color="primary"
              variant="solid"
              :loading="publishing"
              :disabled="!taskName || !recipeId || !clusterId || !headNodeId || (sendModel && modelIncomplete) || (sendImage && imageIncomplete) || !nodeCountOk"
              @click="publish"
            >
              发布任务
            </UButton>
            <div v-if="!nodeCountOk" class="text-xs text-warning text-center">
              该配方需要至少 {{ nodeMin }} 个节点（head + {{ nodeMin - 1 }} 个 worker），当前已选 {{ selectedNodes.length }} 个
            </div>
            <div v-if="sendModel && modelIncomplete" class="text-xs text-warning text-center">
              模型未完整就绪，请先点击「发送模型」完成后发布
            </div>
            <div v-if="sendImage && imageIncomplete" class="text-xs text-warning text-center">
              镜像未就绪，请先点击「发送镜像」完成后发布
            </div>
          </div>
          <div class="text-xs text-gray-400 mt-3">
            发布将按「worker 先、head 后」顺序在节点上执行 docker compose up，并在后台轮询 head 的 vLLM 健康检查。
          </div>
        </UCard>

        <UCard v-if="preview">
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">渲染预览</div>
              <div class="text-xs text-gray-400">各节点 env 关键值</div>
            </div>
          </template>
          <div v-for="(payload, nodeId) in preview.nodes" :key="nodeId" class="mb-3">
            <div class="text-xs font-semibold mb-1">{{ payload.role }} · node {{ nodeId }} · rank {{ payload.node_rank }}</div>
            <pre class="bg-gray-50 dark:bg-gray-900 rounded p-2 text-[11px] overflow-x-auto">
{{ Object.entries(payload.env).filter(([k]) => ['NODE_RANK', 'VLLM_HOST_IP', 'MASTER_ADDR', 'NCCL_IB_HCA', 'NCCL_SOCKET_IFNAME', 'NCCL_IB_GID_INDEX', 'NODES_TOTAL'].includes(k)).map(([k, v]) => `${k}=${v}`).join('\n') }}
            </pre>
          </div>
        </UCard>
      </div>
    </div>
  </div>
</template>
