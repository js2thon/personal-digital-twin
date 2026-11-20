import { create } from 'zustand'

export type ViewKey =
  | 'ops' | 'pipeline' | 'ontology' | 'cognition'
  | 'decision' | 'messages' | 'observability' | 'settings'

export interface NavItem { key: ViewKey; label: string; icon: string }
export interface NavGroup { group: string; items: NavItem[] }

/** 6+1 信息架构：运营首页 + 6 个工作区组 + 观测中心。
 *  仅 ops / pipeline 在本轮 React 化，其余明确回落到旧零构建控制台。 */
export const NAV: NavGroup[] = [
  { group: '运营', items: [{ key: 'ops', label: '数字员工运营', icon: '◈' }] },
  {
    group: '知识生产', items: [
      { key: 'pipeline', label: '知识生产流水线', icon: '▤' },
      { key: 'ontology', label: '知识与本体', icon: '◇' },
    ],
  },
  { group: '认知与记忆', items: [{ key: 'cognition', label: '认知管理', icon: '◎' }] },
  {
    group: '交互与决策', items: [
      { key: 'decision', label: '任务与决策', icon: '▦' },
      { key: 'messages', label: '消息与协作', icon: '✉' },
    ],
  },
  { group: '观测中心', items: [{ key: 'observability', label: '链路与评估', icon: '⌁' }] },
  { group: '系统', items: [{ key: 'settings', label: '系统设置', icon: '⚙' }] },
]

export const REACT_VIEWS: ViewKey[] = ['ops', 'pipeline']
export const LEGACY_VIEW_PARAM: Partial<Record<ViewKey, string>> = {
  ontology: 'ontology', cognition: 'cognition', decision: 'decision',
  messages: 'messages', observability: 'lineage', settings: 'settings',
}

type Theme = 'light' | 'dark'

interface AppState {
  view: ViewKey
  theme: Theme
  setView: (v: ViewKey) => void
  toggleTheme: () => void
}

const initialTheme = (): Theme => {
  try {
    const saved = localStorage.getItem('v13-theme')
    if (saved === 'light' || saved === 'dark') return saved
  } catch { /* localStorage 不可用时按系统偏好 */ }
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export const useAppStore = create<AppState>((set, get) => ({
  view: 'ops',
  theme: initialTheme(),
  setView: (view) => set({ view }),
  toggleTheme: () => {
    const theme: Theme = get().theme === 'light' ? 'dark' : 'light'
    try { localStorage.setItem('v13-theme', theme) } catch { /* ignore */ }
    set({ theme })
  },
}))
