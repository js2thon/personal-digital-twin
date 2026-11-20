import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { MetricCardProps } from '../contracts'

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])

function Sparkline({ data, dark }: { data: number[]; dark: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const chart = echarts.init(el, undefined, { renderer: 'canvas' })
    const line = dark ? '#6f95ff' : '#2f6bff'
    chart.setOption({
      animationDuration: 220,
      grid: { left: 1, right: 1, top: 3, bottom: 3 },
      xAxis: { type: 'category', show: false, boundaryGap: false,
        data: data.map((_, i) => String(i)) },
      yAxis: { type: 'value', show: false, scale: true },
      tooltip: { trigger: 'axis', confine: true },
      series: [{
        type: 'line', data, smooth: true, symbol: 'none',
        lineStyle: { width: 1.6, color: line },
        areaStyle: { color: line, opacity: 0.12 },
      }],
    })
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(el)
    return () => { ro.disconnect(); chart.dispose() }
  }, [data, dark])
  return <div className="spark" ref={ref} />
}

/** 方案 MetricCardProps 契约。sparklineData 为 null 时明确显示「无历史」，
 *  不生成任何假趋势线。 */
export default function MetricCard(p: MetricCardProps & { dark?: boolean }) {
  const hasSpark = Array.isArray(p.sparklineData) && p.sparklineData.length >= 2
  const status = p.status ?? 'normal'
  return (
    <div className={`kpi-row status-${status}`}
      onClick={p.onClick} style={{ cursor: p.onClick ? 'pointer' : 'default' }}>
      <span className="title">{p.title}</span>
      {p.value === null || p.value === '—'
        ? <span className="num muted">无数据源</span>
        : <span className="num">{p.value}{p.unit ?? ''}</span>}
      {hasSpark
        ? <Sparkline data={p.sparklineData as number[]} dark={!!p.dark} />
        : <span className="spark" style={{ textAlign: 'right', color: 'var(--ax-text-tertiary)', fontSize: 10 }}>
            {p.sparklineData === null ? '无历史' : ''}
          </span>}
    </div>
  )
}
