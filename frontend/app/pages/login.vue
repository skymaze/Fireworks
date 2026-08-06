<script setup lang="ts">
/** 登录 / 首次初始化页（Nuxt UI 官方认证页布局：居中卡片 + 品牌区）。
 *
 * - 后端状态未知时先查 /api/auth/status；
 * - setup_required（库中无用户）→ 初始化表单建号；否则 → 登录表单。
 */
const auth = useAuth()
const route = useRoute()
const router = useRouter()

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
  if (!name) { error.value = '请输入用户名'; return }
  // 登录不在此处做密码复杂度/长度校验（交给后端认证）；仅初始化建号时校验
  if (setupMode.value && password.value.length < 8) {
    error.value = '密码至少 8 位'
    return
  }
  if (setupMode.value && password.value !== confirm.value) {
    error.value = '两次输入的密码不一致'
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
      <!-- 品牌区 -->
      <div class="text-center mb-8">
        <div
          class="inline-flex items-center justify-center size-14 rounded-2xl bg-gradient-to-br from-amber-400 to-orange-500 text-3xl shadow-md shadow-orange-500/20 mb-4"
        >
          🎆
        </div>
        <h1 class="text-2xl font-semibold tracking-tight text-gray-900 dark:text-white">
          {{ setupMode ? '创建管理员账号' : '欢迎回来' }}
        </h1>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-1.5">
          {{ setupMode ? '首次使用 · 为控制平面设置唯一登录账号' : '登录 Fireworks 控制平面' }}
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
        <form @submit.prevent="submit" class="space-y-5">
          <UFormField label="用户名" required>
            <UInput
              v-model="username"
              placeholder="admin"
              autocomplete="username"
              data-1p-ignore
              class="w-full"
            >
              <template #leading>
                <UIcon name="i-heroicons-user-solid" class="size-4 text-gray-400 dark:text-gray-500" />
              </template>
            </UInput>
          </UFormField>

          <UFormField label="密码" required>
            <UInput
              v-model="password"
              type="password"
              placeholder="••••••••"
              autocomplete="current-password"
              data-1p-ignore
              class="w-full"
            >
              <template #leading>
                <UIcon name="i-heroicons-lock-closed-solid" class="size-4 text-gray-400 dark:text-gray-500" />
              </template>
            </UInput>
            <p v-if="setupMode" class="text-xs text-gray-400 dark:text-gray-500 mt-1">
              密码至少 8 位
            </p>
          </UFormField>

          <UFormField v-if="setupMode" label="确认密码" required>
            <UInput
              v-model="confirm"
              type="password"
              placeholder="••••••••"
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
            {{ setupMode ? '创建账号并进入' : '登录' }}
          </UButton>
        </form>
      </UCard>

      <!-- 底部说明（仅初始化态：向首次使用者解释单一账号设计） -->
      <p
        v-if="setupMode"
        class="text-center text-xs text-gray-400 dark:text-gray-500 mt-5"
      >
        单一用户控制台：账号创建后仅此账号可登录，修改密码在登录后右上角进行。
      </p>
    </div>
  </div>
</template>
