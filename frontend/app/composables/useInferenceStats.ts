/**
 * 推理统计（浏览器侧差分）：前端拉取 /api/inference/samples 的原始累计快照，
 * 按任务聚类、对相邻样本做差分，得出 tok/s / 生成/提示/请求增量 / TTFT/E2E，
 * 并汇总窗口统计（当前/平均/峰值/抢占/合计）。与后端不再共享派生逻辑。
 */

export interface HistRaw {
  /** 直方图累计：sum=累计总和(秒)、count=累计样本数、buckets=[[上界秒, 累计计数], …] 升序 */
  sum?: number
  count?: number
  buckets?: [number, number][]
}

export interface RawInferenceSample {
  ts: number
  task_id: number
  task_name: string | null
  node_id: number
  model_name: string | null
  data: {
    backend?: string | null
    generation_tokens_total?: number | null
    prompt_tokens_total?: number | null
    num_preemptions_total?: number | null
    request_success_total?: number | null
    kv_cache_percent?: number | null
    ttft?: HistRaw | null
    e2e?: HistRaw | null
  }
}

/** 相邻样本差分后的派生点（同一时间轴，一个样本对应一个点） */
export interface InferencePoint {
  ts: number
  task_id: number
  task_name: string | null
  model_name: string | null
  backend: string
  tokens_per_sec: number | null
  output_tokens: number | null
  prompt_tokens: number | null
  requests: number | null
  preemptions: number | null
  ttft_ms: number | null
  e2e_ms: number | null
  kv_cache_percent: number | null
}

export interface InferenceWindowStats {
  monitoredTasks: number
  sampleCount: number
  freshnessSeconds: number
  currentTokensPerSec: number | null
  averageTokensPerSec: number | null
  peakTokensPerSec: number | null
  peakAt: number | null
  ttftP95Ms: number | null
  kvCachePercent: number | null
  preemptions: number
  windowGeneratedTokens: number
  windowRequests: number
}

function toNum(v: unknown, fallback: number | null = null): number | null {
  const n = Number(v)
  return Number.isFinite(n) ? n : fallback
}

function round1(v: number): number {
  return Math.round(v * 10) / 10
}

/** 计数器差分：prev/cur 任一缺失或回退（vLLM 重启）返回 null */
function counterDelta(prev: unknown, cur: unknown): number | null {
  const a = toNum(prev)
  const b = toNum(cur)
  if (a === null || b === null || b < a) return null
  return b - a
}

// ---------- 直方图分位（累计桶差分 + 桶内线性插值），与 agent 原实现同一算法 ----------

interface HistDistribution {
  bounds: number[]
  deltaCum: number[]
  total: number
}

function histDistribution(prev: HistRaw | null, cur: HistRaw | null): HistDistribution | null {
  const pb = prev?.buckets ?? []
  const cb = cur?.buckets ?? []
  if (!pb.length || !cb.length || pb.length !== cb.length) return null
  const total = toNum(cur?.count) - toNum(prev?.count, 0)
  if (total === null || total <= 0) return null
  const bounds: number[] = []
  const deltaCum: number[] = []
  for (let i = 0; i < cb.length; i++) {
    const d = toNum(cb[i][1]) - toNum(pb[i][1], 0)
    if (d === null || d < 0) return null // 引擎重启计数清零：放弃本对
    // +Inf 桶上界在 agent 侧归一化为 null，这里视为无穷（末桶）
    const bound = cb[i][0] === null || cb[i][0] === undefined ? Infinity : toNum(cb[i][0])
    if (bound === null) return null
    bounds.push(bound)
    deltaCum.push(d)
  }
  return { bounds, deltaCum, total }
}

function quantileFrom(dist: HistDistribution, q: number): number | null {
  const target = q * dist.total
  let acc = 0
  let lower = 0
  for (let i = 0; i < dist.bounds.length; i++) {
    const bucket = dist.deltaCum[i] - (i > 0 ? dist.deltaCum[i - 1] : 0)
    if (acc + bucket >= target) {
      if (bucket <= 0) return Number.isFinite(lower) ? lower : null
      const frac = (target - acc) / bucket
      const value = lower + frac * (dist.bounds[i] - lower)
      if (!Number.isFinite(value)) {
        // 分位落入无上界 +Inf 桶：钳制到最后一个有限上界
        return Number.isFinite(lower) ? lower : null
      }
      return value
    }
    acc += bucket
    lower = dist.bounds[i]
  }
  return null
}

/** 相邻两份直方图差分后取分位（秒）；不可用回退区间均值 */
function histLatency(prev: HistRaw | null, cur: HistRaw | null, q: number): number | null {
  const dist = histDistribution(prev, cur)
  const p = dist ? quantileFrom(dist, q) : null
  if (p !== null) return p
  const dCount = toNum(cur?.count) - toNum(prev?.count, 0)
  const dSum = toNum(cur?.sum) - toNum(prev?.sum, 0)
  if (dCount !== null && dSum !== null && dCount > 0) return dSum / dCount
  return null
}

// ---------- 拉取 / 标注派生点 ----------

export async function fetchRawSamples(
  api: { get: (path: string, params?: Record<string, any>) => Promise<any> },
  opts: { from?: number; to?: number; taskId?: number; limit?: number } = {},
): Promise<RawInferenceSample[]> {
  const params: Record<string, any> = { from_ts: opts.from ?? 0, limit: opts.limit ?? 5000 }
  if (opts.to !== undefined) params.to_ts = opts.to
  if (opts.taskId !== undefined) params.task_id = opts.taskId
  return (await api.get('/inference/samples', params)) as RawInferenceSample[]
}

/** 全局按 ts 排序的原始样本合并去重（支持增量 + 窗口裁剪） */
export function mergeSamples(
  existing: RawInferenceSample[],
  incoming: RawInferenceSample[],
  windowSeconds: number,
): RawInferenceSample[] {
  const key = (r: RawInferenceSample) => `${r.task_id}:${r.node_id}:${r.ts}`
  const map = new Map<string, RawInferenceSample>()
  for (const r of existing) map.set(key(r), r)
  for (const r of incoming) map.set(key(r), r)
  const lastTs = incoming.length ? incoming[incoming.length - 1].ts
    : (existing.length ? existing[existing.length - 1].ts : Date.now() / 1000)
  const cutoff = lastTs - windowSeconds
  return [...map.values()].filter((r) => r.ts >= cutoff).sort((a, b) => a.ts - b.ts)
}

/** 窗内最新的一个样本 ts（增量拉取的 from_ts 锚点；无样本返回 0） */
export function lastSampleTs(samples: RawInferenceSample[]): number {
  return samples.length ? samples[samples.length - 1].ts : 0
}

function baselinePoint(cur: RawInferenceSample): InferencePoint {
  return {
    ts: cur.ts,
    task_id: cur.task_id,
    task_name: cur.task_name ?? null,
    model_name: cur.model_name ?? null,
    backend: cur.data?.backend || 'unknown',
    tokens_per_sec: null,
    output_tokens: null,
    prompt_tokens: null,
    requests: null,
    preemptions: null,
    ttft_ms: null,
    e2e_ms: null,
    kv_cache_percent: toNum(cur.data?.kv_cache_percent),
  }
}

function deriveInterval(prev: RawInferenceSample, cur: RawInferenceSample): InferencePoint {
  const dt = Math.max(cur.ts - prev.ts, 1e-6)
  const pd = prev.data || {}
  const cd = cur.data || {}
  const gen = counterDelta(pd.generation_tokens_total, cd.generation_tokens_total)
  const prompt = counterDelta(pd.prompt_tokens_total, cd.prompt_tokens_total)
  const preempt = counterDelta(pd.num_preemptions_total, cd.num_preemptions_total)
  const req = counterDelta(pd.request_success_total, cd.request_success_total)
  const ttftSec = histLatency(pd.ttft ?? null, cd.ttft ?? null, 0.95)
  const e2eSec = histLatency(pd.e2e ?? null, cd.e2e ?? null, 0.5)
  return {
    ts: cur.ts,
    task_id: cur.task_id,
    task_name: cur.task_name ?? null,
    model_name: cur.model_name ?? null,
    backend: cd.backend || 'unknown',
    tokens_per_sec: gen !== null ? round1(gen / dt) : null,
    output_tokens: gen,
    prompt_tokens: prompt,
    requests: req,
    preemptions: preempt,
    ttft_ms: ttftSec !== null ? round1(ttftSec * 1000) : null,
    e2e_ms: e2eSec !== null ? round1(e2eSec * 1000) : null,
    kv_cache_percent: toNum(cd.kv_cache_percent),
  }
}

/** 对原始样本聚类 + 相邻差分 → 派生点（按 ts 升序；每任务首样本为基线，速率字段为 null） */
export function deriveSeries(raw: RawInferenceSample[]): InferencePoint[] {
  const byTask = new Map<number, RawInferenceSample[]>()
  for (const r of raw) {
    const list = byTask.get(r.task_id) || []
    list.push(r)
    byTask.set(r.task_id, list)
  }
  const points: InferencePoint[] = []
  for (const rows of byTask.values()) {
    // 每任务内部保持 ts 升序（mergeSamples 已全局排序）
    for (let i = 0; i < rows.length; i++) {
      points.push(i === 0 ? baselinePoint(rows[i]) : deriveInterval(rows[i - 1], rows[i]))
    }
  }
  return points.sort((a, b) => a.ts - b.ts)
}

/** 窗口内 TTFT P95：跨所有相邻对累计直方图差分（精确，非"分位之套分位"） */
function windowHistQuantile(raw: RawInferenceSample[], q: number): number | null {
  const byTask = new Map<number, RawInferenceSample[]>()
  for (const r of raw) {
    const list = byTask.get(r.task_id) || []
    list.push(r)
    byTask.set(r.task_id, list)
  }
  let dist: HistDistribution | null = null
  for (const rows of byTask.values()) {
    for (let i = 1; i < rows.length; i++) {
      const d = histDistribution(rows[i - 1].data?.ttft ?? null, rows[i].data?.ttft ?? null)
      if (!d) continue
      if (!dist) {
        dist = d
      } else {
        dist.total += d.total
        for (let j = 0; j < dist.deltaCum.length && j < d.deltaCum.length; j++) {
          dist.deltaCum[j] += d.deltaCum[j]
        }
      }
    }
  }
  return dist ? quantileFrom(dist, q) : null
}

/** 窗口统计（总览卡片用）；points 由 deriveSeries 得到，raw 用于直方图窗口分位 */
export function computeWindowStats(
  raw: RawInferenceSample[],
  windowSeconds: number,
  freshnessSeconds: number,
  points?: InferencePoint[],
): InferenceWindowStats {
  const derived = points ?? deriveSeries(raw)
  const lastByTask = new Map<number, InferencePoint>()
  for (const p of derived) lastByTask.set(p.task_id, p)
  const now = raw.length ? raw[raw.length - 1].ts : Date.now() / 1000
  const currentCut = now - freshnessSeconds
  const currentPoints = [...lastByTask.values()].filter((p) => p.ts >= currentCut)

  let genSum = 0
  let reqSum = 0
  let preemptSum = 0
  let peak: number | null = null
  let peakAt: number | null = null
  for (const p of derived) {
    if (p.output_tokens !== null) genSum += p.output_tokens
    if (p.requests !== null) reqSum += p.requests
    if (p.preemptions !== null) preemptSum += p.preemptions
    if (p.tokens_per_sec !== null && (peak === null || p.tokens_per_sec > peak)) {
      peak = p.tokens_per_sec
      peakAt = p.ts
    }
  }
  const curRates = currentPoints.map((p) => p.tokens_per_sec).filter((v): v is number => v !== null)
  const kvs = currentPoints.map((p) => p.kv_cache_percent).filter((v): v is number => v !== null)
  const ttftSec = windowHistQuantile(raw, 0.95)
  return {
    monitoredTasks: new Set(derived.map((p) => p.task_id)).size,
    sampleCount: derived.length,
    freshnessSeconds,
    currentTokensPerSec: curRates.length ? round1(curRates.reduce((a, b) => a + b, 0)) : null,
    averageTokensPerSec: windowSeconds > 0 ? round1(genSum / windowSeconds) : null,
    peakTokensPerSec: peak,
    peakAt,
    ttftP95Ms: ttftSec !== null ? round1(ttftSec * 1000) : null,
    kvCachePercent: kvs.length ? round1(kvs.reduce((a, b) => a + b, 0) / kvs.length) : null,
    preemptions: Math.round(preemptSum),
    windowGeneratedTokens: Math.round(genSum),
    windowRequests: Math.round(reqSum),
  }
}
