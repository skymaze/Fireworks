<script setup lang="ts">
/* 应用根：全局 UI 语言 + 壳层布局（侧边栏导航见 app/layouts/default.vue）。 */
import '~/assets/css/main.css'
import { en as enUi, zh_cn as zhCnUi } from '@nuxt/ui/locale'

useHead({ title: 'Fireworks · DGX Spark 集群管理工具' })

const { locale } = useI18n()
// Nuxt UI v4 内置标签（表格空态/下拉空态/日历等）跟随应用语言；
// 上游 locale 的 dashboardSearch 缺 title/description，此处补齐命令面板弹窗头。
const uiLocale: ComputedRef<any> = computed(() => {
  const base: any = locale.value === 'en' ? enUi : zhCnUi
  return {
    ...base,
    dashboardSearch: {
      ...(base.dashboardSearch || {}),
      title: locale.value === 'en' ? 'Search' : '搜索',
      description: locale.value === 'en' ? 'Type to search pages and commands' : '输入以搜索页面或执行命令',
    },
  }
})
</script>

<template>
  <UApp :locale="uiLocale">
    <NuxtLoadingIndicator />

    <NuxtLayout>
      <NuxtPage />
    </NuxtLayout>

    <!-- 全局确认弹窗（确认删除等） -->
    <ConfirmDialog />
  </UApp>
</template>
