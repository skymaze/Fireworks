<script setup lang="ts">

const { t } = useI18n()
const api = useApi()
const confirm = useConfirmDialog()
const toast = useToast()

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
    localError.value = errorMsg(e)
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
    localError.value = errorMsg(e)
  } finally {
    importing.value = false
  }
}

onMounted(() => {
  loadRecipes()
})

</script>

<template>
  <UDashboardPanel id="recipes">
    <template #header>
      <UDashboardNavbar :title="$t('recipeStore.tab_local')">
        <template #right>
          <div class="flex gap-2">
            <UButton variant="outline" @click="showImport = true">{{ $t('recipes.import') }}</UButton>
            <UButton color="primary" to="/recipes/new">{{ $t('recipes.create') }}</UButton>
          </div>
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
  <!-- ================= 本地配方（卡片） ================= -->
      <div>
        <ErrorBanner :error="localError" />

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
              <UButton size="sm" color="primary" :to="`/tasks/publish?recipe=${r.id}`">{{ $t('recipes.run') }}</UButton>
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

    
    </template>
  </UDashboardPanel>
</template>
