<script setup lang="ts">
/** 配方编辑表单（新建/编辑共用） */
const props = defineProps<{ recipe?: any | null }>()
const emit = defineEmits<{ saved: [recipe: any] }>()

const { t } = useI18n()
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
  if (dirty.value && !confirm(t('recipes.unsaved_leave_confirm'))) return false
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
    toast.add({ title: t('recipes.var_added_vars'), color: 'info', timeout: 2000 })
    return
  }
  savingVars.value = true
  try {
    await api.patch(`/recipes/${props.recipe.id}`, { variables: next })
    form.variables = next
    resetNewVar()
    toast.add({ title: t('recipes.vars_saved'), color: 'success', timeout: 2000 })
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
  { label: t('recipes.source_user'), value: 'user' },
  { label: t('recipes.source_cluster'), value: 'cluster' },
  { label: t('recipes.source_node'), value: 'node' },
]
// 自动填充键（与 backend/app/services/recipe_render.py 的 cluster_auto_vars/node_auto_vars 一一对应）
const clusterAutoItems = [
  { label: t('recipes.auto_master_addr'), value: 'master_addr' },
  { label: t('recipes.auto_master_port'), value: 'master_port' },
  { label: t('recipes.auto_nodes_total'), value: 'nodes_total' },
  { label: t('recipes.auto_network_type'), value: 'network_type' },
  { label: t('recipes.auto_head_ip'), value: 'head_ip' },
  { label: t('recipes.auto_head_hostname'), value: 'head_hostname' },
]
const nodeAutoItems = [
  { label: t('recipes.auto_node_rank'), value: 'node_rank' },
  { label: t('recipes.auto_role'), value: 'role' },
  { label: t('recipes.auto_hostname'), value: 'hostname' },
  { label: t('recipes.auto_node_ip'), value: 'node_ip' },
  { label: t('recipes.auto_node_roce_ip'), value: 'node_roce_ip' },
  { label: t('recipes.auto_hca'), value: 'hca' },
  { label: t('recipes.auto_netdev'), value: 'netdev' },
  { label: t('recipes.auto_gid_index'), value: 'gid_index' },
  { label: t('recipes.auto_agent_port'), value: 'agent_port' },
  { label: t('recipes.auto_headless'), value: 'headless' },
]
// 自动填充键下拉：只显示当前 source 对应的键（避免选错来源）
// Nuxt UI v4 group 结构 = 数组的数组，组头为 { type: 'label' } 项
const autoItems = computed(() => [
  [
    { type: 'label', label: newVar.source === 'cluster' ? t('recipes.auto_cluster_group') : t('recipes.auto_node_group') },
    ...(newVar.source === 'cluster' ? clusterAutoItems : nodeAutoItems),
  ],
])
// 切换来源时清空不属于新来源的自动键
watch(() => newVar.source, (s) => {
  const valid = s === 'cluster' ? clusterAutoItems : nodeAutoItems
  if (newVar.auto && !valid.some((i) => i.value === newVar.auto)) newVar.auto = ''
})
const typeItems = [
  { label: t('recipes.type_string'), value: 'string' },
  { label: t('recipes.type_int'), value: 'int' },
  { label: t('recipes.type_float'), value: 'float' },
  { label: t('recipes.type_bool'), value: 'bool' },
  { label: t('recipes.type_select'), value: 'select' },
]
const pickerItems = [
  { label: t('common.none'), value: 'none' },
  { label: t('recipes.picker_model'), value: 'model' },
  { label: t('recipes.picker_image'), value: 'image' },
]

// 默认值快速选择（快速选择为模型/镜像时可用）
const defaultPickerOpen = ref(false)
const defaultPickerItems = ref<any[]>([])
const defaultPickerLoading = ref(false)

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
    toast.add({ title: t('recipes.saved'), color: 'success', timeout: 2000 })
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
      <template #header><div class="font-semibold">{{ $t('recipes.basic_info') }}</div></template>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
        <UFormField :label="$t('common.name')" required>
          <UInput v-model="form.name" :placeholder="$t('recipes.name_placeholder')" />
        </UFormField>
        <UFormField :label="$t('recipes.default_image')">
          <UInput v-model="form.image" placeholder="ghcr.io/anemll/dspark-vllm-gx10:0.1.1" />
        </UFormField>
        <UFormField :label="$t('common.description')">
          <UInput v-model="form.description" />
        </UFormField>
      </div>
      <UFormField :label="$t('recipes.compose_label', { varPh: '${VAR}' })" class="mt-4">
        <UTextarea v-model="form.compose_template" :rows="24" class="font-mono text-xs w-full" placeholder="services:\n  app:\n    image: ${IMAGE}\n    ..." />
      </UFormField>
    </UCard>

    <UCard>
      <template #header>
        <div class="flex items-center justify-between">
          <div class="font-semibold">{{ $t('recipes.variables_title', { count: form.variables.length }) }}</div>
        </div>
      </template>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
              <th class="py-2 pr-3 font-medium">Key</th>
              <th class="py-2 pr-3 font-medium">{{ $t('recipes.col_label') }}</th>
              <th class="py-2 pr-3 font-medium">{{ $t('recipes.col_type') }}</th>
              <th class="py-2 pr-3 font-medium">{{ $t('recipes.col_source') }}</th>
              <th class="py-2 pr-3 font-medium">{{ $t('recipes.col_auto') }}</th>
              <th class="py-2 pr-3 font-medium">{{ $t('recipes.col_default') }}</th>
              <th class="py-2 pr-3 font-medium">{{ $t('recipes.col_picker') }}</th>
              <th class="py-2 pr-3 font-medium">{{ $t('recipes.col_required') }}</th>
              <th class="py-2 font-medium text-right">{{ $t('common.actions') }}</th>
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
              <td class="py-2 pr-3 text-xs">{{ v.picker === 'model' ? $t('recipes.picker_model_short') : v.picker === 'image' ? $t('recipes.picker_image_short') : '—' }}</td>
              <td class="py-2 pr-3">{{ v.required ? '✓' : '' }}</td>
              <td class="py-2 text-right whitespace-nowrap">
                <UButton size="xs" variant="ghost" :disabled="savingVars" @click="editVar(i)">{{ $t('common.edit') }}</UButton>
                <UButton size="xs" variant="ghost" color="error" :disabled="savingVars" @click="removeVar(i)">{{ $t('common.delete') }}</UButton>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div ref="varEditor" class="mt-4 p-3 bg-gray-50 dark:bg-gray-900 rounded-md">
        <div class="text-xs text-gray-500 mb-2 flex items-center gap-2">
          <span>{{ editingIndex >= 0 ? $t('recipes.editing_var', { key: newVar.key || '…' }) : $t('recipes.new_var') }}</span>
          <UButton v-if="editingIndex >= 0" size="xs" variant="ghost" @click="resetNewVar">{{ $t('recipes.cancel_edit') }}</UButton>
        </div>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
          <UFormField label="Key"><UInput v-model="newVar.key" placeholder="MAX_MODEL_LEN" class="w-full" /></UFormField>
          <UFormField :label="$t('recipes.col_label')"><UInput v-model="newVar.label" :placeholder="$t('recipes.label_placeholder')" class="w-full" /></UFormField>
          <UFormField :label="$t('recipes.col_type')">
            <USelectMenu v-model="newVar.type" :items="typeItems" class="w-full" />
          </UFormField>
          <UFormField :label="$t('recipes.col_source')">
            <USelectMenu v-model="newVar.source" :items="sourceItems" class="w-full" />
          </UFormField>
          <UFormField :label="$t('recipes.auto_key')">
            <USelectMenu
              v-model="newVar.auto"
              :items="autoItems"
              :placeholder="$t('recipes.auto_key_placeholder')"
              class="w-full"
              :disabled="newVar.source === 'user'"
            />
          </UFormField>
          <UFormField :label="$t('recipes.col_default')">
            <div class="flex gap-2">
              <UInput v-model="newVar.default" class="flex-1" />
              <UButton
                v-if="newVar.picker === 'model' || newVar.picker === 'image'"
                size="sm"
                variant="outline"
                @click="openDefaultPicker"
              >
                {{ newVar.picker === 'model' ? $t('recipes.pick_model') : $t('recipes.pick_image') }}
              </UButton>
            </div>
          </UFormField>
          <UFormField :label="$t('recipes.col_picker')">
            <USelectMenu v-model="newVar.picker" :items="pickerItems" class="w-full" />
          </UFormField>
          <UFormField :label="$t('recipes.col_required')">
            <UCheckbox v-model="newVar.required" />
          </UFormField>
          <div class="flex items-end">
            <UButton size="sm" :disabled="!newVar.key || savingVars" :loading="savingVars" @click="addVar">
              {{ editingIndex >= 0 ? $t('recipes.save_changes') : $t('recipes.add') }}
            </UButton>
          </div>
        </div>
        <div class="text-[11px] text-gray-400 mt-2 space-y-1.5">
          <div class="text-gray-500">{{ $t('recipes.auto_hint_intro') }}</div>
          <div><span class="text-gray-500">{{ $t('recipes.auto_cluster_hint') }}</span></div>
          <div class="pl-2 grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-0.5">
            <div>{{ $t('recipes.auto_b_master_addr') }}</div>
            <div>{{ $t('recipes.auto_b_master_port') }}</div>
            <div>{{ $t('recipes.auto_b_nodes_total') }}</div>
            <div>{{ $t('recipes.auto_b_network_type') }}</div>
            <div>{{ $t('recipes.auto_b_head_ip') }}</div>
            <div>{{ $t('recipes.auto_b_head_hostname') }}</div>
          </div>
          <div><span class="text-gray-500">{{ $t('recipes.auto_node_hint') }}</span></div>
          <div class="pl-2 grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-0.5">
            <div>{{ $t('recipes.auto_b_node_rank') }}</div>
            <div>{{ $t('recipes.auto_b_role') }}</div>
            <div>{{ $t('recipes.auto_b_hostname') }}</div>
            <div>{{ $t('recipes.auto_b_node_ip') }}</div>
            <div>{{ $t('recipes.auto_b_node_roce_ip') }}</div>
            <div>{{ $t('recipes.auto_b_hca') }}</div>
            <div>{{ $t('recipes.auto_b_netdev') }}</div>
            <div>{{ $t('recipes.auto_b_gid_index') }}</div>
            <div>{{ $t('recipes.auto_b_agent_port') }}</div>
            <div>{{ $t('recipes.auto_b_headless') }}</div>
          </div>
          <div class="pt-1">{{ $t('recipes.picker_hint') }}</div>
        </div>
      </div>

      <UModal v-model:open="defaultPickerOpen">
        <template #content>
          <UCard>
            <template #header>
              <div class="font-semibold">{{ newVar.picker === 'model' ? $t('recipes.picker_models_title') : $t('recipes.picker_images_title') }}</div>
            </template>
            <div v-if="defaultPickerLoading" class="py-6 text-center text-sm text-gray-400">{{ $t('common.loading') }}</div>
            <div v-else-if="!defaultPickerItems.length" class="py-6 text-center text-sm text-gray-400">
              {{ $t('recipes.picker_empty', { picker: newVar.picker === 'model' ? $t('recipes.picker_models_title') : $t('recipes.picker_images_title') }) }}
            </div>
            <div v-else class="divide-y divide-gray-100 dark:divide-gray-800 -mx-3">
              <button
                v-for="item in defaultPickerItems"
                :key="item.name"
                class="w-full flex items-center justify-between gap-2 px-3 py-2.5 text-left hover:bg-gray-50 dark:hover:bg-gray-800/60"
                @click="pickDefault(item)"
              >
                <span class="font-mono text-sm break-all min-w-0">{{ item.name }}</span>
                <span class="text-xs text-gray-400 shrink-0">{{ fmtBytes(item.size) }}</span>
              </button>
            </div>
          </UCard>
        </template>
      </UModal>
    </UCard>
  </div>
</template>
