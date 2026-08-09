/**
 * 配方内容双语工具（中文 zh 为默认主语言，英文用并列 `_en` 字段）。
 *
 * 约定：recipe.json / manifest / Recipe 行里的文本字段都可带英文并列字段
 * `xxx_en`（如 name_en、description_en、label_en、help_en、readme_en）。
 * 展示时按当前 locale 选择：en 优先取 `_en`，缺省回退主语言；zh 反之。
 */
export function useLocalized() {
  const { locale } = useI18n()
  const isEn = computed(() => locale.value === 'en')

  // 直接从主字段 + 英文并列字段选值
  const pick = (base?: string | null, en?: string | null): string => {
    const b = base || ''
    const e = en || ''
    return isEn.value ? (e || b) : (b || e)
  }

  // 从对象取双语字段：loc(v, 'label') -> en 优先 v.label_en，否则 v.label
  const loc = (obj: any, key: string): string => pick(obj?.[key], obj?.[`${key}_en`])

  return { pick, loc, isEn }
}
