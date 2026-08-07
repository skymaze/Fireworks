<script setup lang="ts">
const { t } = useI18n()
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
        complete: '', downloading: t('status.downloading'), failed: t('status.failed'), partial: t('status.partial'),
      }
      pickerItems.value = (r.models || []).map((m: any) => ({
        name: m.repo,
        size: m.size_bytes,
        complete: !!m.complete,
        status: m.status,
        statusLabel: labels[m.status] || '',
      }))
    } else if (v.picker === 'image') {
      const r = await api.get('/images/local')
      // 只显示已完整拉取的归档（无关联镜像名的孤儿归档不展示）
      pickerItems.value = (r.archives || [])
        .filter((a: any) => a.image)
        .map((a: any) => ({ name: a.image, size: a.size_bytes, complete: true, status: 'complete', statusLabel: '' }))
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
        modelStatus.value[n.node_id] = { complete: false, cached: false, error: t('tasks.node_unreachable') }
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
            error.value = t('tasks.model_transfer_fail', { error: cur.error || t('common.unknown_error') })
          } else if (cur.status === 'cancelled' || cur.status === 'paused') {
            // 用户在其他页面暂停/取消了传输：停止轮询，保持未就绪状态
            clearInterval(transferTimer!)
            transferTimer = null
            transferring.value = false
            transferJob.value = null
            if (cur.status === 'cancelled') error.value = t('tasks.model_transfer_cancelled')
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
        imageStatus.value[n.node_id] = { present: false, error: t('tasks.node_unreachable') }
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
            error.value = t('tasks.image_transfer_fail', { error: cur.error || t('common.unknown_error') })
          } else if (cur.status === 'cancelled' || cur.status === 'paused') {
            // 用户在其他页面暂停/取消了传输：停止轮询，保持未就绪状态
            clearInterval(imageTransferTimer!)
            imageTransferTimer = null
            imageTransferring.value = false
            imageTransferJob.value = null
            if (cur.status === 'cancelled') error.value = t('tasks.image_transfer_cancelled')
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
      <UButton size="sm" variant="ghost" to="/tasks">{{ $t('common.back') }}</UButton>
      <h1 class="text-xl font-bold">{{ $t('tasks.publish') }}</h1>
    </div>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />
    <UAlert v-if="planWarnings.length" :title="planWarnings.join($t('common.semi_sep'))" color="warning" class="mb-4" />

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <div class="lg:col-span-2 space-y-4">
        <UCard>
          <template #header><div class="font-semibold">{{ $t('tasks.step1') }}</div></template>
          <div class="grid grid-cols-2 gap-4">
            <UFormField :label="$t('tasks.col_recipe')" required>
              <USelectMenu
                v-model="recipeId"
                :items="recipes.map((r) => ({ label: r.name, value: r.id }))"
                :placeholder="$t('tasks.recipe_placeholder')"
              />
            </UFormField>
            <UFormField :label="$t('tasks.col_cluster')" required>
              <USelectMenu
                v-model="clusterId"
                :items="clusters.map((c) => ({ label: $t('tasks.cluster_item', { name: c.name, count: c.members?.length || 0 }), value: c.id }))"
                :placeholder="$t('tasks.cluster_placeholder')"
              />
            </UFormField>
          </div>
          <div class="mt-3 text-xs text-gray-500">{{ recipe?.description || $t('tasks.recipe_desc_placeholder') }}</div>
        </UCard>

        <UCard v-if="plan">
          <template #header><div class="font-semibold">{{ $t('tasks.step2') }}</div></template>
          <UFormField :label="$t('tasks.head_node')" required>
            <USelectMenu
              v-model="headNodeId"
              :items="plan.nodes.map((n: any) => ({ label: `${n.name} (${n.ip}) · rank ${n.node_rank}`, value: n.node_id }))"
            />
          </UFormField>
          <div class="mt-3">
            <div class="text-sm text-gray-600 mb-2">{{ $t('tasks.worker_nodes') }}</div>
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
                    {{ $t('tasks.wire_info', { rank: n.node_rank, roce: n.auto_vars.node_roce_ip || $t('tasks.no_roce_short'), hca: n.auto_vars.hca || '—' }) }}
                  </div>
                </div>
              </label>
            </div>
            <div class="text-xs mt-2" :class="nodeCountOk ? 'text-gray-400' : 'text-warning'">
              {{ $t('tasks.nodes_selected', { count: selectedNodes.length }) }}
              <template v-if="nodeMin">{{ $t('tasks.node_min_note', { min: nodeMin, workers: nodeMin - 1 }) }}</template>
            </div>
          </div>
        </UCard>

        <UCard v-if="recipe && userVars.length">
          <template #header><div class="font-semibold">{{ $t('tasks.step3') }}</div></template>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <UFormField v-for="v in userVars" :key="v.key" :label="v.label || v.key" :hint="v.help">
              <div v-if="v.picker" class="flex gap-2">
                <UInput v-model="varValues[v.key]" :placeholder="v.default || ''" class="flex-1" />
                <UButton size="sm" variant="outline" @click="openPicker(v)">
                  {{ v.picker === 'model' ? $t('tasks.pick_model') : $t('tasks.pick_image') }}
                </UButton>
              </div>
              <USelectMenu
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
                <div class="font-semibold">{{ pickerVar?.picker === 'model' ? $t('tasks.picker_models_title') : $t('tasks.picker_images_title') }}</div>
              </template>
              <div v-if="pickerLoading" class="py-6 text-center text-sm text-gray-400">{{ $t('common.loading') }}</div>
              <div v-else-if="!pickerItems.length" class="py-6 text-center text-sm text-gray-400">
                {{ $t('tasks.picker_empty', { picker: pickerVar?.picker === 'model' ? $t('tasks.picker_models_title') : $t('tasks.picker_images_title') }) }}
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
                      :color="item.status === 'failed' ? 'error' : 'warning'"
                      variant="subtle"
                    >{{ item.statusLabel }}</UBadge>
                    <span class="text-xs text-gray-400">{{ fmtBytes(item.size) }}</span>
                  </span>
                </button>
              </div>
            </UCard>
          </template>
        </UModal>

        <UCard v-if="modelRepo && plan">
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">{{ $t('tasks.model_status_title', { repo: modelRepo }) }}</div>
              <div class="flex items-center gap-2">
                <UButton
                  v-if="modelIncomplete && !transferring"
                  size="xs"
                  color="primary"
                  variant="soft"
                  @click="startModelTransfer"
                >
                  {{ $t('tasks.send_model') }}
                </UButton>
                <UButton size="xs" variant="ghost" :loading="modelChecking" @click="checkModel">{{ $t('common.refresh') }}</UButton>
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
                {{ modelStatus[n.node_id]?.complete ? $t('tasks.ready') : modelStatus[n.node_id]?.cached ? $t('tasks.partial_cache') : modelStatus[n.node_id]?.error || $t('tasks.not_cached') }}
              </UBadge>
            </div>
            <div v-if="transferring" class="text-xs text-primary pt-1">
              {{ $t('tasks.model_transferring') }}
              <template v-if="transferJob">
                （{{ statusLabel(transferJob.status) }}
                <span v-if="transferJob.total_bytes">· {{ ((transferJob.downloaded_bytes || 0) / transferJob.total_bytes * 100).toFixed(0) }}%</span>）
              </template>
              {{ $t('tasks.transfer_unlock') }}
            </div>
            <div class="text-xs text-gray-400 pt-1">
              {{ $t('tasks.model_transfer_note') }}
            </div>
          </div>
        </UCard>

        <UCard v-if="imageRepo && plan">
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">{{ $t('tasks.image_status_title', { repo: imageRepo }) }}</div>
              <div class="flex items-center gap-2">
                <UButton
                  v-if="imageIncomplete && !imageTransferring"
                  size="xs"
                  color="primary"
                  variant="soft"
                  @click="startImageTransfer"
                >
                  {{ $t('tasks.send_image') }}
                </UButton>
                <UButton size="xs" variant="ghost" :loading="imageChecking" @click="checkImage">{{ $t('common.refresh') }}</UButton>
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
                {{ imageStatus[n.node_id]?.present ? $t('tasks.ready') : imageStatus[n.node_id]?.error || $t('tasks.not_cached') }}
              </UBadge>
            </div>
            <div v-if="imageTransferring" class="text-xs text-primary pt-1">
              {{ $t('tasks.image_transferring') }}
              <template v-if="imageTransferJob">
                （{{ statusLabel(imageTransferJob.status) || '...' }}
                <span v-if="imageTransferJob.sent_bytes || imageTransferJob.downloaded_bytes">
                  · {{ ((imageTransferJob.sent_bytes || 0) / (imageTransferJob.size_bytes || imageTransferJob.downloaded_bytes || 1) * 100).toFixed(0) }}%</span>）
              </template>
              {{ $t('tasks.transfer_unlock') }}
            </div>
            <div class="text-xs text-gray-400 pt-1">
              {{ $t('tasks.image_transfer_note') }}
            </div>
          </div>
        </UCard>

        <UCard v-if="recipe && clusterVars.length && plan">
          <template #header><div class="font-semibold">{{ $t('tasks.cluster_auto_vars') }}</div></template>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <UFormField v-for="v in clusterVars" :key="v.key" :label="`${v.label || v.key} (${v.key})`">
              <UInput :model-value="clusterVarValue(v)" disabled />
            </UFormField>
          </div>
          <div class="text-xs text-gray-400 mt-2">
            {{ $t('tasks.cluster_auto_note') }}
          </div>
        </UCard>
      </div>

      <div class="space-y-4">
        <UCard>
          <template #header><div class="font-semibold">{{ $t('tasks.step4') }}</div></template>
          <UFormField :label="$t('tasks.task_name')" required>
            <UInput v-model="taskName" :placeholder="$t('tasks.task_name_placeholder')" />
          </UFormField>
          <UFormField :label="$t('tasks.send_model_label')" class="mt-3">
            <UCheckbox v-model="sendModel" :label="$t('tasks.send_model_label_detail')" />
          </UFormField>
          <UFormField :label="$t('tasks.send_image_label')" class="mt-3">
            <UCheckbox v-model="sendImage" :label="$t('tasks.send_image_label_detail')" />
          </UFormField>
          <div class="mt-3 space-y-2">
            <UButton block color="primary" :loading="previewing" :disabled="!recipeId || !clusterId || !headNodeId" @click="doPreview">
              {{ $t('tasks.preview_render') }}
            </UButton>
            <UButton
              block
              color="primary"
              variant="solid"
              :loading="publishing"
              :disabled="!taskName || !recipeId || !clusterId || !headNodeId || (sendModel && modelIncomplete) || (sendImage && imageIncomplete) || !nodeCountOk"
              @click="publish"
            >
              {{ $t('tasks.publish') }}
            </UButton>
            <div v-if="!nodeCountOk" class="text-xs text-warning text-center">
              {{ $t('tasks.node_min_warning', { min: nodeMin, workers: nodeMin - 1, selected: selectedNodes.length }) }}
            </div>
            <div v-if="sendModel && modelIncomplete" class="text-xs text-warning text-center">
              {{ $t('tasks.model_incomplete_warning') }}
            </div>
            <div v-if="sendImage && imageIncomplete" class="text-xs text-warning text-center">
              {{ $t('tasks.image_incomplete_warning') }}
            </div>
          </div>
          <div class="text-xs text-gray-400 mt-3">
            {{ $t('tasks.publish_note') }}
          </div>
        </UCard>

        <UCard v-if="preview">
          <template #header>
            <div class="flex items-center justify-between">
              <div class="font-semibold">{{ $t('tasks.render_preview') }}</div>
              <div class="text-xs text-gray-400">{{ $t('tasks.env_keys') }}</div>
            </div>
          </template>
          <div v-for="(payload, nodeId) in preview.nodes" :key="nodeId" class="mb-3">
            <div class="text-xs font-semibold mb-1">{{ $t('tasks.preview_node', { role: statusLabel(payload.role), id: nodeId, rank: payload.node_rank }) }}</div>
            <pre class="bg-gray-50 dark:bg-gray-900 rounded p-2 text-[11px] overflow-x-auto">
{{ Object.entries(payload.env).filter(([k]) => ['NODE_RANK', 'VLLM_HOST_IP', 'MASTER_ADDR', 'NCCL_IB_HCA', 'NCCL_SOCKET_IFNAME', 'NCCL_IB_GID_INDEX', 'NODES_TOTAL'].includes(k)).map(([k, v]) => `${k}=${v}`).join('\n') }}
            </pre>
          </div>
        </UCard>
      </div>
    </div>
  </div>
</template>
