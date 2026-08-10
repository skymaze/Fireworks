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

function handleOpenChange(open: boolean) {
  if (open) {
    state.value.open = true
    return
  }
  close(false)
}

onMounted(() => registerConfirmHandler(open))

onUnmounted(() => registerConfirmHandler(null))
</script>

<template>
  <UModal :open="state.open" :title="state.title" @update:open="handleOpenChange">
    <template #body>
      <p class="text-sm text-muted">{{ state.description }}</p>
    </template>
    <template #footer>
      <div class="flex w-full justify-end gap-2">
        <UButton variant="outline" @click="close(false)">{{ $t('common.cancel') }}</UButton>
        <UButton :color="state.color" @click="close(true)">{{ $t('common.confirm') }}</UButton>
      </div>
    </template>
  </UModal>
</template>
