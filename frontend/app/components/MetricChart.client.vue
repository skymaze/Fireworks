<script setup lang="ts">
import { use } from 'echarts/core'
import { BarChart, GraphChart, LineChart } from 'echarts/charts'
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'

use([
  LineChart,
  BarChart,
  GraphChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DataZoomComponent,
  CanvasRenderer,
])

const props = withDefaults(
  defineProps<{
    option: Record<string, unknown>
    height?: string
  }>(),
  { height: '280px' },
)
</script>

<template>
  <VChart :option="props.option" autoresize :style="{ height: props.height }" />
</template>
