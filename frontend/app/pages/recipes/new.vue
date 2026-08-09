<script setup lang="ts">
const router = useRouter()
const formRef = ref<any>(null)
</script>

<template>
  <UDashboardPanel id="recipe-new">
    <template #header>
      <!-- 标题栏右侧放保存按钮 + 未保存状态（与配方管理页「新建配方」按钮同款布局） -->
      <UDashboardNavbar>
        <template #leading>
          <UButton size="sm" variant="ghost" to="/recipes">{{ $t('common.back') }}</UButton>
        </template>
        <template #title>{{ $t('recipes.create') }}</template>
        <template #right>
          <div class="flex items-center gap-2">
            <span v-if="formRef?.dirty" class="text-xs text-amber-600 dark:text-amber-400 flex items-center gap-1">
              <UIcon name="i-lucide-triangle-alert" class="size-4" />
              {{ $t('recipes.unsaved') }}
            </span>
            <UButton color="primary" size="sm" :loading="formRef?.saving" :disabled="!formRef?.canSave || formRef?.savingVars" @click="formRef?.save()">{{ $t('recipes.save') }}</UButton>
          </div>
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
    <div>
      <RecipeForm ref="formRef" @saved="(r: any) => router.push(`/recipes/${r.id}`)" />
    </div>
    </template>
  </UDashboardPanel>
</template>
