<script setup lang="ts">
const route = useRoute()
const api = useApi()
const recipe = ref<any>(null)
const error = ref('')
const formRef = ref<any>(null)

async function load() {
  try {
    recipe.value = await api.get(`/recipes/${route.params.id}`)
  } catch (e) {
    error.value = String(e)
  }
}

onMounted(load)
</script>

<template>
  <div>
    <!-- 标题栏右侧放保存按钮 + 未保存状态（与配方管理页「新建配方」按钮同款布局） -->
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center gap-3">
        <UButton size="sm" variant="ghost" to="/recipes">{{ $t('common.back') }}</UButton>
        <h1 class="text-xl font-bold">{{ $t('recipes.edit_title') }}</h1>
      </div>
      <div class="flex items-center gap-2">
        <span v-if="formRef?.dirty" class="text-xs text-amber-600 dark:text-amber-400 flex items-center gap-1">
          <UIcon name="i-heroicons-exclamation-triangle" class="size-4" />
          {{ $t('recipes.unsaved') }}
        </span>
        <UButton color="primary" size="sm" :loading="formRef?.saving" :disabled="!formRef?.canSave || formRef?.savingVars" @click="formRef?.save()">{{ $t('recipes.save') }}</UButton>
      </div>
    </div>
    <UAlert v-if="error" :title="error" color="error" class="mb-4" />
    <RecipeForm v-if="recipe" ref="formRef" :recipe="recipe" @saved="() => {}" />
  </div>
</template>
