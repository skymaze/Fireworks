<script setup lang="ts">
const { t } = useI18n()
const api = useApi()
const router = useRouter()
const toast = useToast()
// 目录双语：en 优先取 *_en，缺省回退主语言；本地为独立单语个体
const { pick, loc, isEn } = useLocalized()

// ---------- 配方商店（tab: store） ----------
const sources = ref<any[]>([])
const activeSourceId = ref<number | null>(null)
const catalog = ref<any>(null)
const syncing = ref(false)
const catalogLoading = ref(false)
let catalogLoadSeq = 0

const showAddSource = ref(false)
const newSource = reactive({ name: '', url: 'https://github.com/skymaze/FireworksRecipes.git', branch: '' })
const addingSource = ref(false)
const sourceBranches = ref<string[]>([])
const sourceDefaultBranch = ref('')
const probingSource = ref(false)
const sourceProbeError = ref('')
let sourceProbeSeq = 0
let sourceProbeTimer: ReturnType<typeof setTimeout> | null = null

const showEditSource = ref(false)
const editBranch = ref('')
const editBranches = ref<string[]>([])
const editDefaultBranch = ref('')
const probingEditSource = ref(false)
const savingSource = ref(false)
const deletingSource = ref(false)
const editProbeError = ref('')
const deleteSourceTarget = ref<any>(null)
let editSourceProbeSeq = 0

const search = ref('')
const filterProvider = ref('')

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
    if (!q) return true
    return [it.name, it.id, it.provider, it.model, it.params, it.description]
      .filter(Boolean).some((v) => String(v).toLowerCase().includes(q))
  })
})

const providers = computed(() => Array.from(new Set((catalog.value?.items || []).map((it: any) => it.provider as string).filter(Boolean))).sort() as string[])

function recipeMetadata(it: any) {
  const parts: string[] = []
  const provider = String(it.provider || '').trim()
  const model = String(it.model || '').trim()

  if (model) {
    const normalizedModel = model.toLowerCase()
    const normalizedProvider = provider.toLowerCase()
    if (provider && normalizedModel !== normalizedProvider && !normalizedModel.startsWith(`${normalizedProvider}/`)) {
      parts.push(provider)
    }
    parts.push(model)
  } else if (provider) {
    parts.push(provider)
  }

  return Array.from(new Set(parts)).join(' · ')
}

async function loadSources() {
  sources.value = await api.get('/recipes/sources')
  if (!sources.value.length) {
    activeSourceId.value = null
    catalog.value = null
    return
  }
  if (sources.value.length && (activeSourceId.value == null || !sources.value.some((s) => s.id === activeSourceId.value))) {
    activeSourceId.value = sources.value[0].id
    await loadCatalog()
  }
}

async function discoverSourceBranches() {
  const url = newSource.url.trim()
  const seq = ++sourceProbeSeq
  sourceBranches.value = []
  sourceDefaultBranch.value = ''
  sourceProbeError.value = ''
  newSource.branch = ''
  if (!url) return
  probingSource.value = true
  try {
    const result: any = await api.post('/recipes/sources/discover', { url })
    if (seq !== sourceProbeSeq) return
    sourceBranches.value = result.branches || []
    sourceDefaultBranch.value = result.default_branch || ''
    newSource.branch = sourceDefaultBranch.value || sourceBranches.value[0] || ''
  } catch (e) {
    if (seq !== sourceProbeSeq) return
    sourceProbeError.value = errorMsg(e)
  } finally {
    if (seq === sourceProbeSeq) probingSource.value = false
  }
}

function scheduleSourceDiscovery() {
  if (sourceProbeTimer) clearTimeout(sourceProbeTimer)
  // URL 一变化立即废弃旧请求和旧分支，避免防抖窗口内提交到错误仓库。
  sourceProbeSeq++
  sourceBranches.value = []
  sourceDefaultBranch.value = ''
  sourceProbeError.value = ''
  newSource.branch = ''
  if (!newSource.url.trim()) {
    probingSource.value = false
    return
  }
  probingSource.value = true
  sourceProbeTimer = setTimeout(() => {
    sourceProbeTimer = null
    void discoverSourceBranches()
  }, 500)
}

async function addSource() {
  addingSource.value = true
  try {
    const s = await api.post('/recipes/sources', {
      name: newSource.name || 'FireworksRecipes',
      url: newSource.url,
      branch: newSource.branch,
    })
    newSource.name = ''
    showAddSource.value = false
    await loadSources()
    activeSourceId.value = s.id
    catalog.value = null
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    addingSource.value = false
  }
}

async function openSourceSettings() {
  if (!activeSource.value) return
  const sourceId = activeSource.value.id
  const seq = ++editSourceProbeSeq
  showEditSource.value = true
  probingEditSource.value = true
  editProbeError.value = ''
  editBranches.value = []
  editBranch.value = activeSource.value.branch
  try {
    const result: any = await api.post('/recipes/sources/discover', { url: activeSource.value.url })
    if (seq !== editSourceProbeSeq || !showEditSource.value || activeSource.value?.id !== sourceId) return
    editBranches.value = result.branches || []
    editDefaultBranch.value = result.default_branch || ''
    if (!editBranches.value.includes(editBranch.value)) {
      editBranch.value = editDefaultBranch.value || editBranches.value[0] || ''
    }
  } catch (e) {
    if (seq !== editSourceProbeSeq || !showEditSource.value || activeSource.value?.id !== sourceId) return
    editProbeError.value = errorMsg(e)
  } finally {
    if (seq === editSourceProbeSeq) probingEditSource.value = false
  }
}

async function saveSourceBranch() {
  if (!activeSource.value || !editBranch.value) return
  const sourceId = activeSource.value.id
  const changed = editBranch.value !== activeSource.value.branch
  if (!changed) {
    showEditSource.value = false
    toast.add({ title: t('recipeStore.source_saved'), color: 'success' })
    return
  }
  savingSource.value = true
  try {
    await api.patch(`/recipes/sources/${sourceId}`, { branch: editBranch.value })
    await api.post(`/recipes/sources/${sourceId}/sync`)
    showEditSource.value = false
    catalog.value = null
    await loadSources()
    await loadCatalog()
    toast.add({ title: t('recipeStore.branch_updated'), color: 'success' })
  } catch (e) {
    catalog.value = null
    await loadSources()
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    savingSource.value = false
  }
}

function requestDeleteActiveSource() {
  if (!activeSource.value) return
  deleteSourceTarget.value = activeSource.value
  showEditSource.value = false
}

async function confirmDeleteSource() {
  if (!deleteSourceTarget.value) return
  const source = deleteSourceTarget.value
  deletingSource.value = true
  try {
    await api.del(`/recipes/sources/${source.id}`)
    showEditSource.value = false
    activeSourceId.value = null
    catalog.value = null
    deleteSourceTarget.value = null
    await loadSources()
    toast.add({ title: t('recipeStore.source_deleted', { name: source.name }), color: 'success' })
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    deletingSource.value = false
  }
}

async function syncSource() {
  if (!activeSourceId.value) return
  const recover = activeSource.value?.status === 'syncing'
  syncing.value = true
  try {
    await api.post(`/recipes/sources/${activeSourceId.value}/sync${recover ? '?recover=true' : ''}`)
    toast.add({ title: t('recipeStore.synced'), color: 'success' })
    await loadSources()
    await loadCatalog()
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
  } finally {
    syncing.value = false
  }
}

async function loadCatalog() {
  const sourceId = activeSourceId.value
  const seq = ++catalogLoadSeq
  if (!sourceId) {
    catalog.value = null
    return
  }
  catalogLoading.value = true
  catalog.value = null
  try {
    const result = await api.get(`/recipes/sources/${sourceId}/catalog`)
    if (seq === catalogLoadSeq && activeSourceId.value === sourceId) catalog.value = result
  } catch (e) {
    if (seq === catalogLoadSeq && activeSourceId.value === sourceId) {
      catalog.value = null
      toast.add({ title: errorMsg(e), color: 'error' })
    }
  } finally {
    if (seq === catalogLoadSeq) catalogLoading.value = false
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
  try {
    const r = await api.post('/recipes/install', {
      source_id: activeSourceId.value,
      path: item.path,
      lang: isEn.value ? 'en' : 'zh', // 按当前界面语言本地化导入
    })
    detailOpen.value = false
    await loadCatalog()
    if (run) {
      toast.add({ title: t('recipeStore.imported_run', { name: r.name }), color: 'success' })
      router.push(`/tasks/publish?recipe=${r.id}`)
      return
    }
    toast.add({ title: t('recipeStore.imported', { name: r.name }), color: 'success' })
  } catch (e) {
    toast.add({ title: errorMsg(e), color: 'error' })
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
  loadSources()
})

watch(showAddSource, (open) => {
  if (open) void discoverSourceBranches()
  else if (sourceProbeTimer) {
    clearTimeout(sourceProbeTimer)
    sourceProbeTimer = null
  }
})

watch(() => newSource.url, () => {
  if (showAddSource.value) scheduleSourceDiscovery()
})

watch(showEditSource, (open) => {
  if (!open) {
    editSourceProbeSeq++
    probingEditSource.value = false
  }
})

</script>

<template>
  <UDashboardPanel id="store">
    <template #header>
      <UDashboardNavbar :title="$t('recipeStore.tab_store')" >
        <template #leading>
          <UDashboardSidebarCollapse />
        </template>
      </UDashboardNavbar>
          </template>
    <template #body>
  <!-- ================= 配方商店 ================= -->
      <div>

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
              <UButton size="xs" color="primary" variant="soft" :loading="syncing" @click="syncSource">
                {{ $t(activeSource?.status === 'syncing' ? 'recipeStore.recover_sync' : 'recipeStore.sync') }}
              </UButton>
              <UButton size="xs" variant="outline" icon="lucide:settings" @click="openSourceSettings">{{ $t('recipeStore.manage_source') }}</UButton>
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
                <span class="ml-auto text-xs text-gray-400">{{ $t('recipeStore.count', { n: filteredItems.length, total: catalog.items.length }) }}</span>
              </div>
            </UCard>

            <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
              <UCard v-for="(it, i) in filteredItems" :key="i" class="h-full" :ui="{ root: 'flex flex-col', body: 'flex-1' }">
                <template #header>
                  <div class="font-semibold leading-snug">{{ loc(it, 'name') || it.id }}</div>
                </template>
                <template #default>
                  <div class="flex flex-wrap gap-1 text-[11px]">
                    <UBadge v-if="it.version" color="primary" variant="soft" size="xs">v{{ it.version }}</UBadge>
                    <UBadge size="xs" variant="subtle" color="neutral">{{ fmtCtx(it.context_length) }}</UBadge>
                    <UBadge v-if="it.nodes" size="xs" variant="outline" color="primary">
                      {{ it.nodes }} nodes<template v-if="it.tensor_parallel"> · TP{{ it.tensor_parallel }}</template>
                    </UBadge>
                    <UBadge v-if="it.modality" size="xs" variant="subtle" color="neutral">{{ it.modality }}</UBadge>
                    <UBadge v-if="it.params" size="xs" variant="subtle" color="neutral">{{ it.params }}</UBadge>
                  </div>
                  <div v-if="recipeMetadata(it)" class="mt-2 text-xs text-muted break-words">{{ recipeMetadata(it) }}</div>
                  <p v-if="loc(it, 'description')" class="mt-2 text-xs text-gray-500 line-clamp-2">{{ loc(it, 'description') }}</p>
                </template>
                <template #footer>
                  <div class="flex flex-wrap gap-2">
                    <UButton size="sm" color="primary" :loading="importingRecipe" @click="importItem(it, true)">{{ $t('recipeStore.import_run') }}</UButton>
                    <UButton size="sm" variant="outline" @click="importItem(it, false)">{{ $t('recipeStore.import_only') }}</UButton>
                    <UButton size="sm" variant="ghost" :disabled="!it.readme" @click="openDetail(it)">{{ $t('recipeStore.docs') }}</UButton>
                  </div>
                </template>
              </UCard>
              <div v-if="!filteredItems.length" class="col-span-full py-10 text-center text-sm text-gray-400">
                {{ $t('recipeStore.no_match') }}
              </div>
            </div>
          </template>
        </template>
      </div>

      <!-- 添加配方源 -->
      <UModal v-model:open="showAddSource" :title="$t('recipeStore.add_source_title')">
        <template #body>
            <div class="space-y-3">
              <UFormField :label="$t('recipeStore.col_name')">
                <UInput v-model="newSource.name" :placeholder="$t('recipeStore.source_name_ph')" />
              </UFormField>
              <UFormField :label="$t('recipeStore.col_url')" required>
                <UInput v-model="newSource.url" placeholder="https://github.com/owner/FireworksRecipes.git" />
              </UFormField>
              <UFormField :label="$t('recipeStore.col_branch')">
                <USelectMenu
                  v-model="newSource.branch"
                  value-key="value"
                  :items="sourceBranches.map((branch) => ({
                    label: branch === sourceDefaultBranch ? $t('recipeStore.default_branch', { branch }) : branch,
                    value: branch,
                  }))"
                  :disabled="probingSource || !sourceBranches.length"
                  :loading="probingSource"
                  :placeholder="probingSource ? $t('recipeStore.detecting_branches') : $t('recipeStore.select_branch')"
                />
                <template #hint>{{ $t('recipeStore.branch_auto_hint') }}</template>
              </UFormField>
              <UAlert v-if="sourceProbeError" color="error" variant="subtle" :title="sourceProbeError" />
            </div>
        </template>
        <template #footer>
          <div class="flex w-full justify-end gap-2">
            <UButton variant="outline" @click="showAddSource = false">{{ $t('common.cancel') }}</UButton>
            <UButton color="primary" :loading="addingSource" :disabled="!newSource.url.trim() || !newSource.branch || probingSource || !!sourceProbeError" @click="addSource">{{ $t('recipeStore.add_source') }}</UButton>
          </div>
        </template>
      </UModal>

      <!-- 配方源设置：重新读取远端分支、切换并同步，或删除源。 -->
      <UModal v-model:open="showEditSource" :title="activeSource ? $t('recipeStore.manage_source_title', { name: activeSource.name }) : ''">
        <template #body>
          <div v-if="activeSource" class="space-y-3">
            <UFormField :label="$t('recipeStore.col_url')">
              <UInput :model-value="activeSource.url" disabled />
            </UFormField>
            <UFormField :label="$t('recipeStore.col_branch')">
              <USelectMenu
                v-model="editBranch"
                value-key="value"
                :items="editBranches.map((branch) => ({
                  label: branch === editDefaultBranch ? $t('recipeStore.default_branch', { branch }) : branch,
                  value: branch,
                }))"
                :disabled="probingEditSource || !editBranches.length"
                :loading="probingEditSource"
                :placeholder="probingEditSource ? $t('recipeStore.detecting_branches') : $t('recipeStore.select_branch')"
              />
              <template #hint>{{ $t('recipeStore.branch_change_hint') }}</template>
            </UFormField>
            <UAlert v-if="editProbeError" color="error" variant="subtle" :title="editProbeError" />
          </div>
        </template>
        <template #footer>
          <div v-if="activeSource" class="flex w-full justify-between gap-2">
            <UButton color="error" variant="soft" icon="lucide:trash-2" :disabled="savingSource" @click="requestDeleteActiveSource">{{ $t('recipeStore.delete_source') }}</UButton>
            <div class="flex gap-2">
              <UButton variant="outline" @click="showEditSource = false">{{ $t('common.cancel') }}</UButton>
              <UButton color="primary" :loading="savingSource" :disabled="!editBranch || probingEditSource || !!editProbeError" @click="saveSourceBranch">{{ $t('common.save') }}</UButton>
            </div>
          </div>
        </template>
      </UModal>

      <UModal :open="!!deleteSourceTarget" :title="$t('recipeStore.delete_source_title')" @update:open="(open: boolean) => { if (!open && !deletingSource) deleteSourceTarget = null }">
        <template #body>
          <p class="text-sm text-muted">
            {{ $t('recipeStore.delete_source_confirm', { name: deleteSourceTarget?.name }) }}
          </p>
        </template>
        <template #footer>
          <div class="flex w-full justify-end gap-2">
            <UButton variant="outline" :disabled="deletingSource" @click="deleteSourceTarget = null">{{ $t('common.cancel') }}</UButton>
            <UButton color="error" :loading="deletingSource" @click="confirmDeleteSource">{{ $t('common.confirm') }}</UButton>
          </div>
        </template>
      </UModal>

      <!-- 详情 / README -->
      <UModal v-model:open="detailOpen" :title="detailItem ? (loc(detailItem, 'name') || detailItem.id) : ''" scrollable>
        <template #actions>
          <UBadge v-if="detailItem?.version" color="primary" variant="soft" size="xs">v{{ detailItem.version }}</UBadge>
          <UButton v-if="detailItem" size="xs" color="primary" :loading="importingRecipe" @click="importItem(detailItem, true)">{{ $t('recipeStore.import_run') }}</UButton>
          <UButton v-if="detailItem" size="xs" variant="outline" @click="importItem(detailItem, false)">{{ $t('recipeStore.import_only') }}</UButton>
        </template>
        <template #body>
          <template v-if="detailItem">
            <div>
              <div v-if="readmeLoading" class="py-6 text-center text-sm text-gray-400">{{ $t('common.loading') }}</div>
              <div v-else-if="detailReadme" class="fw-md" v-html="renderMd(detailReadme)"></div>
              <div v-else class="py-6 text-center text-sm text-gray-400">{{ $t('recipeStore.no_readme') }}</div>
            </div>
          </template>
        </template>
        <template #footer>
          <div class="flex w-full justify-end">
            <UButton variant="ghost" @click="detailOpen = false">{{ $t('common.cancel') }}</UButton>
          </div>
        </template>
      </UModal>
    </template>
  </UDashboardPanel>
</template>
