<script setup lang="ts">
import '~/assets/css/main.css'

useHead({ title: 'Fireworks · DGX Spark 集群管理工具' })

const route = useRoute()
const auth = useAuth()

const nav = [
  { label: '总览', to: '/' },
  { label: '节点', to: '/nodes' },
  { label: '集群', to: '/clusters' },
  { label: '模型', to: '/models' },
  { label: '镜像', to: '/images' },
  { label: '配方', to: '/recipes' },
  { label: '任务', to: '/tasks' },
]

// 修改密码对话框
const showChangePwd = ref(false)
const pwdForm = reactive({ old: '', neu: '', confirm: '' })
const pwdError = ref('')
const pwdNotice = ref('')
const pwdLoading = ref(false)

async function changePassword() {
  pwdError.value = ''
  pwdNotice.value = ''
  if (pwdForm.neu.length < 8) { pwdError.value = '新密码至少 8 位'; return }
  if (pwdForm.neu !== pwdForm.confirm) { pwdError.value = '两次输入的新密码不一致'; return }
  pwdLoading.value = true
  try {
    await auth.changePassword(pwdForm.old, pwdForm.neu)
    pwdNotice.value = '密码已更新'
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
  <UApp>
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
              <span class="text-xs font-normal text-gray-400 dark:text-gray-500">DGX Spark 集群管理</span>
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
                <span class="text-sm text-gray-600 dark:text-gray-300 truncate max-w-[10rem]">
                  {{ auth.state.value.username }}
                </span>
                <UButton size="xs" variant="ghost" @click="showChangePwd = true">修改密码</UButton>
                <UButton size="xs" variant="ghost" color="error" @click="logout">退出</UButton>
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
            <div class="font-semibold">修改密码</div>
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
            <UFormField label="当前密码" required>
              <UInput v-model="pwdForm.old" type="password" autocomplete="current-password" data-1p-ignore />
            </UFormField>
            <UFormField label="新密码" required>
              <UInput v-model="pwdForm.neu" type="password" placeholder="至少 8 位" autocomplete="new-password" data-1p-ignore />
            </UFormField>
            <UFormField label="确认新密码" required>
              <UInput v-model="pwdForm.confirm" type="password" placeholder="再次输入新密码" autocomplete="new-password" data-1p-ignore />
            </UFormField>
            <UButton type="submit" color="primary" class="w-full justify-center" :loading="pwdLoading">
              确认修改
            </UButton>
          </form>
        </UCard>
      </template>
    </UModal>
    <ConfirmDialog />
  </UApp>
</template>
