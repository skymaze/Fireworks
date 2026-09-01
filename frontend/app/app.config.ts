export default defineAppConfig({
  ui: {
    formField: {
      slots: {
        // 默认 label/hint 同一 flex 行且不换行：窄列下长 hint 会把 CJK label 挤成单字竖排。
        // 加 flex-wrap 后放不下的 hint 整块换到下一行；hint 设 min-w-0 以便同排时能就地收敛折行。
        labelWrapper: 'flex flex-wrap content-center items-center justify-between gap-1',
        hint: 'min-w-0',
      },
    },
  },
})
