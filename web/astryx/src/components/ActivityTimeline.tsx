import { Button } from 'antd'
import { fmtTime } from '../services/api'
import type { ActivityTimelineProps } from '../contracts'

/** 方案 ActivityTimelineProps 契约。空状态与骨架屏优先于转圈。 */
export default function ActivityTimeline(p: ActivityTimelineProps) {
  if (p.loading && p.items.length === 0) {
    return (
      <div>
        {[0, 1, 2, 3, 4].map((i) => (
          <div key={i} className="skeleton" style={{ width: `${92 - i * 9}%` }} />
        ))}
      </div>
    )
  }
  if (p.items.length === 0) {
    return (
      <div className="empty">
        <span className="big">◷</span>
        暂无近期活动
        <div style={{ marginTop: 4, fontSize: 10.5 }}>
          数据来自 runs / decisions / replies / outbox
        </div>
      </div>
    )
  }
  return (
    <>
      <div className="feed">
        {p.items.map((it) => (
          <div key={it.id} className="feed-item"
            onClick={() => p.onItemClick?.(it)}
            style={{ cursor: p.onItemClick ? 'pointer' : 'default' }}>
            <span className="feed-time">{fmtTime(it.timestamp)}</span>
            <i className={`feed-dot k-${it.type}`} />
            <div style={{ minWidth: 0 }}>
              <div className="feed-title" title={it.title}>{it.title}</div>
              <div className="feed-meta">
                {it.status === 'failed' ? '失败' : it.status === 'pending' ? '待处理' : '完成'}
                {it.digitalEmployeeName ? ` · ${it.digitalEmployeeName}` : ''}
                {it.meta?.source ? ` · ${String(it.meta.source)}` : ''}
              </div>
            </div>
          </div>
        ))}
      </div>
      {p.hasMore && (
        <Button type="link" size="small" onClick={p.onLoadMore}
          style={{ paddingLeft: 0, marginTop: 6 }}>
          加载更多
        </Button>
      )}
    </>
  )
}
