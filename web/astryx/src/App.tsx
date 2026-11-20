import { Button, ConfigProvider, Tag, theme as antdTheme } from 'antd'
import { useEffect } from 'react'
import { NAV, REACT_VIEWS, useAppStore, type ViewKey } from './stores/useAppStore'
import Dashboard from './pages/Dashboard'
import KnowledgePipeline from './pages/KnowledgePipeline'
import LegacyBridge from './pages/LegacyBridge'
import { ThreeBackdrop } from './components/ThreeBackdrop'

const VALID = NAV.flatMap((g) => g.items.map((i) => i.key))

function viewFromUrl(): ViewKey {
  const v = new URLSearchParams(window.location.search).get('view') as ViewKey | null
  return v && VALID.includes(v) ? v : 'ops'
}

export default function App() {
  const { view, theme, setView, toggleTheme } = useAppStore()

  useEffect(() => {
    setView(viewFromUrl())
    const onPop = () => setView(viewFromUrl())
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [setView])

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
  }, [theme])

  const navigate = (v: string) => {
    const key = (VALID.includes(v as ViewKey) ? v : 'ops') as ViewKey
    setView(key)
    const url = new URL(window.location.href)
    url.searchParams.set('view', key)
    window.history.replaceState(null, '', url)
  }

  const title = NAV.flatMap((g) => g.items).find((i) => i.key === view)?.label ?? ''

  return (
    <ConfigProvider
      theme={{
        algorithm: theme === 'dark' ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: { colorPrimary: '#2f6bff', borderRadius: 8, fontSize: 13 },
      }}
    >
      <div className="app">
        <ThreeBackdrop theme={theme} />
        <aside className="sidebar">
          <div className="brand">
            <div className="brand-mark">DE</div>
            <div>
              <div className="brand-title">数字员工运营中台</div>
              <div className="brand-sub">V1.3 · React 换栈 Phase 0</div>
            </div>
          </div>
          {NAV.map((g) => (
            <div key={g.group}>
              <div className="nav-group">{g.group}</div>
              {g.items.map((it) => (
                <button key={it.key}
                  className={`nav-item ${view === it.key ? 'active' : ''}`}
                  onClick={() => navigate(it.key)}>
                  <span className="nav-ico">{it.icon}</span>
                  <span>{it.label}</span>
                  {!REACT_VIEWS.includes(it.key) &&
                    <Tag color="default" style={{ marginLeft: 'auto', fontSize: 9, lineHeight: '16px' }}>
                      旧
                    </Tag>}
                </button>
              ))}
            </div>
          ))}
          <div className="nav-foot">
            <div>后端 FastAPI · 8101（Vite 代理同源）</div>
            <div>零 mock 数据 · 六项上下文由服务端注入</div>
            <Button type="link" size="small" href="/" target="_blank"
              style={{ paddingLeft: 0, marginTop: 4, fontSize: 11 }}>
              打开旧版零构建控制台 ↗
            </Button>
          </div>
        </aside>

        <main className="main">
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
            <span className="mono" style={{ color: 'var(--ax-text-tertiary)' }}>
              当前视图 · {title}
            </span>
            <span style={{ marginLeft: 'auto' }} />
            <Button size="small" onClick={toggleTheme}>
              {theme === 'dark' ? '☀ 浅色' : '☾ 暗色'}
            </Button>
          </div>

          {view === 'ops' && (
            <Dashboard active theme={theme} onNavigate={navigate} />
          )}
          {view === 'pipeline' && <KnowledgePipeline active />}
          {!REACT_VIEWS.includes(view) && <LegacyBridge view={view} />}
        </main>
      </div>
    </ConfigProvider>
  )
}
