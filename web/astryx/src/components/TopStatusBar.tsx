import { Button, Tag, Tooltip } from 'antd'
import type { TopStatusBarProps as _Contract } from '../contracts'

/** 方案 Props 契约：health / todayTasks / pendingApprovals / accuracyTrend /
 *  tokenCostToday / onRefresh。本实现额外接真实 spec 与更新时间。
 *  accuracyTrend 与 tokenCostToday 允许 null —— 后端无数据源时显示「无数据源」，
 *  绝不填示例数字。 */
export interface TopStatusBarProps extends _Contract {
  spec?: string
  updatedAt?: number
  loading?: boolean
}

export default function TopStatusBar(p: TopStatusBarProps) {
  const healthLabel = p.health === 'healthy' ? '服务在线'
    : p.health === 'degraded' ? '部分降级' : '服务不可用'
  const healthCls = p.health === 'healthy' ? 'ok'
    : p.health === 'degraded' ? 'warn' : 'danger'
  const dotCls = p.health === 'healthy' ? 'ok'
    : p.health === 'degraded' ? 'warn' : 'danger'

  return (
    <div className="statusbar fade-in">
      <div className={`status-cell ${healthCls}`}>
        <span className="label">服务健康</span>
        <span className="value" style={{ fontSize: 15 }}>
          <i className={`dot ${dotCls}`} />{healthLabel}
        </span>
        <span className="hint mono">{p.spec ?? '—'}</span>
      </div>
      <div className="status-cell">
        <span className="label">今日任务</span>
        <span className="value">{p.todayTasks}</span>
        <span className="hint">按真实时间戳统计</span>
      </div>
      <div className="status-cell">
        <span className="label">待审批</span>
        <span className="value">{p.pendingApprovals}</span>
        <span className="hint">/workflow/decisions</span>
      </div>
      <div className="status-cell">
        <span className="label">准确率趋势</span>
        <span className="value">
          {p.accuracyTrend == null
            ? <Tag color="default">无历史</Tag>
            : `${(p.accuracyTrend * 100).toFixed(1)}%`}
        </span>
        <span className="hint">
          {p.accuracyTrend == null ? 'evaluation/history.runs = 0，不画假折线' : '基线值'}
        </span>
      </div>
      <div className="status-cell">
        <span className="label">Token 成本（今日）</span>
        <span className="value">
          <Tag color="default">无数据源</Tag>
        </span>
        <span className="hint">未接入计费埋点</span>
      </div>
      <div className="status-cell">
        <span className="label">数据刷新</span>
        <Tooltip title={p.updatedAt ? new Date(p.updatedAt).toLocaleTimeString() : '尚未刷新'}>
          <Button size="small" onClick={p.onRefresh} loading={p.loading}>
            立即刷新
          </Button>
        </Tooltip>
        <span className="hint">10s / 30s 分级轮询</span>
      </div>
    </div>
  )
}
