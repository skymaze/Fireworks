/** 全局确认弹窗（替代原生 confirm，配合 app.vue 中的 ConfirmDialog 组件）。 */

interface ConfirmOptions {
  title: string
  description: string
  color?: 'error' | 'warning' | 'primary' | 'neutral'
}

type ConfirmHandler = (opts: ConfirmOptions) => Promise<boolean>

let handler: ConfirmHandler | null = null

export function registerConfirmHandler(h: ConfirmHandler | null) {
  handler = h
}

export function useConfirmDialog() {
  return {
    open: (opts: ConfirmOptions): Promise<boolean> =>
      handler ? handler(opts) : Promise.resolve(false),
  }
}
