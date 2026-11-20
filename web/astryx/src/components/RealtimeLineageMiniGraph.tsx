import { useEffect, useRef, useState } from 'react'
import type { RealtimeLineageMiniGraphProps, LineageNode } from '../contracts'

/** 方案 RealtimeLineageMiniGraphProps 契约。
 *  纯 SVG + CSS 流动虚线（零额外图库），节点数变化时脉冲，不整页闪烁。 */
export default function RealtimeLineageMiniGraph(p: RealtimeLineageMiniGraphProps) {
  const prev = useRef<Record<string, number>>({})
  const [pulsed, setPulsed] = useState<Record<string, boolean>>({})

  useEffect(() => {
    const changed: Record<string, boolean> = {}
    for (const n of p.nodes) {
      if (prev.current[n.id] != null && prev.current[n.id] !== n.count) {
        changed[n.id] = true
      }
      prev.current[n.id] = n.count
    }
    if (Object.keys(changed).length) {
      setPulsed(changed)
      const t = window.setTimeout(() => setPulsed({}), 650)
      return () => window.clearTimeout(t)
    }
  }, [p.nodes])

  if (p.nodes.length === 0) {
    return <div className="empty"><span className="big">⌁</span>链路数据不可用</div>
  }

  return (
    <div className="chain">
      {p.nodes.map((n, i) => (
        <div key={n.id} style={{ display: 'contents' }}>
          <div className={`chain-node ${pulsed[n.id] ? 'pulse' : ''}`}
            onClick={() => p.onNodeClick?.(n)} title="点击进入对应工作区">
            <div className="lbl">{n.label}</div>
            <div className="cnt">{n.count}</div>
          </div>
          {i < p.nodes.length - 1 && (
            <svg className="chain-arrow" width="26" height="14" viewBox="0 0 26 14"
              aria-hidden="true">
              <line x1="1" y1="7" x2="19" y2="7" stroke="var(--ax-primary)"
                strokeWidth="2" className="edge-flow" />
              <path d="M19 3 L25 7 L19 11" fill="none" stroke="var(--ax-primary)"
                strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          )}
        </div>
      ))}
    </div>
  )
}

/** 从真实 /lineage/overview 构造 5 节点，不补零、不编造。 */
export function lineageNodes(counts: Record<string, number> | undefined): LineageNode[] {
  if (!counts) return []
  return [
    { id: 'document', label: '文档', count: counts.documents ?? 0, type: 'document' },
    { id: 'chunk', label: '切片', count: counts.chunks ?? 0, type: 'chunk' },
    { id: 'claim', label: 'Claim', count: counts.claims ?? 0, type: 'claim' },
    { id: 'fact', label: 'Fact', count: counts.facts ?? 0, type: 'fact' },
    { id: 'decision', label: '决策', count: counts.decisions ?? 0, type: 'decision' },
  ]
}
