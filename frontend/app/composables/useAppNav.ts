import type { NavigationMenuItem } from '@nuxt/ui'

/**
 * 侧边栏导航 / 命令面板共享的导航项（参考 nuxt-ui-templates/dashboard 的壳层组织）。
 * 全部使用 Nuxt UI v4 图标（lucide）。
 */
export function useAppNav() {
  const { t } = useI18n()

  const links = computed<NavigationMenuItem[][]>(() => [
    [
      { label: t('nav.home'), icon: 'i-lucide-house', to: '/' },
      { label: t('nav.nodes'), icon: 'i-lucide-server', to: '/nodes' },
      { label: t('nav.clusters'), icon: 'i-lucide-boxes', to: '/clusters' },
      { label: t('nav.models'), icon: 'i-lucide-cpu', to: '/models' },
      { label: t('nav.images'), icon: 'i-lucide-image', to: '/images' },
      { label: t('recipeStore.tab_local'), icon: 'i-lucide-list-checks', to: '/recipes' },
      { label: t('recipeStore.tab_store'), icon: 'i-lucide-store', to: '/store' },
      { label: t('nav.tasks'), icon: 'i-lucide-rocket', to: '/tasks' },
    ],
  ])

  // 命令面板（Cmd/Ctrl+K）分组
  const searchGroups = computed(() => [
    {
      id: 'pages',
      label: t('nav.pages'),
      items: links.value.flat().map((i) => ({ label: i.label, icon: i.icon, to: i.to })),
    },
  ])

  return { links, searchGroups }
}
