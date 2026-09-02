<script setup lang="ts">
const { t } = useI18n()
const api = useApi()
const router = useRouter()
const route = useRoute()
const toast = useToast()
// 发布用的配方来自本地（安装时已按用户语言快照成单语言），直接展示原字段

const recipes = ref<any[]>([])
const clusters = ref<any[]>([])
const recipeId = ref<number | null>(null)
const clusterId = ref<number | null>(null)
const plan = ref<any>(null)
const taskName = ref('')
const headNodeId = ref<number | null>(null)
const workerIds = ref<number[]>([])
// 任务级 head/worker/rank：每个选中节点在本次任务里的 rank（head 固定 0，worker 可编辑）
const nodeRanks = ref<Record<number, number>>({})
const sendModel = ref(true)  // 发布前确保模型发送到节点（缺失则自动管理传输）
const sendImage = ref(true)  // 发布前确保镜像发送到节点（缺失则自动管理传输）
const varValues = reactive<Record<string, string>>({})
const preview = ref<any>(null)
const previewing = ref(false)
const publishing = ref(false)

const recipe = computed(() => recipes.value.find((r) => r.id === recipeId.value))
const userVars = computed(() => (recipe.value?.variables || []).filter((v: any) => v.source === 'user'))
const clusterVars = computed(() => (recipe.value?.variables || []).filter((v: any) => v.source === 'cluster'))

// 快速选择：已下载模型 / 已拉取镜像
const pickerOpen = ref(false)
const pickerVar = ref<any>(null)
const pickerItems = ref<any[]>([])
const pickerLoading = ref(false)
// 本地模型明细（版本列表）：供模型变量选择「发布固定版本」
const localModelsDetail = ref<any[]>([])
const shortSha = (s?: string) => (s ? s.slice(0, 7) : '')

async function loadLocalModelsDetail() {
  try {
    localModelsDetail.value = (await api.get('/models/local')).models || []
  } catch { /* ignore */ }
}

/**
 * 模型版本钉扎：{repo: commit sha}。选定后：
 *  - 发布时随 model_pins 提交，任务按该版本分发（节点缺该版本则差量补齐）；
 *  - '' 表示发布最新（main 当前激活版本）。
 * UI 上 USelectMenu 不支持空字符串选项，用哨兵 'latest' 表示「最新」。
 */
const modelPins = reactive<Record<string, string>>({})
const PIN_LATEST = 'latest'
const pinedRepo = (repo: string) => modelPins[repo] || ''

function pinOptions(repo: string): { label: string; value: string }[] {
  const m = localModelsDetail.value.find((x) => x.repo === repo)
  const opts: { label: string; value: string }[] = [{
    label: t('tasks.pin_latest', { sha: shortSha(m?.active_sha) }),
    value: PIN_LATEST,
  }]
  if (m) {
    for (const v of m.versions || []) {
      if (!v.complete) continue
      opts.push({
        label: shortSha(v.sha) + (v.sha === m.active_sha ? ` · ${t('tasks.pin_active')}` : ''),
        value: v.sha,
      })
    }
  }
  return opts
}

function setModelPin(repo: string, val: string) {
  if (val === PIN_LATEST || !val) delete modelPins[repo]
  else modelPins[repo] = val
}

async function openPicker(v: any) {
  pickerVar.value = v
  pickerItems.value = []
  pickerOpen.value = true
  pickerLoading.value = true
  try {
    if (v.picker === 'model') {
      const r = await api.get('/models/local')
      // 只允许选择已下载完成的模型；未完成（下载中/发送/同步/失败/残留）显示状态并禁用
      const labels: Record<string, string> = {
        complete: '', downloading: t('status.downloading'), sending: t('status.sending'), syncing: t('status.syncing'), failed: t('status.failed'), partial: t('status.partial'),
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
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    pickerLoading.value = false
  }
}

function pickItem(item: any) {
  if (!item.complete) return // 未下载完成的模型不可选
  if (pickerVar.value) varValues[pickerVar.value.key] = item.name
  pickerOpen.value = false
}

// 模型缓存状态：modelStatus[repo][node_id]
const modelStatus = ref<Record<string, Record<string, any>>>({})
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

// ---- 任务级 head/worker/rank 分配（随任务保存，与集群成员解耦）----
function nextFreeRank(): number {
  const used = new Set(Object.values(nodeRanks.value))
  let r = 1
  while (used.has(r)) r++
  return r
}

// 确保每个选中节点有合法 rank：head=0；worker 缺省时自动补不冲突的 rank
function ensureRanks() {
  const ids = new Set(selectedNodes.value.map((n: any) => n.node_id))
  for (const k of Object.keys(nodeRanks.value)) {
    if (!ids.has(Number(k))) delete nodeRanks.value[Number(k)]
  }
  if (headNodeId.value != null) nodeRanks.value[headNodeId.value] = 0
  for (const n of selectedNodes.value) {
    if (n.node_id !== headNodeId.value && nodeRanks.value[n.node_id] == null) {
      nodeRanks.value[n.node_id] = nextFreeRank()
    }
  }
}

// ---- 节点选择（选座式）：同一 grid 里选 head/worker，占用节点置灰不可选 ----
const planLoading = ref(false)

function nodeCardClass(n: any) {
  if (n?.busy) return 'border-gray-300 dark:border-gray-700 bg-gray-100 dark:bg-gray-800/40 opacity-60 cursor-not-allowed'
  if (headNodeId.value === n.node_id) return 'border-primary ring-1 ring-primary bg-primary/10 cursor-pointer'
  if (workerIds.value.includes(n.node_id)) return 'border-primary bg-primary/5 cursor-pointer'
  return 'border-gray-200 dark:border-gray-700 hover:border-primary/70 cursor-pointer'
}

function toggleNode(n: any) {
  if (n?.busy) return
  const id = n.node_id
  if (headNodeId.value === id) {
    // 取消当前 head：已有 worker 时自动提升第一个为 head
    headNodeId.value = null
    const next = workerIds.value[0]
    if (next != null) {
      headNodeId.value = next
      workerIds.value.splice(workerIds.value.indexOf(next), 1)
    }
  } else if (workerIds.value.includes(id)) {
    workerIds.value.splice(workerIds.value.indexOf(id), 1)
  } else if (headNodeId.value == null) {
    headNodeId.value = id
  } else {
    workerIds.value.push(id)
  }
  ensureRanks()
}

// 显式指定某节点为 head：原 head（若仍选中）自动降为 worker，rank 由 watcher 重排
function setHead(n: any) {
  if (n?.busy || headNodeId.value === n.node_id) return
  const prev = headNodeId.value
  headNodeId.value = n.node_id
  const i = workerIds.value.indexOf(n.node_id)
  if (i >= 0) workerIds.value.splice(i, 1)
  if (prev != null && !workerIds.value.includes(prev)) workerIds.value.push(prev)
  ensureRanks()
}

// 重新拉取集群 plan（占用状态可能已变化），移出变占用的节点后自动改选空闲节点
async function loadPlan() {
  if (!clusterId.value) return
  planLoading.value = true
  try {
    const p = await api.get(`/clusters/${clusterId.value}/plan`)
    plan.value = p
    const busyIds = new Set((p.nodes || []).filter((x: any) => x.busy).map((x: any) => x.node_id))
    if (headNodeId.value != null && busyIds.has(headNodeId.value)) headNodeId.value = null
    workerIds.value = workerIds.value.filter((id) => !busyIds.has(id))
    // head/worker 因占用被移空后，自动改选并补齐空闲节点（固定拓扑按需选满）
    autoPickFreeNodes()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    planLoading.value = false
  }
}

// 自动预选空闲节点（像购票按序自动连座）：head 取第一个空闲节点；
// 固定拓扑配方自动选满 N 台（head + 前 N-1 个空闲 worker），空闲不足则全选
function autoPickFreeNodes() {
  if (!plan.value) return
  const free = plan.value.nodes.filter((x: any) => !x.busy)
  if (headNodeId.value == null || !free.some((x: any) => x.node_id === headNodeId.value)) {
    headNodeId.value = free[0]?.node_id || null
  }
  if (fixedNodeCount.value) {
    workerIds.value = free
      .filter((x: any) => x.node_id !== headNodeId.value)
      .map((x: any) => x.node_id)
      .slice(0, fixedNodeCount.value - 1)
  }
  ensureRanks()
}

// 提交给 preview / 发布的后端节点分配
const assignments = computed(() =>
  selectedNodes.value.map((n: any) => ({
    node_id: n.node_id,
    role: n.node_id === headNodeId.value ? 'head' : 'worker',
    node_rank: (n.node_id === headNodeId.value ? 0 : nodeRanks.value[n.node_id]) ?? 0,
  })),
)

// rank 冲突校验（后端同样拒绝，本地提前提示并禁用发布）
const rankConflicts = computed(() => {
  const counts: Record<number, number> = {}
  for (const a of assignments.value) counts[a.node_rank] = (counts[a.node_rank] || 0) + 1
  return Object.entries(counts).filter(([, c]) => c > 1).map(([r]) => Number(r))
})

// 模型/镜像仓库列表：每个 picker=="model"（/=="image"）变量各算一个（可多个），
// 逐个检查/保障/传输；按 picker 动态取键，与后端 tasks.py 一致。
const modelRepos = computed<string[]>(() =>
  Array.from(new Set(
    userVars.value
      .filter((x: any) => x.picker === 'model')
      .map((v: any) => varValues[v.key] || v.default)
      .filter((repo: any) => !!repo),
  )) as string[],
)

// 全部选中节点上所有模型完整（发布按钮前置条件）
const allComplete = computed(() => {
  if (!modelRepos.value.length || !selectedNodes.value.length) return false
  return modelRepos.value.every((repo) =>
    selectedNodes.value.every((n: any) => modelStatus.value[repo]?.[n.node_id]?.complete),
  )
})
// 模型未完整（可点击"发送模型"）
const modelIncomplete = computed(() => !!modelRepos.value.length && !!selectedNodes.value.length && !allComplete.value)

// 自动重查：本次查询后仍有节点未就绪时，延迟 4s 自动重查一次（缓解"分发刚完成、查询过早"窗口）
let modelAutoRetried = false
let modelRetryTimer: ReturnType<typeof setTimeout> | null = null

async function checkModel() {
  if (!modelRepos.value.length || !plan.value) return
  if (modelRetryTimer) clearTimeout(modelRetryTimer)
  modelChecking.value = true
  modelStatus.value = {}
  try {
    for (const repo of modelRepos.value) {
      modelStatus.value[repo] = {}
      for (const n of selectedNodes.value) {
        try {
          // sha 给定则节点按目标 commit 精确校验（版本钉扎）；缺省校验激活版本
          const st = await api.get(`/models/cached/${repo}`, {
            node_id: n.node_id,
            sha: modelPins[repo] || undefined,
          })
          modelStatus.value[repo][n.node_id] = st
        } catch {
          // 节点 agent 不可达：标记出来，避免误判为"未缓存"而触发重复传输
          modelStatus.value[repo][n.node_id] = { complete: false, cached: false, error: t('tasks.node_unreachable') }
        }
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

// 模型传输：控制平面下载 -> 发送 head -> RoCE 同步 worker；多个模型顺序传输，失败/取消即停。
let modelTransferResolve: ((ok: boolean) => void) | null = null

function transferOneModel(repo: string) {
  return new Promise<boolean>((resolve) => {
    modelTransferResolve = resolve
    const head = headNodeId.value || plan.value.nodes[0]?.node_id
    const workers = selectedNodes.value.filter((n: any) => n.node_id !== head).map((n: any) => n.node_id)
    // 版本钉扎：控制平面按该 sha 续传/分发到节点；缺省解析 main 最新
    api.post('/models/download', {
      repo, head_node_id: head, sync_node_ids: workers,
      sha: modelPins[repo] || undefined,
    })
      .then((job) => {
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
                toast.add({ title: t('tasks.model_transfer_fail', { error: cur.error || t('common.unknown_error') }), color: 'error' })
                await checkModel()
                resolve(false)
              } else if (cur.status === 'cancelled' || cur.status === 'paused') {
                // 用户在其他页面暂停/取消了传输：停止轮询，保持未就绪状态
                clearInterval(transferTimer!)
                transferTimer = null
                transferJob.value = null
                if (cur.status === 'cancelled') toast.add({ title: t('tasks.model_transfer_cancelled'), color: 'error' })
                await checkModel()
                resolve(false)
              }
              return
            }
            // 不在 active 列表 = 任务已完成（active 列表不包含 completed）
            clearInterval(transferTimer!)
            transferTimer = null
            transferJob.value = null
            await checkModel()  // 刷新节点缓存状态 -> 发布按钮解锁
            resolve(true)
          } catch { /* ignore */ }
        }, 5000)
      })
      .catch((e) => {
        toast.add({ title: errorMsg(e), color: 'error' })
        resolve(false)
      })
  })
}

async function startModelTransfer() {
  if (!modelRepos.value.length || !plan.value) return
  transferring.value = true
  transferJob.value = null
  try {
    for (const repo of modelRepos.value) {
      // 该模型已全部就绪则跳过
      const ready = selectedNodes.value.every((n: any) => modelStatus.value[repo]?.[n.node_id]?.complete)
      if (ready) continue
      const ok = await transferOneModel(repo)
      if (!ok) break  // 失败/取消：停止后续模型的传输
    }
  } finally {
    transferring.value = false
    transferJob.value = null
    if (transferTimer) { clearInterval(transferTimer); transferTimer = null }
    modelTransferResolve = null
  }
}

// 手动取消模型传输（拉取阶段也可取消：后端标记 cancelled / 作废未完成归档）
async function cancelModelTransfer() {
  if (!transferJob?.value?.id) return
  try {
    await api.post(`/models/downloads/${transferJob.value.id}/cancel`)
    toast.add({ title: t('common.cancel'), color: 'neutral' })
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    if (transferTimer) { clearInterval(transferTimer); transferTimer = null }
    transferring.value = false
    transferJob.value = null
    modelTransferResolve?.(false)  // 终止顺序传输的后续模型
    modelTransferResolve = null
    await checkModel()
  }
}

onUnmounted(() => {
  if (transferTimer) clearInterval(transferTimer)
  if (imageTransferTimer) clearInterval(imageTransferTimer)
  if (modelRetryTimer) clearTimeout(modelRetryTimer)
  if (imageRetryTimer) clearTimeout(imageRetryTimer)
  modelTransferResolve?.(false)
  imageTransferResolve?.(false)
})

// 镜像节点状态（发布前置条件：镜像已分发到节点）；imageStatus[repo][node_id]
const imageStatus = ref<Record<string, Record<string, any>>>({})
const imageChecking = ref(false)
const imageTransferring = ref(false)
const imageTransferJob = ref<any>(null)
let imageTransferTimer: ReturnType<typeof setInterval> | null = null

// 镜像仓库列表：每个标记为镜像快速选择的变量（如 DSPARK_VLLM_IMAGE）各算一个
const imageRepos = computed<string[]>(() =>
  Array.from(new Set(
    userVars.value
      .filter((x: any) => x.picker === 'image')
      .map((v: any) => varValues[v.key] || v.default)
      .filter((repo: any) => !!repo),
  )) as string[],
)

// 全部选中节点镜像就绪（发布按钮前置条件）
const allImageReady = computed(() => {
  if (!imageRepos.value.length || !selectedNodes.value.length) return false
  return imageRepos.value.every((repo) =>
    selectedNodes.value.every((n: any) => imageStatus.value[repo]?.[n.node_id]?.present),
  )
})
const imageIncomplete = computed(() => !!imageRepos.value.length && !!selectedNodes.value.length && !allImageReady.value)

let imageAutoRetried = false
let imageRetryTimer: ReturnType<typeof setTimeout> | null = null

async function checkImage() {
  if (!imageRepos.value.length || !plan.value) return
  if (imageRetryTimer) clearTimeout(imageRetryTimer)
  imageChecking.value = true
  imageStatus.value = {}
  try {
    for (const repo of imageRepos.value) {
      imageStatus.value[repo] = {}
      for (const n of selectedNodes.value) {
        try {
          const st = await api.get('/images/node-status', { image: repo, node_id: n.node_id })
          imageStatus.value[repo][n.node_id] = st
        } catch {
          imageStatus.value[repo][n.node_id] = { present: false, error: t('tasks.node_unreachable') }
        }
      }
    }
  } finally {
    imageChecking.value = false
  }
  if (!imageAutoRetried && !allImageReady.value && selectedNodes.value.length) {
    imageAutoRetried = true
    imageRetryTimer = setTimeout(() => { imageRetryTimer = null; checkImage() }, 4000)
  }
}

// 镜像传输：控制平面归档 -> 发送 head -> RoCE 同步 worker -> 各节点 docker load；
// 多个镜像顺序传输，失败/取消即停。
let imageTransferResolve: ((ok: boolean) => void) | null = null

function transferOneImage(repo: string) {
  return new Promise<boolean>((resolve) => {
    imageTransferResolve = resolve
    const head = headNodeId.value || plan.value.nodes[0]?.node_id
    const workers = selectedNodes.value.filter((n: any) => n.node_id !== head).map((n: any) => n.node_id)
    api.post('/images/transfer', { image: repo, head_node_id: head, sync_node_ids: workers })
      .then((job) => {
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
                toast.add({ title: t('tasks.image_transfer_fail', { error: cur.error || t('common.unknown_error') }), color: 'error' })
                await checkImage()
                resolve(false)
              } else if (cur.status === 'cancelled' || cur.status === 'paused') {
                // 用户在其他页面暂停/取消了传输：停止轮询，保持未就绪状态
                clearInterval(imageTransferTimer!)
                imageTransferTimer = null
                imageTransferJob.value = null
                if (cur.status === 'cancelled') toast.add({ title: t('tasks.image_transfer_cancelled'), color: 'error' })
                await checkImage()
                resolve(false)
              }
              return
            }
            // 不在 active 列表 = 任务已完成（active 已含 failed，不会漏失败）
            clearInterval(imageTransferTimer!)
            imageTransferTimer = null
            imageTransferJob.value = null
            await checkImage()  // 刷新节点镜像状态 -> 发布按钮解锁
            resolve(true)
          } catch { /* ignore */ }
        }, 5000)
      })
      .catch((e) => {
        toast.add({ title: errorMsg(e), color: 'error' })
        resolve(false)
      })
  })
}

async function startImageTransfer() {
  if (!imageRepos.value.length || !plan.value) return
  imageTransferring.value = true
  imageTransferJob.value = null
  try {
    for (const repo of imageRepos.value) {
      // 该镜像已全部就绪则跳过
      const ready = selectedNodes.value.every((n: any) => imageStatus.value[repo]?.[n.node_id]?.present)
      if (ready) continue
      const ok = await transferOneImage(repo)
      if (!ok) break  // 失败/取消：停止后续镜像的传输
    }
  } finally {
    imageTransferring.value = false
    imageTransferJob.value = null
    if (imageTransferTimer) { clearInterval(imageTransferTimer); imageTransferTimer = null }
    imageTransferResolve = null
  }
}

// 手动取消镜像传输（拉取阶段也可取消：后端标记 cancelled / 作废未完成归档）
async function cancelImageTransfer() {
  if (!imageTransferJob?.value?.id) return
  try {
    await api.post(`/images/transfers/${imageTransferJob.value.id}/cancel`)
    toast.add({ title: t('common.cancel'), color: 'neutral' })
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    if (imageTransferTimer) { clearInterval(imageTransferTimer); imageTransferTimer = null }
    imageTransferring.value = false
    imageTransferJob.value = null
    imageTransferResolve?.(false)  // 终止顺序传输的后续镜像
    imageTransferResolve = null
    await checkImage()
  }
}

// 配方（模型/镜像）或集群（plan 节点）/节点选择变化时都刷新缓存状态。
// workerIds 为数组（勾选时原地修改），需 deep 监听，否则勾选 worker 不触发检查
watch([modelRepos, plan, headNodeId, workerIds], checkModel, { deep: true })
watch([imageRepos, plan, headNodeId, workerIds], checkImage, { deep: true })
// 钉扎版本变化 -> 节点按新版本精确复检（缺失则提示发送模型）
watch(() => ({ ...modelPins }), () => { if (plan.value) checkModel() }, { deep: true })

async function loadBase() {
  try {
    ;[recipes.value, clusters.value] = await Promise.all([api.get('/recipes'), api.get('/clusters')])
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  }
  await loadLocalModelsDetail()
  // 支持 ?recipe=<id>：从配方商店「一键下载并运行」跳转时预选配方
  const q = route.query.recipe
  if (q && recipes.value.some((r) => r.id === Number(q))) recipeId.value = Number(q)
}

watch(clusterId, async (id) => {
  plan.value = null
  preview.value = null
  headNodeId.value = null
  workerIds.value = []
  nodeRanks.value = {}
  if (!id) return
  await loadPlan()
  // head/worker/rank 由每次任务自行指定，与集群成员解耦；自动预选空闲节点
  autoPickFreeNodes()
})

watch(recipeId, () => {
  preview.value = null
  // 预填变量默认值
  for (const k of Object.keys(varValues)) delete varValues[k]
  for (const k of Object.keys(modelPins)) delete modelPins[k]
  for (const v of userVars.value) if (v.default != null) varValues[v.key] = String(v.default)
  // 固定拓扑配方确定后，自动预选到目标台数的空闲节点
  autoPickFreeNodes()
})

// head 变化后保持 rank 分配一致：head 恒为 rank0（分布式协调要求 MASTER_ADDR 即 rank0），
// 原 head 若转为 worker 则改用一个不冲突的 rank
watch(headNodeId, (id, old) => {
  if (old != null && old !== id && workerIds.value.includes(old) && nodeRanks.value[old] === 0) {
    nodeRanks.value[old] = nextFreeRank()
  }
  ensureRanks()
})

// 固定拓扑：配方声明的「确切节点数」（node_count，参考 vLLM recipes 按固定数量设备调优）。
// 发布时节点数必须恰好等于该值，不做 min/max 比较；未声明则不限制。
const fixedNodeCount = computed(() => recipe.value?.node_count || null)
const nodeCountOk = computed(() => !fixedNodeCount.value || selectedNodes.value.length === fixedNodeCount.value)

async function doPreview() {
  previewing.value = true
  try {
    preview.value = await api.post(`/recipes/${recipeId.value}/preview`, {
      cluster_id: clusterId.value,
      nodes: assignments.value,
      variables: { ...varValues },
    })
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
    preview.value = null
  } finally {
    previewing.value = false
  }
}

async function publish() {
  publishing.value = true
  try {
    // 版本钉扎提交后端：{repo: sha}，仅记实际钉扎的版本
    const pins: Record<string, string> = {}
    for (const repo of modelRepos.value) if (modelPins[repo]) pins[repo] = modelPins[repo]
    const task = await api.post('/tasks', {
      name: taskName.value,
      recipe_id: recipeId.value,
      cluster_id: clusterId.value,
      nodes: assignments.value,
      variables: { ...varValues },
      model_pins: pins,
      send_model: sendModel.value,
      send_image: sendImage.value,
    })
    router.push(`/tasks/${task.id}`)
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    publishing.value = false
  }
}

onMounted(loadBase)
</script>

<template>
  <UDashboardPanel id="task-publish">
    <template #header>
      <UDashboardNavbar>
        <template #leading>
          <UDashboardSidebarCollapse />
          <UButton size="sm" variant="ghost" to="/tasks">{{ $t('common.back') }}</UButton>
        </template>
        <template #title>{{ $t('tasks.publish') }}</template>
      </UDashboardNavbar>
    </template>
    <template #body>
    <div>

      <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div class="lg:col-span-2 space-y-4">
          <UCard>
            <template #header><div class="font-semibold">{{ $t('tasks.step1') }}</div></template>
            <div class="grid grid-cols-1 gap-4">
              <UFormField :label="$t('tasks.col_recipe')" required>
                <USelectMenu value-key="value"
                  v-model="recipeId"
                  class="w-full"
                  :items="recipes.map((r) => ({ label: r.name, value: r.id }))"
                  :placeholder="$t('tasks.recipe_placeholder')"
                  :ui="{ value: 'min-w-0 flex-1 whitespace-normal! break-words' }"
                />
              </UFormField>
              <UFormField :label="$t('tasks.col_cluster')" required>
                <USelectMenu value-key="value"
                  v-model="clusterId"
                  class="w-full"
                  :items="clusters.map((c) => ({ label: $t('tasks.cluster_item', { name: c.name, count: c.members?.length || 0 }), value: c.id }))"
                  :placeholder="$t('tasks.cluster_placeholder')"
                  :ui="{ value: 'min-w-0 flex-1 whitespace-normal! break-words' }"
                />
              </UFormField>
            </div>
            <div class="mt-3 text-xs text-gray-500">{{ recipe?.description || $t('tasks.recipe_desc_placeholder') }}</div>
          </UCard>

          <UCard v-if="plan">
            <template #header>
              <div class="flex items-center justify-between">
                <div class="font-semibold">{{ $t('tasks.step2') }}</div>
                <UButton size="xs" variant="ghost" :loading="planLoading" @click="loadPlan">{{ $t('common.refresh') }}</UButton>
              </div>
            </template>
            <div class="text-xs text-gray-400 mb-2">{{ $t('tasks.node_pick_hint') }}</div>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <div
                v-for="n in plan.nodes"
                :key="n.node_id"
                role="button"
                :tabindex="n.busy ? -1 : 0"
                :aria-disabled="n.busy || undefined"
                class="p-3 rounded-md border text-left transition-colors select-none"
                :class="nodeCardClass(n)"
                :title="n.busy ? $t('tasks.node_busy_title', { task: n.busy_task }) : undefined"
                @click="toggleNode(n)"
                @keydown.enter.space="toggleNode(n)"
              >
                <div class="flex items-center justify-between gap-2">
                  <span class="text-sm font-medium flex-1 min-w-0 truncate">{{ n.name }}</span>
                  <UBadge v-if="n.busy" size="xs" color="error" variant="subtle">{{ $t('tasks.node_busy') }}</UBadge>
                  <UBadge v-else-if="headNodeId === n.node_id" size="xs" color="primary" variant="solid">{{ $t('tasks.head_badge') }}</UBadge>
                  <UBadge v-else-if="workerIds.includes(n.node_id)" size="xs" color="primary" variant="subtle">{{ $t('tasks.worker_badge') }}</UBadge>
                  <UBadge v-else size="xs" color="neutral" variant="subtle">{{ $t('tasks.node_free') }}</UBadge>
                </div>
                <div class="mt-0.5 text-xs text-gray-400">{{ n.ip }}</div>
                <div class="mt-0.5 text-[11px] text-gray-400">
                  {{ n.auto_vars.node_roce_ip || $t('tasks.no_roce_short') }} · {{ n.auto_vars.hca || '—' }}
                </div>
                <div v-if="n.busy && n.busy_task" class="mt-1 text-[11px] text-error">{{ n.busy_task }}</div>
                <div v-else-if="workerIds.includes(n.node_id)" class="mt-2 flex items-center gap-1.5" @click.stop>
                  <span class="text-[11px] text-gray-400">{{ $t('tasks.col_rank') }}</span>
                  <UInput
                    :model-value="String(nodeRanks[n.node_id] ?? 0)"
                    type="number"
                    min="1"
                    size="xs"
                    class="w-16"
                    @update:model-value="(v: any) => { nodeRanks[n.node_id] = Math.max(0, Number(v)) }"
                  />
                  <UButton size="xs" variant="ghost" color="primary" @click="setHead(n)">{{ $t('tasks.set_head') }}</UButton>
                </div>
              </div>
            </div>
            <div class="mt-2 text-[11px] text-gray-400">{{ $t('tasks.head_rank0_note') }}</div>
            <div v-if="rankConflicts.length" class="text-xs text-warning mt-1">
              {{ $t('tasks.rank_conflict', { ranks: rankConflicts.join(', ') }) }}
            </div>
            <div class="text-xs mt-2" :class="nodeCountOk ? 'text-gray-400' : 'text-warning'">
              {{ $t('tasks.nodes_selected', { count: selectedNodes.length }) }}
              <template v-if="fixedNodeCount">{{ $t('tasks.node_exact_note', { n: fixedNodeCount }) }}</template>
            </div>
          </UCard>

          <UCard v-if="recipe && userVars.length">
            <template #header><div class="font-semibold">{{ $t('tasks.step3') }}</div></template>
            <!-- 每变量一行（label 上、输入跟随、help 一行），避免两列下长 label/help 换行错位 -->
            <div class="space-y-3">
              <UFormField v-for="v in userVars" :key="v.key" :label="v.label || v.key" :hint="v.help">
                <div v-if="v.picker" class="flex gap-2">
                  <UInput v-model="varValues[v.key]" :placeholder="v.default || ''" class="flex-1" />
                  <UButton size="sm" variant="outline" @click="openPicker(v)">
                    {{ v.picker === 'model' ? $t('tasks.pick_model') : $t('tasks.pick_image') }}
                  </UButton>
                </div>
                <USelectMenu value-key="value"
                  v-else-if="v.type === 'select'"
                  v-model="varValues[v.key]"
                  :items="(v.options || []).map((o: string) => ({ label: o, value: o }))"
                />
                <UCheckbox
                  v-else-if="v.type === 'bool'"
                  v-model="varValues[v.key]"
                  :label="varValues[v.key] === 'true' ? 'true' : 'false'"
                />
                <UInput v-else v-model="varValues[v.key]" :placeholder="v.default || ''" class="w-full" />
                <!-- 模型变量版本钉扎：至少有两个可选（最新+历史版本）时展示 -->
                <div v-if="v.picker === 'model' && pinOptions(varValues[v.key]).length > 1" class="mt-1.5 flex items-center gap-2">
                  <span class="text-[11px] text-gray-400 shrink-0">{{ $t('tasks.pin_label') }}</span>
                  <USelectMenu
                    size="xs"
                    value-key="value"
                    :model-value="pinedRepo(varValues[v.key]) || PIN_LATEST"
                    :items="pinOptions(varValues[v.key])"
                    @update:model-value="(val: any) => setModelPin(varValues[v.key], val)"
                  />
                </div>
              </UFormField>
            </div>
          </UCard>

          <UModal
            v-model:open="pickerOpen"
            :title="pickerVar?.picker === 'model' ? $t('tasks.picker_models_title') : $t('tasks.picker_images_title')"
          >
            <template #body>
                <div v-if="pickerLoading" class="py-6 text-center text-sm text-gray-400">{{ $t('common.loading') }}</div>
                <div v-else-if="!pickerItems.length" class="py-6 text-center text-sm text-gray-400">
                  {{ $t('tasks.picker_empty', { picker: pickerVar?.picker === 'model' ? $t('tasks.picker_models_title') : $t('tasks.picker_images_title') }) }}
                </div>
                <div v-else class="divide-y divide-default">
                  <UButton
                    v-for="item in pickerItems"
                    :key="item.name"
                    :disabled="!item.complete"
                    color="neutral"
                    variant="ghost"
                    class="w-full justify-between rounded-none px-3 py-2.5 text-left"
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
                  </UButton>
                </div>
            </template>
          </UModal>

          <UCard v-if="modelRepos.length && plan">
            <template #header>
              <div class="flex items-center justify-between">
                <div class="font-semibold">{{ $t('tasks.models_status_title') }}</div>
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
                  <UButton v-if="transferring" size="xs" variant="ghost" color="error" @click="cancelModelTransfer">{{ $t('common.cancel') }}</UButton>
                  <UButton size="xs" variant="ghost" :loading="modelChecking" @click="checkModel">{{ $t('common.refresh') }}</UButton>
                </div>
              </div>
            </template>
            <div v-if="Object.keys(modelStatus).length">
              <div v-for="repo in modelRepos" :key="repo" class="mb-3 last:mb-0">
                <div class="text-xs font-semibold text-gray-500 mb-1">{{ repo }}</div>
                <div class="space-y-1.5 text-sm">
                  <div v-for="n in selectedNodes" :key="n.node_id" class="flex items-center justify-between">
                    <span>{{ n.name }}</span>
                    <UBadge
                      :color="modelStatus[repo]?.[n.node_id]?.complete ? 'success' : modelStatus[repo]?.[n.node_id]?.cached ? 'warning' : 'error'"
                      variant="subtle"
                    >
                      {{ modelStatus[repo]?.[n.node_id]?.complete ? $t('tasks.ready') : modelStatus[repo]?.[n.node_id]?.cached ? $t('tasks.partial_cache') : modelStatus[repo]?.[n.node_id]?.error || $t('tasks.not_cached') }}
                    </UBadge>
                  </div>
                </div>
              </div>
              <div v-if="transferring" class="text-xs text-primary pt-1">
                {{ $t('tasks.model_transferring') }}
                <template v-if="transferJob">
                  （{{ transferJob.repo }} · {{ statusLabel(transferJob.status) }}
                  <span v-if="transferJob.total_bytes">· {{ ((transferJob.downloaded_bytes || 0) / transferJob.total_bytes * 100).toFixed(0) }}%</span>）
                </template>
                {{ $t('tasks.transfer_unlock') }}
              </div>
              <div class="text-xs text-gray-400 pt-1">
                {{ $t('tasks.model_transfer_note') }}
              </div>
            </div>
          </UCard>

          <UCard v-if="imageRepos.length && plan">
            <template #header>
              <div class="flex items-center justify-between">
                <div class="font-semibold">{{ $t('tasks.images_status_title') }}</div>
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
                  <UButton v-if="imageTransferring" size="xs" variant="ghost" color="error" @click="cancelImageTransfer">{{ $t('common.cancel') }}</UButton>
                  <UButton size="xs" variant="ghost" :loading="imageChecking" @click="checkImage">{{ $t('common.refresh') }}</UButton>
                </div>
              </div>
            </template>
            <div v-if="Object.keys(imageStatus).length">
              <div v-for="repo in imageRepos" :key="repo" class="mb-3 last:mb-0">
                <div class="text-xs font-semibold text-gray-500 mb-1">{{ repo }}</div>
                <div class="space-y-1.5 text-sm">
                  <div v-for="n in selectedNodes" :key="n.node_id" class="flex items-center justify-between">
                    <span>{{ n.name }}</span>
                    <UBadge
                      :color="imageStatus[repo]?.[n.node_id]?.present ? 'success' : 'error'"
                      variant="subtle"
                    >
                      {{ imageStatus[repo]?.[n.node_id]?.present ? $t('tasks.ready') : imageStatus[repo]?.[n.node_id]?.error || $t('tasks.not_cached') }}
                    </UBadge>
                  </div>
                </div>
              </div>
              <div v-if="imageTransferring" class="text-xs text-primary pt-1">
                {{ $t('tasks.image_transferring') }}
                <template v-if="imageTransferJob">
                  （{{ imageTransferJob.image }} · {{ statusLabel(imageTransferJob.status) || '...' }}
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
        </div>

        <div class="space-y-4">
          <UCard>
            <template #header><div class="font-semibold">{{ $t('tasks.step4') }}</div></template>
            <UFormField :label="$t('tasks.task_name')" required :hint="$t('tasks.task_name_hint')">
              <UInput v-model="taskName" :placeholder="$t('tasks.task_name_placeholder')"
                      pattern="[a-z0-9][a-z0-9_-]*" />
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
                :disabled="!taskName || !recipeId || !clusterId || !headNodeId || (sendModel && modelIncomplete) || (sendImage && imageIncomplete) || !nodeCountOk || !!rankConflicts.length"
                @click="publish"
              >
                {{ $t('tasks.publish') }}
              </UButton>
              <div v-if="!nodeCountOk" class="text-xs text-warning text-center">
                {{ $t('tasks.node_exact_warning', { n: fixedNodeCount, selected: selectedNodes.length }) }}
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
  </UDashboardPanel>
</template>
