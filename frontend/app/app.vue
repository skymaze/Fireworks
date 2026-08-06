<script setup lang="ts">
import '~/assets/css/main.css'
import { en as enUi, zh_cn as zhCnUi } from '@nuxt/ui/locale'

useHead({ title: 'Fireworks · DGX Spark 集群管理工具' })

const route = useRoute()
const auth = useAuth()
const { t, locale } = useI18n()
// Nuxt UI v3 内置标签（表格空态/下拉空态/日历等）跟随应用语言
const uiLocale = computed(() => (locale.value === 'en' ? enUi : zhCnUi))

const nav = computed(() => [
  { label: t('nav.home'), to: '/' },
  { label: t('nav.nodes'), to: '/nodes' },
  { label: t('nav.clusters'), to: '/clusters' },
  { label: t('nav.models'), to: '/models' },
  { label: t('nav.images'), to: '/images' },
  { label: t('nav.recipes'), to: '/recipes' },
  { label: t('nav.tasks'), to: '/tasks' },
])

// 修改密码对话框（用户下拉触发）
const showChangePwd = ref(false)
const userMenuOpen = ref(false)
const pwdForm = reactive({ old: '', neu: '', confirm: '' })
const pwdError = ref('')
const pwdNotice = ref('')
const pwdLoading = ref(false)

async function changePassword() {
  pwdError.value = ''
  pwdNotice.value = ''
  if (pwdForm.neu.length < 8) { pwdError.value = t('auth.new_password_min'); return }
  if (pwdForm.neu !== pwdForm.confirm) { pwdError.value = t('auth.new_password_mismatch'); return }
  pwdLoading.value = true
  try {
    await auth.changePassword(pwdForm.old, pwdForm.neu)
    pwdNotice.value = t('auth.password_updated')
    pwdForm.old = ''
    pwdForm.neu = ''
    pwdForm.confirm = ''
    setTimeout(() => { showChangePwd.value = false; pwdNotice.value = '' }, 900)
  } catch (e) {
    pwdError.value = errorMsg(e)
  } finally {
    pwdLoading.value = false
  }
}

async function logout() {
  await auth.logout()
  await navigateTo('/login')
}
</script>

<template>
  <UApp :locale="uiLocale">
    <div class="min-h-screen bg-gray-50 dark:bg-gray-950">
      <header
        v-if="route.path !== '/login'"
        class="border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900"
      >
        <UContainer>
          <div class="flex items-center justify-between h-16">
            <NuxtLink to="/" class="flex items-center gap-2 text-lg font-bold">
              <span>🎆</span>
              <span>Fireworks</span>
              <span class="text-xs font-normal text-gray-400 dark:text-gray-500">{{ t('nav.subtitle') }}</span>
            </NuxtLink>
            <div class="flex items-center">
              <nav class="flex gap-1">
                <NuxtLink
                  v-for="item in nav"
                  :key="item.to"
                  :to="item.to"
                  class="px-3 py-1.5 rounded-md text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800"
                  :class="{ 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white font-medium': $route.path === item.to || (item.to !== '/' && $route.path.startsWith(item.to)) }"
                >
                  {{ item.label }}
                </NuxtLink>
              </nav>
              <div class="ml-4 pl-4 flex items-center gap-1 border-l border-gray-200 dark:border-gray-800">
                <LangSwitcher />
                <UPopover v-model:open="userMenuOpen">
                  <UButton
                    variant="ghost"
                    color="gray"
                    size="sm"
                    leading-icon="i-heroicons-user-circle"
                    trailing-icon="i-heroicons-chevron-down"
                    :label="auth.state.value.username || '-'"
                    class="max-w-[10rem]"
                  />
                  <template #content>
                    <div class="w-40 p-1">
                      <button
                        type="button"
                        class="flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/60"
                        @click="userMenuOpen = false; showChangePwd = true"
                      >
                        <UIcon name="i-heroicons-key" class="size-4" />
                        {{ t('auth.change_password') }}
                      </button>
                      <button
                        type="button"
                        class="flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-500/10"
                        @click="logout"
                      >
                        <UIcon name="i-heroicons-power" class="size-4" />
                        {{ t('auth.logout') }}
                      </button>
                    </div>
                  </template>
                </UPopover>
              </div>
            </div>
          </div>
        </UContainer>
      </header>
      <UContainer class="py-6">
        <NuxtPage />
      </UContainer>
    </div>

    <UModal v-model:open="showChangePwd">
      <template #content>
        <UCard>
          <template #header>
            <div class="font-semibold">{{ t('auth.change_password') }}</div>
          </template>
          <UAlert
            v-if="pwdError"
            :title="pwdError"
            color="error"
            icon="i-heroicons-exclamation-triangle"
            class="mb-4"
          />
          <UAlert
            v-if="pwdNotice"
            :title="pwdNotice"
            color="success"
            icon="i-heroicons-check-circle"
            class="mb-4"
          />
          <form @submit.prevent="changePassword" class="space-y-4">
            <UFormField :label="t('auth.old_password')" required>
              <UInput v-model="pwdForm.old" type="password" autocomplete="current-password" data-1p-ignore />
            </UFormField>
            <UFormField :label="t('auth.new_password')" required>
              <UInput v-model="pwdForm.neu" type="password" placeholder="••••••••" autocomplete="new-password" data-1p-ignore />
            </UFormField>
            <UFormField :label="t('auth.confirm_password')" required>
              <UInput v-model="pwdForm.confirm" type="password" placeholder="••••••••" autocomplete="new-password" data-1p-ignore />
            </UFormField>
            <UButton type="submit" color="primary" class="w-full justify-center" :loading="pwdLoading">
              {{ t('auth.change_submit') }}
            </UButton>
          </form>
        </UCard>
      </template>
    </UModal>
    <ConfirmDialog />
  </UApp>
</template>
