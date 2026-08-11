<script setup lang="ts">
const props = defineProps<{
  open: boolean
  title: string
  resource: string
  clusters: any[]
  loading?: boolean
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  submit: [selection: { clusterId: number; headNodeId: number; syncNodeIds: number[] }]
}>()

const { t } = useI18n()
const clusterId = ref<number | null>(null)
const selectedNodeIds = ref<number[]>([])

const selectedCluster = computed(() => props.clusters.find(cluster => cluster.id === clusterId.value))
const clusterNodes = computed(() => (selectedCluster.value?.members || [])
  .slice()
  .sort((a: any, b: any) => a.net_index - b.net_index)
  .map((member: any) => member.node)
  .filter(Boolean))

watch(() => props.open, (open) => {
  if (!open) return
  clusterId.value = null
  selectedNodeIds.value = []
})

watch(clusterId, () => {
  selectedNodeIds.value = clusterNodes.value.map((node: any) => node.id)
})

function setNodeSelected(nodeId: number, selected: boolean) {
  if (selected) {
    if (!selectedNodeIds.value.includes(nodeId)) selectedNodeIds.value.push(nodeId)
    return
  }
  selectedNodeIds.value = selectedNodeIds.value.filter(id => id !== nodeId)
}

function submit() {
  if (!clusterId.value || !selectedNodeIds.value.length) return
  emit('submit', {
    clusterId: clusterId.value,
    headNodeId: selectedNodeIds.value[0]!,
    syncNodeIds: selectedNodeIds.value.slice(1),
  })
}
</script>

<template>
  <UModal
    :open="open"
    :title="title"
    :ui="{ content: 'sm:max-w-xl' }"
    @update:open="emit('update:open', $event)"
  >
    <template #body>
      <div class="space-y-4">
        <div class="rounded-md bg-gray-50 p-2 text-xs text-gray-500 dark:bg-gray-900/60">
          <span class="font-mono break-all">{{ resource }}</span>
        </div>
        <UFormField :label="t('distribution.cluster')" required>
          <USelectMenu
            v-model="clusterId"
            value-key="value"
            :items="clusters.map(cluster => ({
              label: t('distribution.cluster_item', { name: cluster.name, count: cluster.members?.length || 0 }),
              value: cluster.id,
            }))"
            :placeholder="t('distribution.cluster_placeholder')"
            :disabled="loading"
          />
        </UFormField>

        <UFormField v-if="selectedCluster" :label="t('distribution.nodes')" required>
          <div class="space-y-1 rounded-md border border-gray-200 p-2 dark:border-gray-800">
            <label
              v-for="node in clusterNodes"
              :key="node.id"
              class="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 hover:bg-gray-50 dark:hover:bg-gray-800/60"
            >
              <UCheckbox
                :model-value="selectedNodeIds.includes(node.id)"
                :disabled="loading"
                @update:model-value="setNodeSelected(node.id, !!$event)"
              />
              <span class="min-w-0 flex-1 truncate text-sm">{{ node.name }}</span>
              <span class="text-xs text-gray-400">{{ node.ip }}</span>
              <UBadge
                v-if="selectedNodeIds[0] === node.id"
                color="primary"
                variant="subtle"
                size="sm"
              >head</UBadge>
              <UBadge
                v-else-if="selectedNodeIds.includes(node.id)"
                color="neutral"
                variant="subtle"
                size="sm"
              >worker</UBadge>
            </label>
            <div v-if="!clusterNodes.length" class="py-4 text-center text-sm text-gray-400">
              {{ t('distribution.no_nodes') }}
            </div>
          </div>
          <p class="mt-1 text-[11px] text-gray-400">{{ t('distribution.node_order_hint') }}</p>
        </UFormField>

        <UAlert
          v-if="!clusters.length"
          color="warning"
          variant="subtle"
          :title="t('distribution.no_clusters')"
        />
      </div>
    </template>
    <template #footer>
      <div class="flex w-full justify-end gap-2">
        <UButton variant="outline" :disabled="loading" @click="emit('update:open', false)">
          {{ t('common.cancel') }}
        </UButton>
        <UButton
          color="primary"
          :loading="loading"
          :disabled="!clusterId || !selectedNodeIds.length"
          @click="submit"
        >
          {{ t('distribution.confirm') }}
        </UButton>
      </div>
    </template>
  </UModal>
</template>
