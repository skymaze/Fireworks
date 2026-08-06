<script setup lang="ts">
/** 配方编辑表单（新建/编辑共用） */
const props = defineProps<{ recipe?: any | null }>()
const emit = defineEmits<{ saved: [recipe: any] }>()

const api = useApi()
const toast = useToast()
const saving = ref(false)
const savingVars = ref(false) // 变量自动保存中
const error = ref('')

const form = reactive({
  name: props.recipe?.name || '',
  description: props.recipe?.description || '',
  image: props.recipe?.image || '',
  compose_template: props.recipe?.compose_template || '',
  variables: props.recipe?.variables ? JSON.parse(JSON.stringify(props.recipe.variables)) : [],
})

// ---------- 未保存修改跟踪（保存按钮在右上，离开页面时提醒） ----------
// 基线 = 已持久化状态的表单快照：
// 编辑模式只含基本信息（变量已即时保存，不算脏）；新建含全部（变量随配方一起提交）
const EMPTY_BASELINE = { name: '', description: '', image: '', compose_template: '', variables: [] }
const baseline = ref<any>(props.recipe?.id
  ? {
      name: props.recipe.name, description: props.recipe.description,
      image: props.recipe.image, compose_template: props.recipe.compose_template,
    }
  : JSON.parse(JSON.stringify(EMPTY_BASELINE)),
)
const dirty = computed(() => {
  if (props.recipe?.id) {
    const cur = {
      name: form.name, description: form.description,
      image: form.image, compose_template: form.compose_template,
    }
    return JSON.stringify(cur) !== JSON.stringify(baseline.value)
  }
  // 新建：全部字段与已保存基线比较（含变量）
  const cur = {
    name: form.name, description: form.description, image: form.image,
    compose_template: form.compose_template, variables: form.variables,
  }
  return JSON.stringify(cur) !== JSON.stringify(baseline.value)
})

// 路由离开守卫（点击导航链接时）与刷新/关标签页（beforeunload）双保险
onBeforeRouteLeave(() => {
  if (dirty.value && !confirm('配方有未保存的修改，确定离开吗？')) return false
})
function onBeforeUnload(e: BeforeUnloadEvent) {
  if (dirty.value) e.preventDefault()
}
onMounted(() => window.addEventListener('beforeunload', onBeforeUnload))
onUnmounted(() => window.removeEventListener('beforeunload', onBeforeUnload))

const newVar = reactive({
  key: '', label: '', type: 'string', source: 'user', auto: '', default: '', options: [], required: false, help: '', picker: 'none',
})
// -1 = 新增模式；>=0 = 正在编辑的变量行号
const editingIndex = ref(-1)
// 编辑区容器（打开编辑时滚动到可见位置）
const varEditor = ref<HTMLElement | null>(null)

function resetNewVar() {
  editingIndex.value = -1
  Object.assign(newVar, { key: '', label: '', type: 'string', source: 'user', auto: '', default: '', options: [], required: false, help: '', picker: 'none' })
}

// 变量增删改即时持久化（编辑已有配方时）：先落库成功再更新本地，
// 避免误以为已保存；基本信息仍由「保存配方」按钮统一提交
async function persistVariables(next: any[]) {
  if (!props.recipe?.id) {
    form.variables = next // 新建配方：仅本地，随「保存配方」一起提交
    resetNewVar()
    toast.add({ title: '已加入变量列表，保存配方后生效', color: 'info', timeout: 2000 })
    return
  }
  savingVars.value = true
  try {
    await api.patch(`/recipes/${props.recipe.id}`, { variables: next })
    form.variables = next
    resetNewVar()
    toast.add({ title: '变量已保存', color: 'success', timeout: 2000 })
  } catch (e) {
    error.value = String(e) // 失败：保留编辑状态与表单内容，便于重试
  } finally {
    savingVars.value = false
  }
}

async function addVar() {
  if (!newVar.key) return
  const v = { ...newVar, picker: newVar.picker === 'none' ? '' : newVar.picker }
  const next = editingIndex.value >= 0
    ? form.variables.map((x, i) => (i === editingIndex.value ? v : x))
    : [...form.variables, v]
  await persistVariables(next)
}

function editVar(i: number) {
  const v = form.variables[i]
  editingIndex.value = i
  Object.assign(newVar, {
    key: v.key, label: v.label, type: v.type, source: v.source,
    auto: v.auto || '', default: v.default ?? '', options: v.options || [],
    required: !!v.required, help: v.help || '', picker: v.picker || 'none',
  })
  varEditor.value?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
}

async function removeVar(i: number) {
  const next = form.variables.filter((_, x) => x !== i)
  // 删除正在编辑的行（或该行之前的行）后修正编辑指向
  if (editingIndex.value === i) resetNewVar()
  else if (editingIndex.value > i) editingIndex.value--
  await persistVariables(next)
}

const sourceItems = [
  { label: '用户填写', value: 'user' },
  { label: '集群自动', value: 'cluster' },
  { label: '节点自动', value: 'node' },
]
// 自动填充键（与 backend/app/services/recipe_render.py 的 cluster_auto_vars/node_auto_vars 一一对应）
const clusterAutoItems = [
  { label: 'master_addr — head 节点 RoCE IP（无 RoCE 回落管理网 IP），多节点协调地址', value: 'master_addr' },
  { label: 'master_port — 集群 master 端口（创建集群时配置，默认 25000）', value: 'master_port' },
  { label: 'nodes_total — 任务节点总数（head + worker）', value: 'nodes_total' },
  { label: 'network_type — 集群网络类型（roce / ib / ethernet）', value: 'network_type' },
  { label: 'head_ip — head 节点管理网 IP', value: 'head_ip' },
  { label: 'head_hostname — head 节点主机名', value: 'head_hostname' },
]
const nodeAutoItems = [
  { label: 'node_rank — 节点 rank（head=0，worker 依次）', value: 'node_rank' },
  { label: 'role — 节点角色（head / worker）', value: 'role' },
  { label: 'hostname — 节点主机名', value: 'hostname' },
  { label: 'node_ip — 节点管理网 IP', value: 'node_ip' },
  { label: 'node_roce_ip — 首选 RoCE 口 IP（无 RoCE 回落管理网 IP）', value: 'node_roce_ip' },
  { label: 'hca — 可用 RoCE HCA 列表（逗号分隔，NCCL 多 rail）', value: 'hca' },
  { label: 'netdev — 首选 RoCE 口网卡名（回落物理网卡）', value: 'netdev' },
  { label: 'gid_index — 首选 RoCE 口 GID 索引', value: 'gid_index' },
  { label: 'agent_port — 节点 agent 服务端口（9000）', value: 'agent_port' },
  { label: 'headless — head 为空、worker 为 "1"（worker 不跑 API server）', value: 'headless' },
]
// 自动填充键下拉：只显示当前 source 对应的键（避免选错来源）
// Nuxt UI v3 group 结构 = 数组的数组，组头为 { type: 'label' } 项
const autoItems = computed(() => [
  [
    { type: 'label', label: newVar.source === 'cluster' ? '集群自动填充' : '节点自动填充' },
    ...(newVar.source === 'cluster' ? clusterAutoItems : nodeAutoItems),
  ],
])
// 切换来源时清空不属于新来源的自动键
watch(() => newVar.source, (s) => {
  const valid = s === 'cluster' ? clusterAutoItems : nodeAutoItems
  if (newVar.auto && !valid.some((i) => i.value === newVar.auto)) newVar.auto = ''
})
const typeItems = [
  { label: '字符串', value: 'string' },
  { label: '整数', value: 'int' },
  { label: '浮点', value: 'float' },
  { label: '布尔', value: 'bool' },
  { label: '选择', value: 'select' },
]
const pickerItems = [
  { label: '无', value: 'none' },
  { label: '模型（已下载）', value: 'model' },
  { label: '镜像（已拉取）', value: 'image' },
]

// 默认值快速选择（快速选择为模型/镜像时可用）
const defaultPickerOpen = ref(false)
const defaultPickerItems = ref<any[]>([])
const defaultPickerLoading = ref(false)

const fmt = (v: number) =>
  v >= 1024 ** 3 ? `${(v / 1024 ** 3).toFixed(1)} GB` : v >= 1024 ** 2 ? `${(v / 1024 ** 2).toFixed(0)} MB` : `${(v / 1024).toFixed(0)} KB`

async function openDefaultPicker() {
  defaultPickerItems.value = []
  defaultPickerOpen.value = true
  defaultPickerLoading.value = true
  try {
    if (newVar.picker === 'model') {
      const r = await api.get('/models/local')
      defaultPickerItems.value = (r.models || []).map((m: any) => ({ name: m.repo, size: m.size_bytes }))
    } else if (newVar.picker === 'image') {
      const r = await api.get('/images/local')
      defaultPickerItems.value = (r.archives || []).map((a: any) => ({ name: a.image, size: a.size_bytes }))
    }
  } catch (e) {
    error.value = String(e)
  } finally {
    defaultPickerLoading.value = false
  }
}

function pickDefault(item: any) {
  newVar.default = item.name
  defaultPickerOpen.value = false
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    const body = { ...form }
    let r
    if (props.recipe?.id) {
      r = await api.patch(`/recipes/${props.recipe.id}`, body)
    } else {
      r = await api.post('/recipes', body)
    }
    // 保存成功后刷新基线：不再提示未保存（编辑模式仅基本信息；新建含全部字段）
    baseline.value = props.recipe?.id
      ? {
          name: form.name, description: form.description,
          image: form.image, compose_template: form.compose_template,
        }
      : JSON.parse(JSON.stringify({
          name: form.name, description: form.description,
          image: form.image, compose_template: form.compose_template,
          variables: form.variables,
        }))
    toast.add({ title: '配方已保存', color: 'success', timeout: 2000 })
    emit('saved', r)
  } catch (e) {
    error.value = String(e)
  } finally {
    saving.value = false
  }
}

// 保存按钮/未保存状态由父页面标题栏持有（与列表页「新建配方」按钮同款布局）
// savingVars 暴露：变量保存中禁用「保存配方」，避免全量 PATCH 的旧 variables 覆盖新变量（竞态）
const canSave = computed(() => !!form.name)
defineExpose({ save, dirty, saving, savingVars, canSave })
</script>

<template>
  <div class="space-y-4">
    <UAlert v-if="error" :title="error" color="error" />

    <UCard>
      <template #header><div class="font-semibold">基本信息</div></template>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
        <UFormField label="名称" required>
          <UInput v-model="form.name" placeholder="配方名称" />
        </UFormField>
        <UFormField label="默认镜像">
          <UInput v-model="form.image" placeholder="ghcr.io/anemll/dspark-vllm-gx10:0.1.1" />
        </UFormField>
        <UFormField label="描述">
          <UInput v-model="form.description" />
        </UFormField>
      </div>
      <UFormField label="Compose 模板（每节点一份，支持 ${VAR} 占位符，由生成的 .env 插值）" class="mt-4">
        <UTextarea v-model="form.compose_template" :rows="24" class="font-mono text-xs w-full" placeholder="services:\n  app:\n    image: ${IMAGE}\n    ..." />
      </UFormField>
    </UCard>

    <UCard>
      <template #header>
        <div class="flex items-center justify-between">
          <div class="font-semibold">变量定义（{{ form.variables.length }}）</div>
        </div>
      </template>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
              <th class="py-2 pr-3 font-medium">Key</th>
              <th class="py-2 pr-3 font-medium">标签</th>
              <th class="py-2 pr-3 font-medium">类型</th>
              <th class="py-2 pr-3 font-medium">来源</th>
              <th class="py-2 pr-3 font-medium">自动填充</th>
              <th class="py-2 pr-3 font-medium">默认值</th>
              <th class="py-2 pr-3 font-medium">快速选择</th>
              <th class="py-2 pr-3 font-medium">必填</th>
              <th class="py-2 font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(v, i) in form.variables" :key="i" class="border-b border-gray-100 dark:border-gray-800/60">
              <td class="py-2 pr-3 font-mono text-xs">{{ v.key }}</td>
              <td class="py-2 pr-3 text-xs">{{ v.label }}</td>
              <td class="py-2 pr-3 text-xs">{{ v.type }}</td>
              <td class="py-2 pr-3 text-xs">{{ v.source }}</td>
              <td class="py-2 pr-3 font-mono text-xs text-gray-500">{{ v.auto || '—' }}</td>
              <td class="py-2 pr-3 font-mono text-xs text-gray-500">{{ v.default ?? '—' }}</td>
              <td class="py-2 pr-3 text-xs">{{ v.picker === 'model' ? '模型' : v.picker === 'image' ? '镜像' : '—' }}</td>
              <td class="py-2 pr-3">{{ v.required ? '✓' : '' }}</td>
              <td class="py-2 text-right whitespace-nowrap">
                <UButton size="xs" variant="ghost" :disabled="savingVars" @click="editVar(i)">编辑</UButton>
                <UButton size="xs" variant="ghost" color="error" :disabled="savingVars" @click="removeVar(i)">删除</UButton>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div ref="varEditor" class="mt-4 p-3 bg-gray-50 dark:bg-gray-900 rounded-md">
        <div class="text-xs text-gray-500 mb-2 flex items-center gap-2">
          <span>{{ editingIndex >= 0 ? `编辑变量「${newVar.key || '…'}」` : '新增变量' }}</span>
          <UButton v-if="editingIndex >= 0" size="xs" variant="ghost" @click="resetNewVar">取消编辑</UButton>
        </div>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
          <UFormField label="Key"><UInput v-model="newVar.key" placeholder="MAX_MODEL_LEN" class="w-full" /></UFormField>
          <UFormField label="标签"><UInput v-model="newVar.label" placeholder="最大上下文长度" class="w-full" /></UFormField>
          <UFormField label="类型">
            <USelect v-model="newVar.type" :items="typeItems" class="w-full" />
          </UFormField>
          <UFormField label="来源">
            <USelect v-model="newVar.source" :items="sourceItems" class="w-full" />
          </UFormField>
          <UFormField label="自动填充键">
            <USelect
              v-model="newVar.auto"
              :items="autoItems"
              placeholder="选择自动填充键"
              class="w-full"
              :disabled="newVar.source === 'user'"
            />
          </UFormField>
          <UFormField label="默认值">
            <div class="flex gap-2">
              <UInput v-model="newVar.default" class="flex-1" />
              <UButton
                v-if="newVar.picker === 'model' || newVar.picker === 'image'"
                size="sm"
                variant="outline"
                @click="openDefaultPicker"
              >
                {{ newVar.picker === 'model' ? '选模型' : '选镜像' }}
              </UButton>
            </div>
          </UFormField>
          <UFormField label="快速选择">
            <USelect v-model="newVar.picker" :items="pickerItems" class="w-full" />
          </UFormField>
          <UFormField label="必填">
            <UCheckbox v-model="newVar.required" />
          </UFormField>
          <div class="flex items-end">
            <UButton size="sm" :disabled="!newVar.key || savingVars" :loading="savingVars" @click="addVar">
              {{ editingIndex >= 0 ? '保存修改' : '添加' }}
            </UButton>
          </div>
        </div>
        <div class="text-[11px] text-gray-400 mt-2 space-y-1.5">
          <div class="text-gray-500">自动填充：变量由平台按集群/节点信息自动取值（发布渲染时生成），用户仍可在发布页覆盖；取值来自 agent 上报的硬件探测结果与集群配置。</div>
          <div><span class="text-gray-500">集群自动（cluster 源，全部节点取相同值）：</span></div>
          <div class="pl-2 grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-0.5">
            <div>· <span class="font-mono">master_addr</span> — head 节点 RoCE IP（无 RoCE 口回落管理网 IP），多节点分布式协调地址</div>
            <div>· <span class="font-mono">master_port</span> — 集群 master 端口（创建集群时配置，默认 25000）</div>
            <div>· <span class="font-mono">nodes_total</span> — 任务节点总数（head + worker）</div>
            <div>· <span class="font-mono">network_type</span> — 集群网络类型（roce / ib / ethernet）</div>
            <div>· <span class="font-mono">head_ip</span> — head 节点管理网 IP</div>
            <div>· <span class="font-mono">head_hostname</span> — head 节点主机名</div>
          </div>
          <div><span class="text-gray-500">节点自动（node 源，按各节点独立取值）：</span></div>
          <div class="pl-2 grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-0.5">
            <div>· <span class="font-mono">node_rank</span> — 节点 rank（head=0，worker 按加入顺序递增）</div>
            <div>· <span class="font-mono">role</span> — 节点角色（head / worker）</div>
            <div>· <span class="font-mono">hostname</span> — 节点主机名</div>
            <div>· <span class="font-mono">node_ip</span> — 节点管理网 IP</div>
            <div>· <span class="font-mono">node_roce_ip</span> — 首选 RoCE 口 IP（无 RoCE 口回落管理网 IP）</div>
            <div>· <span class="font-mono">hca</span> — 可用 RoCE HCA 列表（逗号分隔，NCCL 多 rail）</div>
            <div>· <span class="font-mono">netdev</span> — 首选 RoCE 口网卡名（无 RoCE 回落物理网卡）</div>
            <div>· <span class="font-mono">gid_index</span> — 首选 RoCE 口 GID 索引（RoCEv2 必需）</div>
            <div>· <span class="font-mono">agent_port</span> — 节点 agent 服务端口（9000）</div>
            <div>· <span class="font-mono">headless</span> — head 为空、worker 为 "1"（worker 不跑 API server）</div>
          </div>
          <div class="pt-1">快速选择：模型/镜像变量在发布页提供「选择已下载模型 / 已拉取镜像」按钮，自动填入变量值；此处为快速选择变量设置默认值时也可直接选择。</div>
        </div>
      </div>

      <UModal v-model:open="defaultPickerOpen">
        <template #content>
          <UCard>
            <template #header>
              <div class="font-semibold">{{ newVar.picker === 'model' ? '已下载模型' : '已拉取镜像' }}</div>
            </template>
            <div v-if="defaultPickerLoading" class="py-6 text-center text-sm text-gray-400">加载中…</div>
            <div v-else-if="!defaultPickerItems.length" class="py-6 text-center text-sm text-gray-400">
              暂无{{ newVar.picker === 'model' ? '已下载模型' : '已拉取镜像' }}，请先在「模型」/「镜像」页下载或拉取
            </div>
            <div v-else class="divide-y divide-gray-100 dark:divide-gray-800 -mx-3">
              <button
                v-for="item in defaultPickerItems"
                :key="item.name"
                class="w-full flex items-center justify-between gap-2 px-3 py-2.5 text-left hover:bg-gray-50 dark:hover:bg-gray-800/60"
                @click="pickDefault(item)"
              >
                <span class="font-mono text-sm break-all min-w-0">{{ item.name }}</span>
                <span class="text-xs text-gray-400 shrink-0">{{ fmt(item.size) }}</span>
              </button>
            </div>
          </UCard>
        </template>
      </UModal>
    </UCard>
  </div>
</template>
