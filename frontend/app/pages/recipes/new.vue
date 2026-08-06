<script setup lang="ts">
const router = useRouter()
const formRef = ref<any>(null)
</script>

<template>
  <div>
    <!-- 标题栏右侧放保存按钮 + 未保存状态（与配方管理页「新建配方」按钮同款布局） -->
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center gap-3">
        <UButton size="sm" variant="ghost" to="/recipes">返回</UButton>
        <h1 class="text-xl font-bold">新建配方</h1>
      </div>
      <div class="flex items-center gap-2">
        <span v-if="formRef?.dirty" class="text-xs text-amber-600 dark:text-amber-400 flex items-center gap-1">
          <UIcon name="i-heroicons-exclamation-triangle" class="size-4" />
          有未保存的修改
        </span>
        <UButton color="primary" size="sm" :loading="formRef?.saving" :disabled="!formRef?.canSave || formRef?.savingVars" @click="formRef?.save()">保存配方</UButton>
      </div>
    </div>
    <RecipeForm ref="formRef" @saved="(r: any) => router.push(`/recipes/${r.id}`)" />
  </div>
</template>
