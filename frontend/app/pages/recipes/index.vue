<script setup lang="ts">
const api = useApi()
const confirm = useConfirmDialog()
const recipes = ref<any[]>([])
const error = ref('')
const notice = ref('')

const showImport = ref(false)
const importJson = ref('')
const importing = ref(false)

async function load() {
  try {
    recipes.value = await api.get('/recipes')
    error.value = ''
  } catch (e) {
    error.value = String(e)
  }
}

async function duplicate(r: any) {
  await api.post(`/recipes/${r.id}/duplicate`)
  notice.value = `已复制 ${r.name}`
  await load()
}

async function removeRecipe(r: any) {
  const ok = await confirm.open({ title: '删除配方', description: `确认删除配方「${r.name}」？` })
  if (!ok) return
  await api.del(`/recipes/${r.id}`)
  await load()
}

async function exportRecipe(r: any) {
  const data = await api.get(`/recipes/${r.id}/export`)
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${r.name}.recipe.json`
  a.click()
  URL.revokeObjectURL(url)
}

async function doImport() {
  importing.value = true
  error.value = ''
  try {
    const parsed = JSON.parse(importJson.value)
    const r = await api.post('/recipes/import', parsed)
    showImport.value = false
    importJson.value = ''
    notice.value = r.import_notice ? `导入成功。${r.import_notice}` : '导入成功'
    await load()
  } catch (e) {
    error.value = String(e)
  } finally {
    importing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="flex items-center justify-between mb-4">
      <h1 class="text-xl font-bold">配方管理</h1>
      <div class="flex gap-2">
        <UButton variant="outline" @click="showImport = true">导入配方</UButton>
        <UButton color="primary" to="/recipes/new">新建配方</UButton>
      </div>
    </div>

    <UAlert v-if="error" :title="error" color="error" class="mb-4" />
    <UAlert v-if="notice" :title="notice" color="success" class="mb-4" />

    <UCard>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
              <th class="py-2 pr-4 font-medium">名称</th>
              <th class="py-2 pr-4 font-medium">镜像</th>
              <th class="py-2 pr-4 font-medium">变量数</th>
              <th class="py-2 pr-4 font-medium">描述</th>
              <th class="py-2 font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in recipes" :key="r.id" class="border-b border-gray-100 dark:border-gray-800/60">
              <td class="py-2.5 pr-4">
                <NuxtLink :to="`/recipes/${r.id}`" class="font-medium hover:underline">{{ r.name }}</NuxtLink>
                <UBadge v-if="r.is_seed" size="xs" variant="subtle" class="ml-1">内置</UBadge>
              </td>
              <td class="py-2.5 pr-4 font-mono text-xs text-gray-500 truncate max-w-[260px]">{{ r.image || '—' }}</td>
              <td class="py-2.5 pr-4">{{ r.variables?.length || 0 }}</td>
              <td class="py-2.5 pr-4 text-gray-500 truncate max-w-[240px]">{{ r.description || '—' }}</td>
              <td class="py-2.5 text-right whitespace-nowrap">
                <UButton size="xs" variant="ghost" :to="`/recipes/${r.id}`">编辑</UButton>
                <UButton size="xs" variant="ghost" @click="duplicate(r)">复制</UButton>
                <UButton size="xs" variant="ghost" @click="exportRecipe(r)">导出</UButton>
                <UButton size="xs" variant="ghost" color="error" @click="removeRecipe(r)">删除</UButton>
              </td>
            </tr>
            <tr v-if="!recipes.length">
              <td colspan="5" class="py-8 text-center text-gray-400">暂无配方</td>
            </tr>
          </tbody>
        </table>
      </div>
    </UCard>

    <UModal v-model:open="showImport">
      <template #content>
        <UCard>
        <template #header><div class="font-semibold">导入配方（JSON）</div></template>
        <UFormField label="配方 JSON" hint="可用任一配方的「导出」获取 JSON 格式">
          <UTextarea v-model="importJson" :rows="12" class="font-mono text-xs w-full" placeholder='{"name": "...", "compose_template": "...", "variables": [...]}' />
        </UFormField>
        <template #footer>
          <div class="flex justify-end gap-2">
            <UButton variant="outline" @click="showImport = false">取消</UButton>
            <UButton color="primary" :loading="importing" :disabled="!importJson.trim()" @click="doImport">导入</UButton>
          </div>
        </template>
      </UCard>
      </template>
    </UModal>
  </div>
</template>
