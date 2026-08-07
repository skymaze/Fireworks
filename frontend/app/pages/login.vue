<script setup lang="ts">
/** 登录 / 首次初始化页（Nuxt UI 官方认证页布局：居中卡片 + 品牌区）。
 *
 * - 后端状态未知时先查 /api/auth/status；
 * - setup_required（库中无用户）→ 初始化表单建号；否则 → 登录表单。
 */
const auth = useAuth()
const route = useRoute()
const router = useRouter()
const { t } = useI18n()

const setupMode = ref(false)
const username = ref('')
const password = ref('')
const confirm = ref('')
const error = ref('')
const loading = ref(false)

onMounted(async () => {
  const s = await auth.refresh()
  setupMode.value = s.setupRequired || route.query.setup === '1'
})

async function submit() {
  error.value = ''
  const name = username.value.trim()
  if (!name) { error.value = t('auth.password_required'); return }
  // 登录不在此处做密码复杂度/长度校验（交给后端认证）；仅初始化建号时校验
  if (setupMode.value && password.value.length < 8) {
    error.value = t('auth.password_min')
    return
  }
  if (setupMode.value && password.value !== confirm.value) {
    error.value = t('auth.password_mismatch')
    return
  }
  loading.value = true
  try {
    if (setupMode.value) await auth.setup(name, password.value)
    else await auth.login(name, password.value)
    await router.push('/')
  } catch (e) {
    error.value = errorMsg(e)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="flex items-center justify-center min-h-[calc(100vh-4rem)] px-4">
    <div class="w-full max-w-sm">
      <!-- 语言切换（登录页独立放置，右上角） -->
      <div class="flex justify-end mb-2">
        <LangSwitcher />
      </div>

      <!-- 品牌区 -->
      <div class="text-center mb-8">
        <div
          class="inline-flex items-center justify-center size-14 rounded-2xl bg-gradient-to-br from-amber-400 to-orange-500 text-3xl shadow-md shadow-orange-500/20 mb-4"
        >
          🎆
        </div>
        <h1 class="text-2xl font-semibold tracking-tight text-gray-900 dark:text-white">
          {{ setupMode ? t('auth.create_admin') : t('auth.welcome_back') }}
        </h1>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-1.5">
          {{ setupMode ? t('auth.subtitle_setup') : t('auth.subtitle_login') }}
        </p>
      </div>

      <!-- 表单卡片 -->
      <UCard class="shadow-sm">
        <UAlert
          v-if="error"
          :title="error"
          color="error"
          icon="i-heroicons-exclamation-triangle"
          class="mb-4"
        />
        <form method="post" @submit.prevent="submit" class="space-y-5">
          <UFormField :label="t('auth.username')" required>
            <UInput
              v-model="username"
              :placeholder="t('auth.username_placeholder')"
              autocomplete="username"
              data-1p-ignore
              class="w-full"
            >
              <template #leading>
                <UIcon name="i-heroicons-user-solid" class="size-4 text-gray-400 dark:text-gray-500" />
              </template>
            </UInput>
          </UFormField>

          <UFormField :label="t('auth.password')" required>
            <UInput
              v-model="password"
              type="password"
              :placeholder="t('auth.password_placeholder')"
              autocomplete="current-password"
              data-1p-ignore
              class="w-full"
            >
              <template #leading>
                <UIcon name="i-heroicons-lock-closed-solid" class="size-4 text-gray-400 dark:text-gray-500" />
              </template>
            </UInput>
            <p v-if="setupMode" class="text-xs text-gray-400 dark:text-gray-500 mt-1">
              {{ t('auth.password_min') }}
            </p>
          </UFormField>

          <UFormField v-if="setupMode" :label="t('auth.confirm_password')" required>
            <UInput
              v-model="confirm"
              type="password"
              :placeholder="t('auth.password_placeholder')"
              autocomplete="new-password"
              data-1p-ignore
              class="w-full"
            >
              <template #leading>
                <UIcon name="i-heroicons-lock-closed-solid" class="size-4 text-gray-400 dark:text-gray-500" />
              </template>
            </UInput>
          </UFormField>

          <UButton
            type="submit"
            color="primary"
            size="lg"
            class="w-full justify-center"
            :loading="loading"
          >
            {{ setupMode ? t('auth.create_account') : t('auth.login') }}
          </UButton>
        </form>
      </UCard>

      <!-- 底部说明（仅初始化态：向首次使用者解释单一账号设计） -->
      <p
        v-if="setupMode"
        class="text-center text-xs text-gray-400 dark:text-gray-500 mt-5"
      >
        {{ t('auth.setup_footer') }}
      </p>
    </div>
  </div>
</template>
