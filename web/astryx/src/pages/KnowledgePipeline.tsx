import { Button, Modal, Segmented, Tag } from 'antd'
import { useMemo, useState } from 'react'
import {
  api, apiQuiet, EP, fmtTime,
  type NeedsReview, type PipelineRun,
} from '../services/api'
import { usePolling } from '../services/usePolling'
import PipelineStepper, { EXCEPTION_STATES, MAIN_STEPS } from '../components/PipelineStepper'
import type { RealPipelineStatus } from '../contracts'

interface Data { runs: PipelineRun[] | null; review: NeedsReview | null }

async function load(): Promise<Data> {
  const [runs, review] = await Promise.all([
    apiQuiet<PipelineRun[]>(EP.runs),
    apiQuiet<NeedsReview>(EP.needsReview),
  ])
  return { runs, review }
}

const SOURCE_LABEL: Record<string, string> = {
  document: '文档', chat: '消息', calendar: '日历',
  minutes: '会议', upload: '本地上传',
}

function tone(status: string): string {
  if (status === 'COMPLETED') return 'success'
  if (EXCEPTION_STATES.some((s) => s.key === status)) return 'error'
  return 'processing'
}

interface Props { active: boolean }

export default function KnowledgePipeline({ active }: Props) {
  const poll = usePolling(load, 10_000, active)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [source, setSource] = useState<string>('all')
  const [confirmRun, setConfirmRun] = useState<PipelineRun | null>(null)
  const [actionMsg, setActionMsg] = useState<string>('')
  const [busy, setBusy] = useState(false)

  const runs = poll.data?.runs ?? []
  const kinds = useMemo(
    () => ['all', ...Array.from(new Set(runs.map((r) => r.source_kind || 'unknown')))],
    [runs],
  )
  const filtered = source === 'all' ? runs : runs.filter((r) => (r.source_kind || 'unknown') === source)
  const selected = runs.find((r) => r.id === selectedId) ?? filtered[0] ?? runs[0] ?? null

  const statusDist = useMemo(() => {
    const m: Record<string, number> = {}
    for (const r of runs) m[r.status] = (m[r.status] ?? 0) + 1
    return Object.entries(m).sort((a, b) => b[1] - a[1])
  }, [runs])

  const heat = useMemo(() => {
    const byKind: Record<string, Record<string, number>> = {}
    for (const r of runs) {
      const k = r.source_kind || 'unknown'
      byKind[k] = byKind[k] ?? {}
      byKind[k][r.status] = (byKind[k][r.status] ?? 0) + 1
    }
    return Object.entries(byKind)
  }, [runs])

  const doReprocess = async () => {
    if (!confirmRun) return
    setBusy(true); setActionMsg('')
    try {
      const r = await api<{ status: string; task_id?: string }>(
        EP.reprocess(confirmRun.document_id), { method: 'POST' },
      )
      setActionMsg(`后端返回：${JSON.stringify(r)}`)
      void poll.refresh()
    } catch (e) {
      // 照实披露：Celery 未运行 / 文档不存在等一律显示真实错误，不假装成功
      setActionMsg(`提交失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false); setConfirmRun(null)
    }
  }

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">知识生产流水线</h1>
        <span className="page-sub">真实 15 状态状态机 · ingest.pipeline_runs · 10s 刷新</span>
        <div className="page-actions">
          <Button size="small" onClick={() => void poll.refresh()} loading={poll.loading}>刷新</Button>
        </div>
      </div>

      <div className="card fade-in">
        <div className="card-head">流水线步骤条
          <span className="sub">点击步骤可下钻状态说明</span>
        </div>
        <PipelineStepper
          currentStatus={selected?.status ?? ''}
          steps={MAIN_STEPS}
          failedReason={selected?.error_info ? JSON.stringify(selected.error_info) : undefined}
          onStepClick={(s: RealPipelineStatus) =>
            setActionMsg(`主路径步骤 ${s}：${MAIN_STEPS.find((x) => x.key === s)?.description ?? ''}`)}
        />
        <div className="step-meta" style={{ marginTop: 2 }}>
          {statusDist.length
            ? statusDist.map(([k, n]) => <span key={k} className="pill">{k} · {n}</span>)
            : <span className="pill">暂无 run 数据</span>}
          <span style={{ marginLeft: 'auto' }}>
            需复核 {poll.data?.review?.count ?? 0} 条
          </span>
        </div>
        <div className="note warn">
          <b>状态机命名差异（照实披露）：</b>方案草案写的是
          <code> DISAMBIGUATING / CLAIM_GENERATED / APPROVED / FACT_PUBLISHED </code>，
          后端真实状态是 <code>ENTITY_RESOLUTION / KNOWLEDGE_REVIEW / PUBLISHING / COMPLETED</code>
          （共 15 态：主路径 10 + 异常 5）。本组件按<b>真实状态</b>渲染，不做虚假映射。
        </div>
      </div>

      <div className="card" style={{ marginTop: 'var(--ax-gap)' }}>
        <div className="card-head">数据源
          <span className="sub">按真实 source_kind 过滤</span>
        </div>
        <Segmented
          value={source}
          onChange={(v) => setSource(String(v))}
          options={kinds.map((k) => ({ label: k === 'all' ? '全部' : (SOURCE_LABEL[k] ?? k), value: k }))}
        />
      </div>

      <div className="ops-grid" style={{ gridTemplateColumns: 'minmax(0, 1.4fr) minmax(0, 1fr)', marginTop: 'var(--ax-gap)' }}>
        <div className="card">
          <div className="card-head">蒸馏任务列表
            <span className="sub">{filtered.length} 条</span>
          </div>
          {poll.loading && !poll.data
            ? <>{[0, 1, 2, 3].map((i) => <div key={i} className="skeleton" />)}</>
            : filtered.length === 0
              ? <div className="empty"><span className="big">▤</span>该数据源暂无蒸馏任务</div>
              : (
                <div className="scroll-y">
                  <table className="tbl">
                    <thead>
                      <tr><th>文档</th><th>状态</th><th>切片</th><th>来源</th><th>更新</th></tr>
                    </thead>
                    <tbody>
                      {filtered.map((r) => (
                        <tr key={r.id} onClick={() => { setSelectedId(r.id); setActionMsg('') }}
                          style={{ cursor: 'pointer',
                            background: selected?.id === r.id ? 'var(--ax-primary-soft)' : undefined }}>
                          <td className="ellip" title={r.document_title}>{r.document_title || r.id}</td>
                          <td><Tag color={tone(r.status)}>{r.status}</Tag></td>
                          <td className="mono">{r.chunks ?? '—'}</td>
                          <td>{SOURCE_LABEL[r.source_kind || ''] ?? r.source_kind ?? '—'}</td>
                          <td className="mono">{fmtTime(r.updated_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
        </div>

        <div className="card">
          <div className="card-head">任务详情
            <span className="sub">GET /documents/runs/{'{id}'}</span>
          </div>
          {!selected
            ? <div className="empty"><span className="big">◇</span>选择左侧任务查看步骤与证据</div>
            : (
              <>
                <div className="kpi-row"><span className="title">状态</span>
                  <span className="num"><Tag color={tone(selected.status)}>{selected.status}</Tag></span></div>
                <div className="kpi-row"><span className="title">重试次数</span><span className="num mono">{selected.retry_count}</span></div>
                <div className="kpi-row"><span className="title">切片 / Claim / Fact</span>
                  <span className="num mono">{selected.chunks ?? '—'} / {selected.claims ?? '—'} / {selected.facts ?? '—'}</span></div>
                <div className="kpi-row"><span className="title">创建</span><span className="num mono" style={{ fontSize: 11 }}>{fmtTime(selected.created_at)}</span></div>
                <div className="kpi-row"><span className="title">run_id</span>
                  <span className="num mono" style={{ fontSize: 10 }}>{selected.id.slice(0, 8)}…</span></div>
                {selected.error_info && Object.keys(selected.error_info).length > 0 && (
                  <div className="note warn mono" style={{ marginTop: 8, wordBreak: 'break-all' }}>
                    error_info: {JSON.stringify(selected.error_info).slice(0, 260)}
                  </div>
                )}
                <div style={{ marginTop: 10, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <Button size="small" danger onClick={() => setConfirmRun(selected)}>
                    重新蒸馏（写操作）
                  </Button>
                  <Button size="small" disabled title="后端未提供 batch-approve 端点">
                    批量转事实（端点不存在）
                  </Button>
                  <Button size="small" disabled title="后端未提供 batch-reject 端点">
                    批量拒绝（端点不存在）
                  </Button>
                </div>
                <div className="note" style={{ marginTop: 8 }}>
                  方案 §3.5 列出的 <code>batch-approve / batch-reject</code> 在后端
                  <b>并不存在</b>，故按钮禁用，不提供会 404 的假操作；
                  可恢复状态（FAILED / NEEDS_REVIEW 等）走已存在的 <code>reprocess</code>。
                </div>
                {actionMsg && <div className="note" style={{ marginTop: 8 }}>{actionMsg}</div>}
              </>
            )}
        </div>
      </div>

      <div className="card" style={{ marginTop: 'var(--ax-gap)' }}>
        <div className="card-head">蒸馏分布热力图
          <span className="sub">按数据源 × 状态 · 真实计数</span>
        </div>
        {heat.length === 0
          ? <div className="empty"><span className="big">▦</span>暂无分布数据</div>
          : heat.map(([kind, dist]) => {
            const total = Object.values(dist).reduce((a, b) => a + b, 0)
            return (
              <div key={kind} style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '7px 0' }}>
                <span style={{ width: 78, fontSize: 11, color: 'var(--ax-text-secondary)' }}>
                  {SOURCE_LABEL[kind] ?? kind}
                </span>
                <div style={{ flex: 1, display: 'flex', height: 14, borderRadius: 4, overflow: 'hidden', background: 'var(--ax-surface-2)' }}>
                  {Object.entries(dist).map(([st, n]) => (
                    <div key={st} title={`${st}: ${n}`}
                      style={{ width: `${(n / total) * 100}%`,
                        background: st === 'COMPLETED' ? 'var(--ax-success)'
                          : EXCEPTION_STATES.some((x) => x.key === st) ? 'var(--ax-danger)'
                            : 'var(--ax-primary)',
                        opacity: 0.85 }} />
                  ))}
                </div>
                <span className="mono" style={{ width: 34, textAlign: 'right' }}>{total}</span>
              </div>
            )
          })}
      </div>

      <Modal
        title="确认重新蒸馏（真实写操作）"
        open={!!confirmRun}
        onOk={() => void doReprocess()}
        onCancel={() => setConfirmRun(null)}
        okText="确认提交" cancelText="取消" okButtonProps={{ danger: true, loading: busy }}
      >
        <p>将向后端提交 <code>POST /documents/{'{document_id}'}/reprocess</code>，
          真实修改 <b>ingest.pipeline_runs</b> 状态。</p>
        <p style={{ color: 'var(--ax-text-secondary)', fontSize: 11.5 }}>
          若 Celery worker 未运行，后端会返回真实错误，前端不会假装成功。
        </p>
      </Modal>
    </>
  )
}
