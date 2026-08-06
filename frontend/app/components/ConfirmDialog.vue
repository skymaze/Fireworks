<script setup lang="ts">
/** 全局确认弹窗：替代原生 confirm，避免 WebView/嵌入式环境原生对话框不可用。 */
import { registerConfirmHandler } from '~/composables/useConfirmDialog'

const state = ref<{
  open: boolean
  title: string
  description: string
  color: 'error' | 'warning' | 'primary' | 'neutral'
}>({ open: false, title: '', description: '', color: 'error' })

let resolver: ((ok: boolean) => void) | null = null

function open(opts: { title: string; description: string; color?: 'error' | 'warning' | 'primary' | 'neutral' }) {
  state.value = { open: true, title: opts.title, description: opts.description, color: opts.color || 'error' }
  return new Promise<boolean>((resolve) => {
    resolver = resolve
  })
}

function close(ok: boolean) {
  state.value.open = false
  resolver?.(ok)
  resolver = null
}

onMounted(() => registerConfirmHandler(open))

onUnmounted(() => registerConfirmHandler(null))
</script>

<template>
  <UModal v-model:open="state.open">
    <template #content>
      <UCard>
        <template #header>
          <div class="font-semibold">{{ state.title }}</div>
        </template>
        <p class="text-sm text-gray-600 dark:text-gray-300">{{ state.description }}</p>
        <template #footer>
          <div class="flex justify-end gap-2">
            <UButton variant="outline" @click="close(false)">取消</UButton>
            <UButton :color="state.color" @click="close(true)">确认</UButton>
          </div>
        </template>
      </UCard>
    </template>
  </UModal>
</template>
