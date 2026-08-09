<script setup lang="ts">
const { t } = useI18n()
const api = useApi()
const confirm = useConfirmDialog()
const toast = useToast()
const router = useRouter()

// 当前页签：本地配方 / 配方商店
const tab = ref<'local' | 'store'>('local')
// 配方内容双语（仅配方源/商店侧）：en 优先取 *_en 字段，缺省回退主语言（zh）。
// 本地配方为「导入/导出时按语言本地化的单语言独立个体」，直接展示原字段。
const { pick, loc, isEn } = useLocalized()

// ---------- 本地配方（tab: local） ----------
const recipes = ref<any[]>([])
const localError = ref('')

const showImport = ref(false)
const importJson = ref('')
const importFileName = ref('')
const importing = ref(false)

async function loadRecipes() {
  try {
    recipes.value = await api.get('/recipes')
    localError.value = ''
  } catch (e) {
    localError.value = String(e)
  }
}

async function duplicate(r: any) {
  await api.post(`/recipes/${r.id}/duplicate`)
  toast.add({ title: t('recipes.duplicated', { name: r.name }), color: 'success' })
  await loadRecipes()
}

async function removeRecipe(r: any) {
  const ok = await confirm.open({ title: t('recipes.delete_title'), description: t('recipes.delete_confirm', { name: r.name }) })
  if (!ok) return
  await api.del(`/recipes/${r.id}`)
  await loadRecipes()
}

// 导出为「配方源格式」文件（便于分享 / 提交配方源 PR）：
// 名称还原为不带版本后缀的 base + version 字段；字段为本地语言（导出时按当前界面语言而已）。
function buildShareFile(r: any, data: any) {
  const name = String(r?.name ?? data?.name ?? '')
  const m = name.match(/^(.*) \((v?\d+(?:\.\d+)*)\)$/)
  const base = m ? m[1] : name
  const version = m ? m[2] : undefined
  const file: any = { name: base }
  if (version) file.version = version
  if (data?.description) file.description = data.description
  file.image = data?.image
  file.compose_template = data?.compose_template
  file.variables = data?.variables ?? []
  if (data?.nodes) file.nodes = data.nodes
  if (data?.tensor_parallel) file.tensor_parallel = data.tensor_parallel
  return file
}

async function exportRecipe(r: any) {
  const data = await api.get(`/recipes/${r.id}/export`)
  const file = buildShareFile(r, data)
  const blob = new Blob([JSON.stringify(file, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${(file.name || r.name).replace(/[\\/:*?"<>|]/g, '_')}.recipe.json`
  a.click()
  URL.revokeObjectURL(url)
  toast.add({ title: t('recipes.exported', { name: r.name }), color: 'success' })
}

// 语言兜底本地化：导入/粘贴的配方源文件优先使用当前界面语言，缺省回退配方中其他语言
function localizeRecipeFile(parsed: any) {
  const name = pick(parsed?.name, parsed?.name_en)
  const description = pick(parsed?.description, parsed?.description_en)
  const variables = (Array.isArray(parsed?.variables) ? parsed.variables : []).map((v: any) => {
    const nv = { ...v }
    for (const k of ['label', 'help']) {
      if (v[`${k}_en`] != null) {
        nv[k] = isEn.value ? (v[`${k}_en`] || v[k] || '') : (v[k] || v[`${k}_en`] || '')
        delete nv[`${k}_en`]
      }
    }
    return nv
  })
  return {
    name: name || parsed?.name || 'Imported recipe',
    description,
    image: parsed?.image,
    compose_template: parsed?.compose_template,
    variables,
    nodes: parsed?.nodes,
  }
}

function onFilePicked(e: Event) {
  const input = e.target as HTMLInputElement
  const f = input.files?.[0]
  importFileName.value = f?.name || ''
  if (f) {
    f.text().then(async (text) => {
      try {
        importJson.value = text // 预填到粘贴区，便于直接编辑/查看
        localError.value = ''
      } catch {
        localError.value = String(t('recipes.import_file_invalid'))
      }
    })
  }
}

async function doImport() {
  importing.value = true
  localError.value = ''
  try {
    // 支持文件（onFilePicked 已预填到 importJson 可查看/编辑）或直接粘贴
    const parsed = JSON.parse(importJson.value)
    const payload = localizeRecipeFile(parsed)
    const r = await api.post('/recipes/import', payload)
    showImport.value = false
    importJson.value = ''
    importFileName.value = ''
    toast.add({
      title: r.import_notice ? t('recipes.import_success_pre', { notice: r.import_notice }) : t('recipes.import_success'),
      color: 'success',
    })
    await loadRecipes()
  } catch (e) {
    localError.value = String(e)
  } finally {
    importing.value = false
  }
}

// ---------- 配方商店（tab: store） ----------
const sources = ref<any[]>([])
const activeSourceId = ref<number | null>(null)
const catalog = ref<any>(null)
const storeError = ref('')
const syncing = ref(false)
const catalogLoading = ref(false)

const showAddSource = ref(false)
const newSource = reactive({ name: '', url: 'https://github.com/skymaze/FireworksRecipes.git', branch: 'main' })
const addingSource = ref(false)

const search = ref('')
const filterProvider = ref('')
const filterDtype = ref('')

const detailOpen = ref(false)
const detailItem = ref<any>(null)
const detailReadme = ref('')
const readmeLoading = ref(false)
const importingRecipe = ref(false)

const activeSource = computed(() => sources.value.find((s) => s.id === activeSourceId.value) || null)

const filteredItems = computed(() => {
  const items = catalog.value?.items || []
  const q = search.value.trim().toLowerCase()
  return items.filter((it: any) => {
    if (filterProvider.value && it.provider !== filterProvider.value) return false
    if (filterDtype.value && it.dtype !== filterDtype.value) return false
    if (!q) return true
    return [it.id, it.provider, it.model, it.params, it.dtype, it.topology, it.description]
      .filter(Boolean).some((v) => String(v).toLowerCase().includes(q))
  })
})

const providers = computed(() => Array.from(new Set((catalog.value?.items || []).map((it: any) => it.provider as string).filter(Boolean))).sort() as string[])
const dtypes = computed(() => Array.from(new Set((catalog.value?.items || []).map((it: any) => it.dtype as string).filter(Boolean))).sort() as string[])

async function loadSources() {
  sources.value = await api.get('/recipes/sources')
  if (sources.value.length && (activeSourceId.value == null || !sources.value.some((s) => s.id === activeSourceId.value))) {
    activeSourceId.value = sources.value[0].id
    await loadCatalog()
  }
}

async function addSource() {
  addingSource.value = true
  storeError.value = ''
  try {
    const s = await api.post('/recipes/sources', {
      name: newSource.name || 'FireworksRecipes',
      url: newSource.url,
      branch: newSource.branch || 'main',
    })
    newSource.name = ''
    showAddSource.value = false
    await loadSources()
    activeSourceId.value = s.id
    await loadCatalog()
  } catch (e) {
    storeError.value = String(e)
  } finally {
    addingSource.value = false
  }
}

async function syncSource() {
  if (!activeSourceId.value) return
  syncing.value = true
  storeError.value = ''
  try {
    await api.post(`/recipes/sources/${activeSourceId.value}/sync`)
    toast.add({ title: t('recipeStore.synced'), color: 'success' })
    await loadSources()
    await loadCatalog()
  } catch (e) {
    storeError.value = String(e)
  } finally {
    syncing.value = false
  }
}

async function loadCatalog() {
  if (!activeSourceId.value) return
  catalogLoading.value = true
  storeError.value = ''
  try {
    catalog.value = await api.get(`/recipes/sources/${activeSourceId.value}/catalog`)
  } catch (e) {
    catalog.value = null
    storeError.value = String(e)
  } finally {
    catalogLoading.value = false
  }
}

// 迷你 markdown 渲染（无第三方依赖；先 HTML 转义防注入，再套常用格式）
function escapeHtml(s: string) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}
function renderMd(md: string) {
  const esc = (s: string) => escapeHtml(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>')
  const lines = md.split('\n')
  const out: string[] = []
  let inCode = false
  let codeBuf: string[] = []
  for (const raw of lines) {
    if (/^```/.test(raw.trim())) {
      if (inCode) { out.push(`<pre class="fw-md-code">${codeBuf.map(esc).join('\n')}</pre>`); codeBuf = [] }
      inCode = !inCode
      continue
    }
    if (inCode) { codeBuf.push(raw); continue }
    const line = raw
    const h = line.match(/^(#{1,4})\s+(.*)$/)
    if (h) { out.push(`<h${h[1].length} class="fw-md-h${h[1].length}">${esc(h[2])}</h${h[1].length}>`); continue }
    if (/^\s*[-*]\s+/.test(line)) { out.push(`<li class="fw-md-li">${esc(line.replace(/^\s*[-*]\s+/, ''))}</li>`); continue }
    const bq = line.match(/^\s*>\s?(.*)$/)
    if (bq) { out.push(`<div class="fw-md-blockquote">${esc(bq[1])}</div>`); continue }
    if (/^\|.*\|$/.test(line)) { out.push(`<div class="fw-md-table">${esc(line)}</div>`); continue }
    if (!line.trim()) { out.push(''); continue }
    out.push(`<p class="fw-md-p">${esc(line)}</p>`)
  }
  if (inCode) out.push(`<pre class="fw-md-code">${codeBuf.map(esc).join('\n')}</pre>`)
  return out.join('\n')
}

async function openDetail(item: any) {
  detailItem.value = item
  detailReadme.value = ''
  detailOpen.value = true
  const readmePath = pick(item.readme, item.readme_en) // 按 locale 选英文/默认 README，缺省回退
  if (!readmePath) return
  readmeLoading.value = true
  try {
    const r = await api.get(`/recipes/sources/${activeSourceId.value}/readme`, { path: readmePath })
    detailReadme.value = r.content || ''
  } catch {
    detailReadme.value = `<!-- ${t('recipeStore.no_readme')} -->`
  } finally {
    readmeLoading.value = false
  }
}

// 从配方源导入到本地（每次必新建独立配方；本地只存当前语言快照）。run: 导入后进发布向导
async function importItem(item: any, run: boolean) {
  if (!activeSourceId.value) return
  importingRecipe.value = true
  storeError.value = ''
  try {
    const r = await api.post('/recipes/install', {
      source_id: activeSourceId.value,
      path: item.path,
      lang: isEn.value ? 'en' : 'zh', // 按当前界面语言本地化导入
    })
    detailOpen.value = false
    await Promise.all([loadRecipes(), loadCatalog()])
    if (run) {
      toast.add({ title: t('recipeStore.imported_run', { name: r.name }), color: 'success' })
      router.push(`/tasks/publish?recipe=${r.id}`)
      return
    }
    toast.add({ title: t('recipeStore.imported', { name: r.name }), color: 'success' })
  } catch (e) {
    storeError.value = String(e)
  } finally {
    importingRecipe.value = false
  }
}

function fmtCtx(v: number | null | undefined) {
  if (!v) return '—'
  if (v >= 1000000) {
    const m = v / 1000000
    return `${Number.isInteger(m) ? m : Math.round(m * 10) / 10}M ctx`
  }
  if (v >= 1024) return `${Math.round(v / 1024)}K ctx`
  return `${v} ctx`
}

onMounted(() => {
  loadRecipes()
  loadSources()
})
</script>

<template>
  <div>
    <div class="flex items-center justify-between mb-4">
      <h1 class="text-xl font-bold">{{ $t('recipes.title') }}</h1>
      <div class="flex gap-2">
        <button
          class="px-3 py-1.5 rounded-md text-sm font-medium"
          :class="tab === 'local' ? 'bg-primary text-white' : 'bg-gray-100 dark:bg-gray-800/60 text-gray-600 dark:text-gray-300'"
          @click="tab = 'local'"
        >{{ $t('recipeStore.tab_local') }}</button>
        <button
          class="px-3 py-1.5 rounded-md text-sm font-medium"
          :class="tab === 'store' ? 'bg-primary text-white' : 'bg-gray-100 dark:bg-gray-800/60 text-gray-600 dark:text-gray-300'"
          @click="tab = 'store'"
        >{{ $t('recipeStore.tab_store') }}</button>
      </div>
    </div>

    <!-- ================= 本地配方（卡片） ================= -->
    <div v-if="tab === 'local'">
      <UAlert v-if="localError" :title="localError" color="error" class="mb-4" />
      <div class="mb-4 flex justify-end gap-2">
        <UButton variant="outline" @click="showImport = true">{{ $t('recipes.import') }}</UButton>
        <UButton color="primary" to="/recipes/new">{{ $t('recipes.create') }}</UButton>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        <UCard v-for="r in recipes" :key="r.id" class="flex flex-col">
          <div class="flex-1 min-w-0">
            <div class="flex items-start justify-between gap-2">
              <NuxtLink :to="`/recipes/${r.id}`" class="font-semibold hover:underline leading-snug min-w-0">{{ r.name }}</NuxtLink>
              <UBadge v-if="r.is_seed" size="xs" variant="subtle" class="shrink-0">{{ $t('recipes.seed') }}</UBadge>
            </div>
            <div class="mt-1 font-mono text-xs text-gray-500 truncate">{{ r.image || '—' }}</div>
            <div class="flex flex-wrap gap-1 mt-2 text-[11px]">
              <UBadge v-if="r.node_count" size="xs" variant="outline" color="primary">
                {{ r.node_count }} nodes · TP{{ r.tensor_parallel }}
              </UBadge>
              <UBadge size="xs" variant="subtle" color="neutral">{{ r.variables?.length || 0 }} {{ r.variables?.length === 1 ? 'var' : 'vars' }}</UBadge>
            </div>
            <p v-if="r.description" class="mt-2 text-xs text-gray-500 line-clamp-2">{{ r.description }}</p>
          </div>
          <div class="flex gap-2 mt-3 pt-3 border-t border-gray-100 dark:border-gray-800">
            <UButton size="sm" variant="ghost" :to="`/recipes/${r.id}`">{{ $t('common.edit') }}</UButton>
            <UButton size="sm" variant="ghost" @click="duplicate(r)">{{ $t('recipes.duplicate') }}</UButton>
            <UButton size="sm" variant="ghost" @click="exportRecipe(r)">{{ $t('recipes.export') }}</UButton>
            <UButton size="sm" variant="ghost" color="error" class="ml-auto" @click="removeRecipe(r)">{{ $t('common.delete') }}</UButton>
          </div>
        </UCard>
        <div v-if="!recipes.length" class="col-span-full py-12 text-center text-sm text-gray-400">
          {{ $t('recipes.empty') }}
        </div>
      </div>

      <!-- 导入（配方源格式文件 / 粘贴） -->
      <UModal v-model:open="showImport">
        <template #content>
          <UCard>
            <template #header><div class="font-semibold">{{ $t('recipes.import_title') }}</div></template>
            <div class="space-y-3">
              <UFormField :label="$t('recipes.import_file_label')" :hint="$t('recipes.import_file_hint')">
                <input
                  type="file"
                  accept=".json,.recipe.json,application/json"
                  class="block w-full text-sm text-gray-600 dark:text-gray-300 file:mr-3 file:rounded-md file:border-0 file:bg-gray-100 dark:file:bg-gray-800 file:px-3 file:py-1.5 file:text-sm file:font-medium hover:file:bg-gray-200 dark:hover:file:bg-gray-700"
                  @change="onFilePicked"
                />
                <p v-if="importFileName" class="mt-1 text-xs font-mono text-gray-500">{{ importFileName }}</p>
              </UFormField>
              <div class="text-center text-xs text-gray-400">— {{ $t('recipes.import_or') }} —</div>
              <UTextarea v-model="importJson" :rows="8" class="font-mono text-xs w-full" placeholder='{"name": "...", "compose_template": "...", "variables": [...]}' />
            </div>
            <template #footer>
              <div class="flex justify-end gap-2">
                <UButton variant="outline" @click="showImport = false">{{ $t('common.cancel') }}</UButton>
                <UButton color="primary" :loading="importing" :disabled="!importJson.trim()" @click="doImport">{{ $t('recipes.import_btn') }}</UButton>
              </div>
            </template>
          </UCard>
        </template>
      </UModal>
    </div>

    <!-- ================= 配方商店 ================= -->
    <div v-else>
      <UAlert v-if="storeError" :title="storeError" color="error" class="mb-4" />

      <!-- 源管理 -->
      <UCard class="mb-4">
        <template #header>
          <div class="flex items-center justify-between">
            <div class="font-semibold">{{ $t('recipeStore.source_title') }}</div>
            <UButton size="xs" variant="outline" @click="showAddSource = true">{{ $t('recipeStore.add_source') }}</UButton>
          </div>
        </template>
        <div v-if="sources.length" class="flex flex-wrap items-center gap-3">
          <USelectMenu
            :model-value="activeSourceId"
            @update:model-value="(v: any) => { activeSourceId = v; loadCatalog() }"
            value-key="value"
            :items="sources.map((s) => ({ label: `${s.name} (${s.branch})`, value: s.id }))"
            class="min-w-[240px]"
          />
          <span v-if="activeSource" class="text-xs text-gray-500 font-mono break-all min-w-0">{{ activeSource.url }}</span>
          <div class="flex items-center gap-2 ml-auto">
            <UBadge :color="activeSource?.status === 'synced' ? 'success' : activeSource?.status === 'failed' ? 'error' : 'warning'" variant="subtle">
              {{ activeSource?.status }}
            </UBadge>
            <span v-if="activeSource?.last_commit" class="text-xs text-gray-400 font-mono">{{ ($t('recipeStore.commit') + ' ' + activeSource.last_commit.slice(0, 7)) }}</span>
            <UButton size="xs" color="primary" variant="soft" :loading="syncing" @click="syncSource">{{ $t('recipeStore.sync') }}</UButton>
          </div>
          <div v-if="activeSource?.error" class="w-full text-xs text-error">{{ activeSource.error }}</div>
        </div>
        <div v-else class="text-sm text-gray-500">
          {{ $t('recipeStore.no_source') }}
        </div>
      </UCard>

      <!-- 目录：筛选 + 卡片 -->
      <template v-if="activeSource">
        <div v-if="catalogLoading" class="py-10 text-center text-sm text-gray-400">{{ $t('common.loading') }}</div>
        <div v-else-if="!catalog" class="py-10 text-center text-sm text-gray-400">{{ $t('recipeStore.catalog_empty') }}</div>
        <template v-else>
          <UCard class="mb-4">
            <div class="flex flex-wrap items-center gap-3">
              <UInput :model-value="search" icon="lucide:search" class="w-64" :placeholder="$t('recipeStore.search')"
                @update:model-value="(v: any) => search = v" />
              <USelectMenu v-if="providers.length > 1" :model-value="filterProvider" value-key="value"
                :items="[{ label: $t('recipeStore.all_provider'), value: '' }, ...providers.map((p) => ({ label: p, value: p }))]"
                class="w-44" @update:model-value="(v: any) => filterProvider = v" />
              <USelectMenu v-if="dtypes.length > 1" :model-value="filterDtype" value-key="value"
                :items="[{ label: $t('recipeStore.all_dtype'), value: '' }, ...dtypes.map((d) => ({ label: d, value: d }))]"
                class="w-32" @update:model-value="(v: any) => filterDtype = v" />
              <span class="ml-auto text-xs text-gray-400">{{ $t('recipeStore.count', { n: filteredItems.length, total: catalog.items.length }) }}</span>
            </div>
          </UCard>

          <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            <UCard v-for="(it, i) in filteredItems" :key="i" class="flex flex-col">
              <div class="flex-1">
                <div class="flex items-start justify-between gap-2">
                  <div class="min-w-0">
                    <div class="font-semibold leading-snug">{{ it.id }}</div>
                    <div class="text-xs text-gray-500">{{ it.provider || '—' }}{{ it.model ? ` · ${it.model}` : '' }}</div>
                  </div>
                  <UBadge v-if="it.version" color="primary" variant="soft" size="xs">v{{ it.version }}</UBadge>
                </div>
                <div class="flex flex-wrap gap-1 mt-2 text-[11px]">
                  <UBadge v-if="it.dtype" size="xs" variant="subtle" color="neutral">{{ it.dtype }}</UBadge>
                  <UBadge size="xs" variant="subtle" color="neutral">{{ fmtCtx(it.context_length) }}</UBadge>
                  <UBadge v-if="it.nodes" size="xs" variant="outline" color="primary">
                    {{ it.nodes }} nodes · TP{{ it.tensor_parallel }}
                  </UBadge>
                  <UBadge v-else-if="it.topology" size="xs" variant="subtle" color="neutral">{{ it.topology }}</UBadge>
                  <UBadge v-if="it.modality" size="xs" variant="subtle" color="neutral">{{ it.modality }}</UBadge>
                  <UBadge v-if="it.params" size="xs" variant="subtle" color="neutral">{{ it.params }}</UBadge>
                </div>
                <p v-if="loc(it, 'description')" class="mt-2 text-xs text-gray-500 line-clamp-2">{{ loc(it, 'description') }}</p>
              </div>
              <div class="flex gap-2 mt-3 pt-3 border-t border-gray-100 dark:border-gray-800">
                <UButton size="sm" color="primary" :loading="importingRecipe" @click="importItem(it, true)">{{ $t('recipeStore.import_run') }}</UButton>
                <UButton size="sm" variant="outline" @click="importItem(it, false)">{{ $t('recipeStore.import_only') }}</UButton>
                <UButton size="sm" variant="ghost" :disabled="!it.readme" @click="openDetail(it)">{{ $t('recipeStore.docs') }}</UButton>
              </div>
            </UCard>
            <div v-if="!filteredItems.length" class="col-span-full py-10 text-center text-sm text-gray-400">
              {{ $t('recipeStore.no_match') }}
            </div>
          </div>
        </template>
      </template>
    </div>

    <!-- 添加配方源 -->
    <UModal v-model:open="showAddSource">
      <template #content>
        <UCard>
          <template #header><div class="font-semibold">{{ $t('recipeStore.add_source_title') }}</div></template>
          <div class="space-y-3">
            <UFormField :label="$t('recipeStore.col_name')">
              <UInput v-model="newSource.name" :placeholder="$t('recipeStore.source_name_ph')" />
            </UFormField>
            <UFormField :label="$t('recipeStore.col_url')" required>
              <UInput v-model="newSource.url" placeholder="https://github.com/owner/FireworksRecipes.git" />
            </UFormField>
            <UFormField :label="$t('recipeStore.col_branch')">
              <UInput v-model="newSource.branch" placeholder="main" />
            </UFormField>
          </div>
          <template #footer>
            <div class="flex justify-end gap-2">
              <UButton variant="outline" @click="showAddSource = false">{{ $t('common.cancel') }}</UButton>
              <UButton color="primary" :loading="addingSource" :disabled="!newSource.url.trim()" @click="addSource">{{ $t('recipeStore.add_source') }}</UButton>
            </div>
          </template>
        </UCard>
      </template>
    </UModal>

    <!-- 详情 / README -->
    <UModal v-model:open="detailOpen">
      <template #content>
        <UCard v-if="detailItem">
          <template #header>
            <div class="flex items-center justify-between gap-2">
              <div class="font-semibold min-w-0 truncate">{{ detailItem.id }} <UBadge v-if="detailItem.version" color="primary" variant="soft" size="xs" class="ml-1">v{{ detailItem.version }}</UBadge></div>
              <div class="flex gap-2 shrink-0">
                <UButton size="xs" color="primary" :loading="importingRecipe" @click="importItem(detailItem, true)">{{ $t('recipeStore.import_run') }}</UButton>
                <UButton size="xs" variant="outline" @click="importItem(detailItem, false)">{{ $t('recipeStore.import_only') }}</UButton>
              </div>
            </div>
          </template>
          <div style="max-height: 70vh; overflow-y: auto; padding-right: .25rem;">
            <div v-if="readmeLoading" class="py-6 text-center text-sm text-gray-400">{{ $t('common.loading') }}</div>
            <div v-else-if="detailReadme" class="fw-md" v-html="renderMd(detailReadme)"></div>
            <div v-else class="py-6 text-center text-sm text-gray-400">{{ $t('recipeStore.no_readme') }}</div>
          </div>
          <template #footer>
            <div class="text-right">
              <UButton variant="ghost" @click="detailOpen = false">{{ $t('common.cancel') }}</UButton>
            </div>
          </template>
        </UCard>
      </template>
    </UModal>
  </div>
</template>

<style scoped>
.fw-md { font-size: 0.875rem; line-height: 1.5; }
.fw-md :deep(h1) { font-size: 1.125rem; font-weight: 700; margin: .5rem 0; }
.fw-md :deep(h2) { font-size: 1rem; font-weight: 700; margin: .5rem 0; }
.fw-md :deep(h3) { font-size: .875rem; font-weight: 700; margin: .5rem 0; }
.fw-md :deep(h4) { font-size: .875rem; font-weight: 600; margin: .5rem 0; }
.fw-md :deep(p) { margin: .35em 0; }
.fw-md :deep(code) { background: rgba(128, 128, 128, .12); border-radius: 4px; padding: 0 .25em; font-size: .75rem; }
.fw-md :deep(li) { list-style: disc; margin-left: 1.25em; }
.fw-md :deep(.fw-md-blockquote) { border-left: 2px solid rgba(128, 128, 128, .5); padding-left: .5rem; margin: .25rem 0; opacity: .75; }
.fw-md :deep(.fw-md-code) { background: rgba(128, 128, 128, .12); border-radius: 6px; padding: .5rem; overflow-x: auto; font-size: .75rem; margin: .5rem 0; }
.fw-md :deep(.fw-md-table) { font-family: ui-monospace, monospace; font-size: .6875rem; opacity: .7; margin: .25rem 0; }
</style>
