import { Button } from 'antd'
import { LEGACY_VIEW_PARAM, type ViewKey } from '../stores/useAppStore'

/** 其余 6 个工作区本轮不重复造轮子：继续由既有 461 KB 零构建控制台承载。
 *  明确给出深链接，而不是渲染一个没有数据的空壳页面。 */
export default function LegacyBridge({ view }: { view: ViewKey }) {
  const param = LEGACY_VIEW_PARAM[view] ?? 'twin'
  const url = `${import.meta.env.BASE_URL}../?view=${param}`
  const labels: Partial<Record<ViewKey, string>> = {
    ontology: '知识与本体（力导向图谱 / 双时间轴 / 版本 Diff）',
    cognition: '认知管理（候选 / 冲突 / 版本时间线）',
    decision: '任务与决策（决策漏斗 / 审批看板 / 证据链）',
    messages: '消息与协作（草稿对比 / 四重闸门）',
    observability: '链路与评估（桑基 / 门禁灯）',
    settings: '系统设置',
  }
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">{labels[view] ?? view}</h1>
        <span className="page-sub">Sprint 1 未 React 化 · 沿用既有控制台</span>
      </div>
      <div className="card fade-in">
        <div className="card-head">渐进增强边界</div>
        <div className="note">
          本轮换栈 Phase 0 只把 <b>运营驾驶舱</b> 与 <b>知识生产流水线</b> 迁到
          React 19 + TypeScript + Vite 6。其余工作区已有完整实现（195 个端点、
          8 个视图），与其重写成空壳，不如保留并逐步迁移。
        </div>
        <div style={{ marginTop: 12 }}>
          <Button type="primary" href={url} target="_blank">
            在新窗口打开该工作区
          </Button>
        </div>
        <div className="note warn" style={{ marginTop: 12 }}>
          <b>不宣称已完成：</b>方案 §3.3 的决策漏斗、审批看板、任务 Trace、
          G6 图谱、桑基链路、评估趋势折线、消息草稿对比均<b>尚未实现</b>；
          <code>evaluation/history.runs = 0</code> 时趋势折线没有真实序列可画。
        </div>
      </div>
    </>
  )
}
