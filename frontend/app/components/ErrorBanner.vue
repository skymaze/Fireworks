<script setup lang="ts">
/** 错误通知（toast 化）：替代页面上原先的顶部 error 横幅 UAlert。
 *
 * 收到新的 error 即弹 toast（不阻塞、自动消失），自身不渲染任何视觉元素；
 * 相同消息去重，避免重复弹。错误文案由调用方（errorMsg(e)）预先整理。 */
const props = defineProps<{ error?: string | null | undefined }>()
const toast = useToast()
let last = ''

function notify(v: string) {
  if (v && v !== last) {
    last = v
    toast.add({ title: v, color: 'error' })
  }
}

watch(() => props.error, (v) => notify(v || ''))
onMounted(() => notify(props.error || ''))
</script>

<template>
  <div class="hidden" />
</template>
