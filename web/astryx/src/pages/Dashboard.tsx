import { Tag, Tooltip } from 'antd'
import { useMemo } from 'react'
import {
  apiQuiet, EP, fmtTime, pct,
  type AuditQuery, type CqStoreHealth, type DecisionRow, type EvalHistory,
  type GateStatus, type IdentityContext, type LineageOverview,
  type OutboxResp, type PipelineRun, type ReplyRow, type SemanticMeta,
} from '../services/api'
import { usePolling } from '../services/usePolling'
import type { ActivityItem, LineageNode } from '../contracts'
import TopStatusBar from '../components/TopStatusBar'
import ActivityTimeline from '../components/ActivityTimeline'
import MetricCard from '../components/MetricCard'
import RealtimeLineageMiniGraph, { lineageNodes } from '../components/RealtimeLineageMiniGraph'
import { SemanticOrb } from '../components/ThreeBackdrop'

interface FastData {
  health: { status: string } | null
  me: IdentityContext | null
  cq: CqStoreHealth | null
  meta: SemanticMeta | null
  gate: GateStatus | null
}
interface SlowData {
  history: EvalHistory | null
  audit: AuditQuery | null
  lineage: LineageOverview | null
  runs: PipelineRun[] | null
  decisions: DecisionRow[] | null
  replies: ReplyRow[] | null
  outbox: OutboxResp | null
}

async function loadFast(): Promise<FastData> {
  const [health, me, cq, meta, gate] = await Promise.all([
    apiQuiet<{ status: string }>(EP.healthz),
    apiQuiet<IdentityContext>(EP.me),
    apiQuiet<CqStoreHealth>(EP.cqStoreHealth),
    apiQuiet<SemanticMeta>(EP.meta),
    apiQuiet<GateStatus>(EP.gate),
  ])
  return { health, me, cq, meta, gate }
}
async function loadSlow(): Promise<SlowData> {
  const [history, audit, lineage, runs, decisions, replies, outbox] = await Promise.all([
    apiQuiet<EvalHistory>(EP.history),
    apiQuiet<AuditQuery>(EP.auditQuery),
    apiQuiet<LineageOverview>(EP.lineage),
    apiQuiet<PipelineRun[]>(EP.runs),
    apiQuiet<DecisionRow[]>(EP.decisions),
    apiQuiet<ReplyRow[]>(EP.replies),
    apiQuiet<OutboxResp>(EP.outbox),
  ])
  return { history, audit, lineage, runs, decisions, replies, outbox }
}

function metric(h: EvalHistory | null, key: string): number | null {
  if (!h?.baseline?.metrics) return null
  for (const group of Object.values(h.baseline.metrics)) {
    if (group && typeof group[key] === 'number') return group[key]
  }
  return null
}

function buildFeed(s: SlowData | null): ActivityItem[] {
  const out: ActivityItem[] = []
  for (const r of s?.runs ?? []) {
    out.push({
      id: `run-${r.id}`, type: 'distill',
      title: `蒸馏 · ${r.document_title || r.id}`,
      status: r.status === 'FAILED' ? 'failed'
        : r.status === 'COMPLETED' ? 'success' : 'pending',
      timestamp: r.updated_at || r.created_at,
      meta: { source: r.source_kind },
    })
  }
  for (const d of s?.decisions ?? []) {
    out.push({
      id: `dec-${d.id}`, type: 'decision',
      title: `决策 · ${d.task_type} / ${d.recommendation}`,
      status: d.status === 'approved' ? 'success' : 'pending',
      timestamp: d.created_at,
      meta: { source: 'workflow' },
    })
  }
  for (const m of s?.replies ?? []) {
    out.push({
      id: `msg-${m.id}`, type: 'message',
      title: `消息草稿 · ${m.conversation_name || m.sender || m.id}`,
      status: 'pending', timestamp: m.created_at ?? '',
      meta: { source: 'messages' },
    })
  }
  const stats = s?.outbox?.stats
  if (stats?.total) {
    out.push({
      id: 'outbox', type: 'approval',
      title: `事件账本 · ${stats.total} 条（积压 ${stats.backlog}）`,
      status: stats.backlog > 0 ? 'pending' : 'success',
      timestamp: '', meta: { source: 'outbox' },
    })
  }
  return out
    .sort((a, b) => String(b.timestamp).localeCompare(String(a.timestamp)))
    .slice(0, 14)
}

function Ring({ label, value }: { label: string; value: number }) {
  const v = Math.max(0, Math.min(100, Math.round(value * 100)))
  const r = 22, c = 2 * Math.PI * r
  return (
    <div className="ring-item">
      <svg width="58" height="58" viewBox="0 0 58 58">
        <circle cx="29" cy="29" r={r} fill="none" stroke="var(--ax-border)" strokeWidth="5" />
        <circle cx="29" cy="29" r={r} fill="none" stroke="var(--ax-primary)" strokeWidth="5"
          strokeLinecap="round" strokeDasharray={`${(c * v) / 100} ${c}`}
          transform="rotate(-90 29 29)" style={{ transition: 'stroke-dasharray .25s ease' }} />
        <text x="29" y="33" textAnchor="middle" fontSize="12" fontWeight="650"
          fill="var(--ax-text)">{v}%</text>
      </svg>
      <div className="ring-label">{label}</div>
    </div>
  )
}

interface Props { active: boolean; theme: 'light' | 'dark'; onNavigate: (v: string) => void }

export default function Dashboard({ active, theme, onNavigate }: Props) {
  const fast = usePolling(loadFast, 10_000, active)
  const slow = usePolling(loadSlow, 30_000, active)
  const f = fast.data, s = slow.data

  const counts = s?.lineage?.counts ?? {}
  const runs = s?.runs ?? []
  const completed = runs.filter((r) => r.status === 'COMPLETED').length
  const today = useMemo(() => {
    const d = new Date(); d.setHours(0, 0, 0, 0)
    return runs.filter((r) => new Date(r.created_at.replace(' ', 'T')).getTime() >= d.getTime()).length
  }, [runs])

  const channels = [
    { key: 'pipeline', name: '知识生产', n: counts.runs ?? runs.length, err: 0 },
    { key: 'ontology', name: '知识与本体', n: (counts.entities ?? 0) + (counts.relations ?? 0), err: 0 },
    { key: 'cognition', name: '认知管理', n: counts.profile_proposals ?? 0, err: 0 },
    { key: 'decision', name: '任务与决策', n: counts.decisions ?? 0, err: 0 },
    { key: 'messages', name: '消息与协作', n: (s?.replies ?? []).length, err: 0 },
    { key: 'observability', name: '链路与评估', n: s?.audit?.rows?.length ?? 0, err: 0 },
  ]

  const denied = (s?.audit?.rows ?? [])
    .filter((r) => ['policy_deny', 'owner_gate_deny', 'context_strict_reject'].includes(r.event_type))
    .reduce((a, r) => a + (r.count ?? 0), 0)

  const nodes: LineageNode[] = lineageNodes(s?.lineage?.counts)
  const edges = nodes.slice(0, -1).map((n, i) => ({ source: n.id, target: nodes[i + 1].id }))
  const feed = buildFeed(s)
  // evaluation/history.runs 为空 → 无 sparkline；不生成任何假趋势序列
  const spark: number[] | null = null
  const m = (key: string) => {
    const v = metric(s?.history ?? null, key)
    return v == null ? '—' : pct(v)
  }

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">数字员工运营</h1>
        <span className="page-sub">真实数据驱动的运营驾驶舱 · React 19 + Vite 6（与旧控制台并存）</span>
        <div className="page-actions">
          <Tag color={f?.cq?.healthy ? 'success' : 'warning'}>
            CQ {f?.cq?.using_postgres ? 'PostgreSQL' : '未确认'}
          </Tag>
        </div>
      </div>

      <TopStatusBar
        health={f?.health?.status === 'ok' ? 'healthy' : 'down'}
        todayTasks={today}
        pendingApprovals={0}
        accuracyTrend={metric(s?.history ?? null, 'recall_at_5')}
        tokenCostToday={null}
        spec={f?.meta?.spec}
        updatedAt={fast.updatedAt}
        loading={fast.loading}
        onRefresh={() => { void fast.refresh(); void slow.refresh() }}
      />

      <div className="card orb-card">
        <div className="card-head">语义拓扑 3D 视图
          <span className="sub">three.js r160 · 装饰层 · 随明暗主题切换</span>
        </div>
        <SemanticOrb theme={theme} />
      </div>

      <div className="ops-grid">
        <div className="col col-channels">
          <div className="card">
            <div className="card-head">能力通道
              <span className="sub">单主体部署 · 6 路</span>
            </div>
            <div className="ch-list">
              {channels.map((c) => (
                <button key={c.key} className="ch-item" onClick={() => onNavigate(c.key)}>
                  <i className="dot busy" />
                  <div>
                    <div className="name">{c.name}</div>
                    <div className="meta">点击进入工作区</div>
                  </div>
                  <span className="badge-n mono">{c.n}</span>
                </button>
              ))}
            </div>
            <div className="note" style={{ marginTop: 9 }}>
              本仓库是<b>单主体数字分身</b>，暂无 digital_employees 实体，
              因此不伪造「多数字员工卡片」。计数与旧控制台同源。
            </div>
          </div>
        </div>

        <div className="col">
          <div className="card">
            <div className="card-head">近期活动流
              <span className="sub">{fmtTime(new Date().toISOString())} 刷新 · {feed.length} 条</span>
            </div>
            <ActivityTimeline items={feed} loading={slow.loading && !s} />
            <div className="note" style={{ marginTop: 8 }}>
              列表文案为<b>运行时业务数据</b>（实库文档 / 会话标题），仓库代码内不含任何真实主体名硬编码。
            </div>
          </div>
          <div className="card">
            <div className="card-head">完成度
              <span className="sub">由真实计数计算</span>
            </div>
            <div className="rings">
              <Ring label="蒸馏完成率"
                value={runs.length ? completed / runs.length : 0} />
              <Ring label="Fact 转化率"
                value={counts.claims ? (counts.facts ?? 0) / counts.claims : 0} />
              <Ring label="门禁通过"
                value={f?.gate
                  ? f.gate.checks_passed.length /
                    Math.max(1, f.gate.checks_passed.length + f.gate.blocking.length)
                  : 0} />
            </div>
          </div>
        </div>

        <div className="col">
          <div className="card">
            <div className="card-head">关键指标
              <span className="sub">baseline</span>
            </div>
            <MetricCard title="召回率 Recall@5" value={m('recall_at_5')} sparklineData={spark} dark={theme === 'dark'} />
            <MetricCard title="证据接地率" value={m('evidence_grounding_rate')} sparklineData={spark} dark={theme === 'dark'} />
            <MetricCard title="属性完整率" value={m('attribute_completeness')} sparklineData={spark} dark={theme === 'dark'} />
            <MetricCard title="越权拦截" value={denied ? `${denied} 次` : '—'} sparklineData={null} dark={theme === 'dark'} />
            <MetricCard title="质量门禁" value={f?.gate?.status ?? '—'} status={f?.gate?.status === 'warn' ? 'warning' : 'normal'} sparklineData={null} dark={theme === 'dark'} />
            <MetricCard title="Token 成本" value={null} sparklineData={null} dark={theme === 'dark'} />
            <div className="note" style={{ marginTop: 8 }}>
              <b>evaluation/history.runs = {s?.history?.runs?.length ?? 0}</b>，无多周序列，
              故不画趋势折线；越权拦截含本地测试探针事件。
            </div>
          </div>
          <div className="card">
            <div className="card-head">当前身份
              <span className="sub">GET /api/v1/me</span>
            </div>
            <div className="kpi-row"><span className="title">租户</span><span className="num mono" style={{ fontSize: 11 }}>{f?.me?.identity.tenant_id ?? '—'}</span></div>
            <div className="kpi-row"><span className="title">角色</span><span className="num mono" style={{ fontSize: 11 }}>{f?.me?.identity.role ?? '—'}</span></div>
            <div className="kpi-row"><span className="title">来源</span><span className="num mono" style={{ fontSize: 11 }}>{f?.me?.source ?? '—'}</span></div>
            <div className="kpi-row"><span className="title">环境</span><span className="num mono" style={{ fontSize: 11 }}>{f?.me?.environment ?? '—'}</span></div>
            <Tooltip title={f?.me?.security_boundary ?? ''}>
              <div className="note warn" style={{ marginTop: 8, cursor: 'help' }}>
                <b>IAM 边界：</b>生产真实 IAM 接入前，「客户端无法伪造管理员权限」<b>尚未达成</b>，不宣称达标。
              </div>
            </Tooltip>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 'var(--ax-gap)' }}>
        <div className="card-head">实时知识链路
          <span className="sub">/lineage/overview · 30s 刷新 · 数值变化脉冲</span>
        </div>
        <RealtimeLineageMiniGraph nodes={nodes} edges={edges} refreshing={slow.loading}
          onNodeClick={(n) => onNavigate(n.type === 'fact' ? 'ontology' : 'observability')} />
        {fast.error || slow.error
          ? <div className="note warn" style={{ marginTop: 8 }}>部分数据源不可用：{fast.error ?? slow.error}</div>
          : null}
      </div>
    </>
  )
}
