import { Tooltip } from 'antd'
import type {
  PipelineStep, PipelineStepperProps, RealPipelineStatus,
} from '../contracts'

/** 后端真实主路径（ingest.pipeline_runs），与方案草案的 14 状态命名不同。 */
export const MAIN_STEPS: PipelineStep[] = [
  { key: 'PENDING', label: '待处理', description: '任务已入队' },
  { key: 'INGESTING', label: '接入', description: '数据源接入' },
  { key: 'PARSING', label: '解析', description: '文档解析' },
  { key: 'CHUNKING', label: '切片', description: '生成语义切片' },
  { key: 'EXTRACTING', label: '抽取', description: 'LLM 抽取 Claim' },
  { key: 'VALIDATING', label: '校验', description: '主张校验' },
  { key: 'ENTITY_RESOLUTION', label: '实体消歧', description: '实体对齐' },
  { key: 'KNOWLEDGE_REVIEW', label: '知识复核', description: '知识层复核' },
  { key: 'PUBLISHING', label: '发布', description: '发布 Fact' },
  { key: 'COMPLETED', label: '完成', description: '流水线终态' },
]

/** 异常 / 人工复核分支，单独处理，不塞进主路径。 */
export const EXCEPTION_STATES: { key: RealPipelineStatus; label: string; tone: string }[] = [
  { key: 'RETRY_WAIT', label: '重试等待', tone: 'warn' },
  { key: 'FAILED', label: '失败', tone: 'danger' },
  { key: 'NEEDS_REVIEW', label: '需人工复核', tone: 'warn' },
  { key: 'REVIEW_DISMISSED', label: '复核驳回', tone: 'danger' },
  { key: 'CANCELLED', label: '已取消', tone: 'warn' },
]

const MAIN_INDEX: Record<string, number> = Object.fromEntries(
  MAIN_STEPS.map((s, i) => [s.key, i]),
)

export function isException(status: string): boolean {
  return EXCEPTION_STATES.some((s) => s.key === status)
}

/** 方案 PipelineStepperProps 契约（currentStatus / steps / progress /
 *  failedReason / onStepClick / size）。当前步骤呼吸动画、已完成实心、
 *  异常态红/黄，未到达灰空心；尊重 prefers-reduced-motion 由全局 CSS 处理。 */
export default function PipelineStepper(p: PipelineStepperProps) {
  const steps = p.steps.length ? p.steps : MAIN_STEPS
  const current = String(p.currentStatus || '').toUpperCase()
  const idx = MAIN_INDEX[current]
  const exception = isException(current)
  const progress = p.progress != null
    ? Math.max(0, Math.min(100, p.progress))
    : idx >= 0 ? Math.round((idx / Math.max(1, steps.length - 1)) * 100) : 0

  return (
    <div>
      <div className="stepper">
        {steps.map((s, i) => {
          const done = idx >= 0 && i < idx
          const active = idx >= 0 && i === idx
          const failed = exception && i === Math.max(0, idx)
          const cls = ['step', done ? 'done' : '', active ? 'current' : '',
            failed ? 'failed' : ''].filter(Boolean).join(' ')
          return (
            <Tooltip key={s.key} title={s.description ?? s.key}>
              <button className={cls} onClick={() => p.onStepClick?.(s.key)}>
                <span className="idx">{i + 1}</span>
                <span className="lbl">{s.label}</span>
              </button>
            </Tooltip>
          )
        })}
      </div>
      <div className="step-meta">
        <span>当前状态：<b className="mono">{current || '—'}</b></span>
        <span className="pill">主路径进度 {progress}%</span>
        {exception && (
          <span className={`pill ${EXCEPTION_STATES.find((s) => s.key === current)?.tone ?? 'warn'}`}>
            异常分支 · {EXCEPTION_STATES.find((s) => s.key === current)?.label ?? current}
          </span>
        )}
        {p.failedReason && <span className="pill danger" title={p.failedReason}>失败原因可查看</span>}
        <span style={{ marginLeft: 'auto', color: 'var(--ax-text-tertiary)' }}>
          真实状态机 · ingest.pipeline_runs
        </span>
      </div>
    </div>
  )
}
