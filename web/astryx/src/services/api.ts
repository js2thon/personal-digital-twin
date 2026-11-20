/** V1.3 换栈：统一 API 层。
 *  所有数据来自既有 FastAPI(8101)，经 Vite 代理同源访问；不含任何 mock / 编造数字。
 *  六项语义上下文由后端 AC-01 全局中间件注入，前端不伪造 tenant / 权限。
 */
export const API = '/api/v1'

export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail)
  }
}

export interface IdentityContext {
  identity: {
    tenant_id: string
    user_id: string
    actor: string
    role: string
    scope: string[]
    data_classification: string
    is_owner_class: boolean
  }
  context: {
    request_id: string
    trace_id: string
    schema_version: string
    context_source: string
    derived_fields: string[]
    strict: boolean
  }
  source: string
  writable: boolean
  request_body_trusted: boolean
  note: string
  security_boundary: string
  environment: string
}

export interface CqStoreHealth {
  service: string
  requested_mode: string
  environment: string
  memory_store_allowed: boolean
  authoritative_table: string
  healthy: boolean
  actual_store?: string | null
  degraded_reason?: string | null
  using_postgres?: boolean
  db_ok?: boolean
  hydrated?: boolean
  persistence?: Record<string, unknown>
  startup_gate?: Record<string, unknown>
}

export interface GateStatus {
  status: string
  scope: string
  blocking: unknown[]
  warnings: string[]
  passed_count: number
  checks_passed: string[]
  release_blocking: boolean
  run_id: string
  versions?: Record<string, string>
}

export interface EvalHistory {
  runs: unknown[]
  baseline: {
    created_at: string
    dataset_version: string
    metrics: Record<string, Record<string, number>>
  }
}

export interface AuditRow {
  id: number
  ts: string
  event_type: string
  rule: string
  severity: string
  path?: string
  status_code?: number
  count: number
  by_rule?: Record<string, number>
}
export interface AuditQuery { rows: AuditRow[]; count: number; source: string }

export interface LineageOverview {
  counts: Record<string, number>
  by_source: { source_kind: string; n: number }[]
  flow?: unknown
}

export interface PipelineRun {
  id: string
  document_id: string
  document_title: string
  status: string
  retry_count: number
  created_at: string
  updated_at: string
  error_info?: Record<string, unknown>
  source_kind?: string
  origin_url?: string
  chunks?: number
  claims?: number
  facts?: number
}
export interface NeedsReview {
  count: number
  by_reason: Record<string, number>
  labels: Record<string, string>
  runs: PipelineRun[]
}

export interface DecisionRow {
  id: string
  task_type: string
  recommendation: string
  risk_level: string
  status: string
  confidence: number
  created_at: string
  evidence_count: number
  approval_count: number
  snapshot_count: number
}
export interface ReplyRow {
  id: string
  conversation_name?: string
  sender?: string
  status?: string
  created_at?: string
  draft_text?: string
}
export interface OutboxResp {
  stats: {
    total: number
    by_status: Record<string, number>
    backlog: number
    by_type_24h: { event_type: string; n: number }[]
  }
  events: unknown[]
}

export interface SemanticMeta {
  spec: string
  query_id?: string
  security_audit?: { durable?: boolean; db_rows?: number; middleware?: string }
  [key: string]: unknown
}

export const EP = {
  healthz: '/healthz',
  me: `${API}/me`,
  cqStoreHealth: `${API}/health/cq-store`,
  meta: `${API}/semantic/meta`,
  gate: `${API}/evaluation/gate`,
  history: `${API}/evaluation/history`,
  auditQuery: `${API}/security/audit/query?limit=100`,
  lineage: `${API}/lineage/overview`,
  runs: `${API}/documents/runs/recent?limit=30`,
  needsReview: `${API}/documents/runs/needs-review?limit=20`,
  runDetail: (id: string) => `${API}/documents/runs/${id}`,
  decisions: `${API}/workflow/decisions?limit=20`,
  replies: `${API}/messages/replies?limit=20`,
  outbox: `${API}/projection/outbox?limit=20`,
  reprocess: (docId: string) => `${API}/documents/${docId}/reprocess`,
  // 旧零构建控制台（其余工作区继续由它承载，不重复造轮子）
  legacy: '/',
}

export async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.method && init.method !== 'GET'
        ? { 'Content-Type': 'application/json' }
        : {}),
      ...(init?.headers ?? {}),
    },
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      detail = typeof body?.detail === 'string'
        ? body.detail : JSON.stringify(body?.detail ?? body)
    } catch { /* 保留状态码文案 */ }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as T
}

/** 单个数据源失败不影响整页：返回 null 由 UI 显示「无数据源」。 */
export async function apiQuiet<T>(url: string): Promise<T | null> {
  try {
    return await api<T>(url)
  } catch {
    return null
  }
}

/** SSE 预留：后端当前未开放流式端点，明确关闭并要求调用方回退轮询，
 *  绝不创建一个连不上的 EventSource 假装实时。 */
export const SSE_AVAILABLE = false
export function onStream(_path: string, _handler: (data: unknown) => void): () => void {
  return () => {}
}

export function fmtTime(value?: string): string {
  if (!value) return '—'
  const s = String(value).replace('T', ' ').replace(/\.\d+.*$/, '')
  return s.length >= 16 ? s.slice(5, 16) : s
}
export function pct(v: number | undefined): string {
  return v == null ? '—' : `${(v * 100).toFixed(1)}%`
}
