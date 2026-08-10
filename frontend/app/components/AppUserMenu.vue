<script setup lang="ts">
/** 侧边栏底部用户菜单（参考官方 dashboard 模板）：改密 / 语言 / 主题 / 登出。 */
import type { DropdownMenuItem } from '@nuxt/ui'
import { errorMsg } from '~/composables/useApi'

defineProps<{ collapsed?: boolean }>()

const { t, locale, setLocale } = useI18n()
const auth = useAuth()
const colorMode = useColorMode()

// ---------- 修改密码（沿用原 app.vue 逻辑） ----------
const showChangePwd = ref(false)
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

const themeItems: DropdownMenuItem[] = [
  { label: t('ui.theme.system'), icon: 'i-lucide-monitor', type: 'checkbox', checked: colorMode.value === 'system', onSelect: () => { colorMode.preference = 'system' } },
  { label: t('ui.theme.light'), icon: 'i-lucide-sun', type: 'checkbox', checked: colorMode.value === 'light', onSelect: () => { colorMode.preference = 'light' } },
  { label: t('ui.theme.dark'), icon: 'i-lucide-moon', type: 'checkbox', checked: colorMode.value === 'dark', onSelect: () => { colorMode.preference = 'dark' } },
]

const languageItems: DropdownMenuItem[] = [
  { label: '中文', type: 'checkbox', checked: locale.value === 'zh', onSelect: () => setLocale('zh') },
  { label: 'English', type: 'checkbox', checked: locale.value === 'en', onSelect: () => setLocale('en') },
]

const items = computed<DropdownMenuItem[][]>(() => [
  [{
    type: 'label',
    label: auth.state.value.username || '—',
  }],
  [{
    label: t('auth.change_password'),
    icon: 'i-lucide-key',
    onSelect: () => { showChangePwd.value = true },
  }],
  [{
    label: t('ui.language'),
    icon: 'i-lucide-languages',
    children: languageItems,
  }, {
    label: t('ui.theme_label'),
    icon: 'i-lucide-palette',
    children: themeItems,
  }],
  [{
    label: t('auth.logout'),
    icon: 'i-lucide-power',
    color: 'error',
    onSelect: logout,
  }],
])
</script>

<template>
  <div>
    <UDropdownMenu
      :items="items"
      :content="{ align: 'end', collisionPadding: 12 }"
    >
      <UButton
        color="neutral"
        variant="ghost"
        block
        :square="collapsed"
        :label="collapsed ? undefined : auth.state.value.username || '—'"
        leading-icon="i-lucide-circle-user"
        :trailing-icon="collapsed ? undefined : 'i-lucide-chevrons-up-down'"
        class="data-[state=open]:bg-elevated"
      />
    </UDropdownMenu>

    <!-- 修改密码弹窗 -->
    <UModal v-model:open="showChangePwd" :title="t('auth.change_password')">
      <template #body>
          <UAlert
            v-if="pwdError"
            :title="pwdError"
            color="error"
            icon="i-lucide-triangle-alert"
            class="mb-4"
          />
          <UAlert
            v-if="pwdNotice"
            :title="pwdNotice"
            color="success"
            icon="i-lucide-circle-check"
            class="mb-4"
          />
          <form id="change-password-form" method="post" class="space-y-4" @submit.prevent="changePassword">
            <UFormField :label="t('auth.old_password')" required>
              <UInput v-model="pwdForm.old" type="password" autocomplete="current-password" data-1p-ignore class="w-full" />
            </UFormField>
            <UFormField :label="t('auth.new_password')" required>
              <UInput v-model="pwdForm.neu" type="password" placeholder="••••••••" autocomplete="new-password" data-1p-ignore class="w-full" />
            </UFormField>
            <UFormField :label="t('auth.confirm_password')" required>
              <UInput v-model="pwdForm.confirm" type="password" placeholder="••••••••" autocomplete="new-password" data-1p-ignore class="w-full" />
            </UFormField>
          </form>
      </template>
      <template #footer>
        <UButton type="submit" form="change-password-form" color="primary" class="w-full justify-center" :loading="pwdLoading">
          {{ t('auth.change_submit') }}
        </UButton>
      </template>
    </UModal>
  </div>
</template>
