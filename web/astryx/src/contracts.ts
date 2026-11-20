/** V1.3 落地包 §2.2 / §3.2「组件 Props 设计」的 TypeScript 契约落地。
 *
 * 说明（照实披露）：
 * - 单主体仓库暂无 digital_employees 实体，DigitalEmployeeCardProps 作为未来
 *   多 DE 演进契约保留，当前首页用「能力通道卡」承载，不伪造多 DE 数据。
 * - 方案 §3.2 的 PipelineStatus 列出 DISAMBIGUATING / CLAIM_GENERATED /
 *   APPROVED / FACT_PUBLISHED 等状态，与后端 ingest.pipeline_runs 的真实状态机
 *   不一致；真实状态见 REAL_PIPELINE_STATES，组件按真实状态实现。
 * - accuracyTrend / tokenCostToday / sparklineData 在无历史与无计费埋点时
 *   允许为空，UI 必须显示「无数据源」，不得填充示例数字。
 */

export interface TopStatusBarProps {
  health: 'healthy' | 'degraded' | 'down'
  todayTasks: number
  pendingApprovals: number
  accuracyTrend: number | null
  tokenCostToday: number | null
  onRefresh?: () => void
}

export interface DigitalEmployeeCardProps {
  id: string
  name: string
  roleName: string
  status: 'online' | 'busy' | 'paused' | 'error'
  todayCompleted: number
  todayFailed: number
  lastActiveAt: string
  onClick: (id: string) => void
}

export interface ActivityItem {
  id: string
  type: 'qa' | 'reply' | 'decision' | 'distill' | 'message' | 'approval'
  title: string
  digitalEmployeeName?: string
  status: 'success' | 'pending' | 'failed'
  timestamp: string
  meta?: Record<string, unknown>
}

export interface ActivityTimelineProps {
  items: ActivityItem[]
  loading?: boolean
  onItemClick?: (item: ActivityItem) => void
  hasMore?: boolean
  onLoadMore?: () => void
}

export interface MetricCardProps {
  title: string
  value: number | string | null  // null 时组件显示「无数据源」，不填示例数字
  unit?: string
  trend?: number
  sparklineData?: number[] | null
  status?: 'normal' | 'warning' | 'danger'
  onClick?: () => void
}

export interface LineageNode {
  id: string
  label: string
  count: number
  type: 'document' | 'chunk' | 'claim' | 'fact' | 'decision'
}

export interface RealtimeLineageMiniGraphProps {
  nodes: LineageNode[]
  edges: { source: string; target: string }[]
  onNodeClick?: (node: LineageNode) => void
  refreshing?: boolean
}

/** 方案 §3.2 原文状态（保留作对照，不用于实际渲染）。 */
export type PlannedPipelineStatus =
  | 'PENDING' | 'PARSING' | 'CHUNKING' | 'EXTRACTING' | 'DISAMBIGUATING'
  | 'CLAIM_GENERATED' | 'NEEDS_REVIEW' | 'APPROVED' | 'FACT_PUBLISHED'
  | 'COMPLETED' | 'RETRY_WAIT' | 'FAILED' | 'REVIEW_DISMISSED' | 'CANCELLED'

/** 后端真实状态机（app/workers/pipeline_tasks.py + dws_source/distill_status.py）。 */
export const REAL_PIPELINE_STATES = [
  'PENDING', 'INGESTING', 'PARSING', 'CHUNKING', 'EXTRACTING',
  'VALIDATING', 'ENTITY_RESOLUTION', 'KNOWLEDGE_REVIEW', 'PUBLISHING',
  'COMPLETED', 'RETRY_WAIT', 'FAILED', 'NEEDS_REVIEW',
  'REVIEW_DISMISSED', 'CANCELLED',
] as const
export type RealPipelineStatus = typeof REAL_PIPELINE_STATES[number]

export interface PipelineStep {
  key: RealPipelineStatus
  label: string
  description?: string
}

export interface PipelineStepperProps {
  currentStatus: RealPipelineStatus | string
  steps: PipelineStep[]
  progress?: number
  failedReason?: string
  onStepClick?: (status: RealPipelineStatus) => void
  size?: 'small' | 'default'
}
