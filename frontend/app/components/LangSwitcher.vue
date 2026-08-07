<script setup lang="ts">
/** 语言切换（中文 / English）：地球图标 + 当前语言 + 下拉（勾选高亮）。
 * 经典 SaaS 风格；选择写入 @nuxtjs/i18n 的 fw_locale cookie 记住。 */
const { locale, setLocale } = useI18n()

const langs = [
  { label: '中文', code: 'zh' },
  { label: 'English', code: 'en' },
]

const current = computed(
  () => langs.find((l) => l.code === locale.value)?.label ?? String(locale.value),
)
</script>

<template>
  <UPopover>
    <UButton
      variant="ghost"
      color="gray"
      size="sm"
      :label="current"
      leading-icon="i-lucide-languages"
      trailing-icon="i-lucide-chevron-down"
    />
    <template #content>
      <div class="w-34 p-1">
        <button
          v-for="l in langs"
          :key="l.code"
          type="button"
          class="flex w-full items-center justify-between rounded-md px-3 py-2 text-sm transition-colors"
          :class="l.code === locale
            ? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white'
            : 'text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/60'"
          @click="setLocale(l.code)"
        >
          <span>{{ l.label }}</span>
          <UIcon v-if="l.code === locale" name="i-lucide-check" class="size-4" />
        </button>
      </div>
    </template>
  </UPopover>
</template>
