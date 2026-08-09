/**
 * 应用壳层共享状态（模拟官方模板 useDashboard 的 createSharedComposable 语义，
 * 不引入额外依赖：模块级单例 ref，跨组件共享并随路由自动复位）。
 */
const sidebarOpen = ref(false)

export function useDashboard() {
  const route = useRoute()

  // 路由切换自动收起移动端侧栏 / 通知面板
  watch(
    () => route.fullPath,
    () => {
      sidebarOpen.value = false
    },
  )

  return {
    sidebarOpen,
  }
}
