/** 服务端已对完整累计快照做差分；前端只负责选择窗口和展示聚合结果。 */

export interface InferencePoint {
  ts: number
  task_id: number
  task_name: string | null
  tokens_per_sec: number | null
  prompt_tokens_per_sec: number | null
  requests: number | null
  ttft_ms: number | null
  e2e_ms: number | null
  kv_cache_percent: number | null
}

export interface InferenceMetricsSummary {
  decode_average_tokens_per_sec: number | null
  prefill_average_tokens_per_sec: number | null
  decode_peak_tokens_per_sec: number | null
  decode_peak_at: number | null
  prefill_peak_tokens_per_sec: number | null
  prefill_peak_at: number | null
  request_peak_per_sec: number | null
  ttft_p95_ms: number | null
  kv_cache_peak_percent: number | null
  window_generated_tokens: number
  window_prompt_tokens: number
  window_requests: number
}

export interface InferenceMetricsResponse {
  bucket_seconds: number
  source_intervals: number
  points: InferencePoint[]
  summary: InferenceMetricsSummary
}

export function emptyInferenceMetrics(): InferenceMetricsResponse {
  return {
    bucket_seconds: 0,
    source_intervals: 0,
    points: [],
    summary: {
      decode_average_tokens_per_sec: null,
      prefill_average_tokens_per_sec: null,
      decode_peak_tokens_per_sec: null,
      decode_peak_at: null,
      prefill_peak_tokens_per_sec: null,
      prefill_peak_at: null,
      request_peak_per_sec: null,
      ttft_p95_ms: null,
      kv_cache_peak_percent: null,
      window_generated_tokens: 0,
      window_prompt_tokens: 0,
      window_requests: 0,
    },
  }
}

export async function fetchInferenceMetrics(
  api: { get: (path: string, params?: Record<string, any>) => Promise<any> },
  opts: { from: number; to?: number; taskId?: number; maxPoints?: number },
): Promise<InferenceMetricsResponse> {
  const params: Record<string, any> = {
    from_ts: opts.from,
    max_points: opts.maxPoints ?? 360,
  }
  if (opts.to !== undefined) params.to_ts = opts.to
  if (opts.taskId !== undefined) params.task_id = opts.taskId
  return (await api.get('/inference/metrics', params)) as InferenceMetricsResponse
}
