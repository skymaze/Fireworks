import assert from 'node:assert/strict'
import test from 'node:test'

import {
  emptyInferenceMetrics,
  fetchInferenceMetrics,
} from '../app/composables/useInferenceStats.ts'

test('empty inference metrics has a stable complete shape', () => {
  const metrics = emptyInferenceMetrics()
  assert.deepEqual(metrics.points, [])
  assert.equal(metrics.summary.window_generated_tokens, 0)
  assert.equal(metrics.summary.decode_peak_tokens_per_sec, null)
  assert.equal(metrics.summary.kv_cache_peak_percent, null)
})

test('metrics query sends a window and chart resolution, not a row limit', async () => {
  let request: { path: string; params?: Record<string, any> } | null = null
  const expected = emptyInferenceMetrics()
  const api = {
    async get(path: string, params?: Record<string, any>) {
      request = { path, params }
      return expected
    },
  }

  const result = await fetchInferenceMetrics(api, {
    from: 100,
    to: 200,
    taskId: 7,
    maxPoints: 288,
  })

  assert.equal(result, expected)
  assert.deepEqual(request, {
    path: '/inference/metrics',
    params: { from_ts: 100, to_ts: 200, task_id: 7, max_points: 288 },
  })
  assert.equal('limit' in request!.params!, false)
})
