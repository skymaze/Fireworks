<script setup lang="ts">
/* 应用壳层（参考 nuxt-ui-templates/dashboard）：
   UDashboardGroup + 可折叠/可调宽侧边栏（导航 + 底部用户菜单）+ Cmd/Ctrl+K 命令面板。 */
const route = useRoute()
const { t } = useI18n()
const { links, searchGroups } = useAppNav()
// useDashboard 内置：路由切换自动收起侧栏（移动端）
const { sidebarOpen } = useDashboard()

const isLogin = computed(() => route.path === '/login')

// 快捷键 g-* 跳转（官方模板 defineShortcuts 风格）
defineShortcuts({
  'g-h': () => navigateTo('/'),
  'g-n': () => navigateTo('/nodes'),
  'g-c': () => navigateTo('/clusters'),
  'g-m': () => navigateTo('/models'),
  'g-i': () => navigateTo('/images'),
  'g-r': () => navigateTo('/recipes'),
  'g-t': () => navigateTo('/tasks'),
})
</script>

<template>
  <!-- 登录/初始化页：裸布局 -->
  <div v-if="isLogin" class="min-h-screen bg-gray-50 dark:bg-gray-950">
    <slot />
  </div>

  <!-- 已登录：仪表盘壳层 -->
  <UDashboardGroup v-else unit="rem">
    <UDashboardSidebar
      id="app"
      v-model:open="sidebarOpen"
      collapsible
      resizable
      class="bg-elevated/25"
      :ui="{ footer: 'lg:border-t lg:border-default' }"
    >
      <template #header="{ collapsed }">
        <NuxtLink to="/" class="flex items-center gap-2 py-1 min-w-0">
          <span class="text-xl shrink-0">🎆</span>
          <span v-if="!collapsed" class="font-bold truncate">Fireworks</span>
        </NuxtLink>
      </template>

      <template #default="{ collapsed }">
        <UDashboardSearchButton :collapsed="collapsed" class="bg-transparent ring-default" />

        <UNavigationMenu
          :collapsed="collapsed"
          :items="links[0]"
          orientation="vertical"
          tooltip
        />
      </template>

      <template #footer="{ collapsed }">
        <AppUserMenu :collapsed="collapsed" />
      </template>
    </UDashboardSidebar>

    <UDashboardSearch :groups="searchGroups" />

    <slot />
  </UDashboardGroup>
</template>
