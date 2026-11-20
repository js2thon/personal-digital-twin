import { useCallback, useEffect, useRef, useState } from 'react'

/** 统一分级轮询：页面隐藏 / 视图未激活时暂停，支持手动刷新。
 *  SSE 由后端开放后可在 services/api.ts 的 onStream() 中替换，接口保持不变。
 */
export function usePolling<T>(
  loader: () => Promise<T>,
  intervalMs: number,
  enabled = true,
) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [updatedAt, setUpdatedAt] = useState(0)
  const fnRef = useRef(loader)
  fnRef.current = loader

  const tick = useCallback(async () => {
    try {
      const next = await fnRef.current()
      setData(next)
      setError(null)
      setUpdatedAt(Date.now())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!enabled) return
    let alive = true
    const run = async () => {
      if (!alive || document.hidden) return
      await tick()
    }
    void run()
    const id = window.setInterval(run, intervalMs)
    const onVisible = () => { if (!document.hidden) void run() }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      alive = false
      window.clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [enabled, intervalMs, tick])

  return { data, error, loading, updatedAt, refresh: tick }
}
