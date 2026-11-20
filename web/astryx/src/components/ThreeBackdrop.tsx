/** V1.3 React 中台 · three.js 3D 视觉层
 *
 * 照实口径（沿用本仓库「不编造」约定）：
 * - 这一层是**纯装饰**：星尘、线框多面体、同心轨道，不承载任何业务数据，
 *   也不做「指标 → 3D 节点」的牵强映射；真实指标以卡片数字为准。
 * - three.js 由 npm 本地依赖提供（r160），零 CDN、零外网请求。
 * - 明暗主题来自 Zustand store，切换时销毁并重建场景，颜色随主题同步。
 * - 尊重 prefers-reduced-motion（只渲染静态帧）与页面隐藏（暂停 RAF）。
 * - StrictMode 下会 mount/unmount 两次，cleanup 必须彻底释放 renderer 与几何体。
 */
import { useEffect, useRef, type RefObject } from 'react'
import * as THREE from 'three'

export interface ThreeBackdropProps {
  theme: 'light' | 'dark'
  density?: number
}

interface SceneHandle {
  camera: THREE.PerspectiveCamera
  render: () => void
  tick: (dt: number, clock: number) => void
  dispose: () => void
}

function buildScene(
  renderer: THREE.WebGLRenderer,
  dark: boolean,
  density: number,
  compact: boolean,
): SceneHandle {
  const scene = new THREE.Scene()
  const camera = new THREE.PerspectiveCamera(52, 1, 0.1, 600)
  camera.position.set(0, 0, compact ? 54 : 96)
  const world = new THREE.Group()
  scene.add(world)

  const main = dark ? 0x6f95ff : 0x2f6bff
  const dustCol = dark ? 0x93aede : 0x8399bb
  const k = compact ? 0.55 : 1

  const shellGeo = new THREE.IcosahedronGeometry(23 * k, 1)
  const shellMat = new THREE.MeshBasicMaterial({
    color: main, wireframe: true, transparent: true, opacity: 0.24,
  })
  const shell = new THREE.Mesh(shellGeo, shellMat)
  world.add(shell)

  const coreGeo = new THREE.IcosahedronGeometry(7.5 * k, 0)
  const coreMat = new THREE.MeshBasicMaterial({ color: main, transparent: true, opacity: 0.18 })
  const core = new THREE.Mesh(coreGeo, coreMat)
  world.add(core)

  const rings: THREE.Mesh[] = []
  const ringSpec: ReadonlyArray<readonly [number, number, number, number]> = [
    [36, 0x2f6bff, 0.34, 0.30],
    [46, 0x16a34a, 0.24, -0.55],
    [56, 0x6b4ea8, 0.20, 1.05],
  ]
  ringSpec.forEach(([r, col, op, tilt]) => {
    const m = new THREE.Mesh(
      new THREE.TorusGeometry(r * k, 0.09 * k + 0.03, 5, 128),
      new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: op }),
    )
    m.rotation.set(tilt, tilt * 0.6, 0)
    world.add(m)
    rings.push(m)
  })

  const n = Math.max(80, density)
  const arr = new Float32Array(n * 3)
  for (let i = 0; i < n; i++) {
    const th = Math.random() * Math.PI * 2
    const ph = Math.acos(2 * Math.random() - 1)
    const r = (62 + Math.random() * 120) * (compact ? 0.65 : 1)
    arr[i * 3] = r * Math.sin(ph) * Math.cos(th)
    arr[i * 3 + 1] = r * Math.cos(ph) * 0.7
    arr[i * 3 + 2] = r * Math.sin(ph) * Math.sin(th)
  }
  const dustGeo = new THREE.BufferGeometry()
  dustGeo.setAttribute('position', new THREE.BufferAttribute(arr, 3))
  const dustMat = new THREE.PointsMaterial({
    color: dustCol, size: 0.62, transparent: true, opacity: 0.55,
    sizeAttenuation: true, depthWrite: false,
  })
  const dust = new THREE.Points(dustGeo, dustMat)
  scene.add(dust)

  const linkPos: number[] = []
  for (let i = 0; i < 42; i++) {
    const a = i * 3
    const b = ((i * 7 + 13) % 42) * 3
    linkPos.push(arr[a], arr[a + 1], arr[a + 2], arr[b], arr[b + 1], arr[b + 2])
  }
  const linkGeo = new THREE.BufferGeometry()
  linkGeo.setAttribute('position', new THREE.Float32BufferAttribute(linkPos, 3))
  const linkMat = new THREE.LineBasicMaterial({ color: main, transparent: true, opacity: 0.12 })
  const links = new THREE.LineSegments(linkGeo, linkMat)
  scene.add(links)

  return {
    camera,
    render: () => renderer.render(scene, camera),
    tick: (dt: number, clock: number) => {
      world.rotation.y += dt * 0.055
      world.rotation.x = Math.sin(clock * 0.12) * 0.10
      core.scale.setScalar(1 + Math.sin(clock * 1.1) * 0.10)
      shell.rotation.z -= dt * 0.02
      rings.forEach((m, i) => {
        m.rotation.z += dt * (0.05 + i * 0.02) * (i % 2 ? -1 : 1)
      })
      dust.rotation.y -= dt * 0.008
      links.rotation.y = dust.rotation.y
    },
    dispose: () => {
      ;[shellGeo, coreGeo, dustGeo, linkGeo].forEach((g) => g.dispose())
      ;[shellMat, coreMat, dustMat, linkMat].forEach((m) => m.dispose())
      rings.forEach((m) => {
        m.geometry.dispose()
        ;(m.material as THREE.Material).dispose()
      })
    },
  }
}

function useThreeCanvas(
  ref: RefObject<HTMLCanvasElement | null>,
  theme: 'light' | 'dark',
  density: number,
  compact: boolean,
) {
  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true })
    } catch {
      canvas.style.display = 'none'
      return
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5))
    renderer.setClearColor(0x000000, 0)
    renderer.outputColorSpace = THREE.SRGBColorSpace

    const handle = buildScene(renderer, theme === 'dark', density, compact)
    const resize = () => {
      const w = canvas.clientWidth || window.innerWidth
      const h = canvas.clientHeight || window.innerHeight
      renderer.setSize(w, h, false)
      handle.camera.aspect = w / h
      handle.camera.updateProjectionMatrix()
      handle.render()
    }
    resize()
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null
    if (ro) ro.observe(canvas)
    else window.addEventListener('resize', resize)

    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
    let raf = 0
    let last = 0
    let clock = 0
    let paused = false
    const frame = (ts: number) => {
      if (paused) return
      const dt = last ? Math.min(0.05, (ts - last) / 1000) : 0.016
      last = ts
      clock += dt
      handle.tick(dt, clock)
      handle.render()
      raf = requestAnimationFrame(frame)
    }
    if (reduce) handle.render()
    else raf = requestAnimationFrame(frame)

    const onVis = () => {
      if (reduce) return
      if (document.hidden) {
        paused = true
        cancelAnimationFrame(raf)
      } else if (paused) {
        paused = false
        last = 0
        raf = requestAnimationFrame(frame)
      }
    }
    document.addEventListener('visibilitychange', onVis)

    return () => {
      cancelAnimationFrame(raf)
      document.removeEventListener('visibilitychange', onVis)
      if (ro) ro.disconnect()
      else window.removeEventListener('resize', resize)
      handle.dispose()
      renderer.dispose()
    }
  }, [ref, theme, density, compact])
}

/** 全屏 fixed 背景层：不参与 grid 布局、不拦截任何交互。 */
export function ThreeBackdrop({ theme, density = 520 }: ThreeBackdropProps) {
  const ref = useRef<HTMLCanvasElement>(null)
  useThreeCanvas(ref, theme, density, false)
  return <canvas ref={ref} className="three-backdrop" aria-hidden="true" />
}

/** 可嵌入卡片的 3D 语义球（同样为纯装饰，caption 明确标注）。 */
export function SemanticOrb({ theme, density = 220 }: ThreeBackdropProps) {
  const ref = useRef<HTMLCanvasElement>(null)
  useThreeCanvas(ref, theme, density, true)
  return (
    <div className="orb-wrap">
      <canvas ref={ref} className="orb-canvas" aria-hidden="true" />
      <div className="orb-caption">
        3D 装饰层 · 指标以卡片数字为准 · three.js r160（本地 npm 依赖，无业务数据映射）
      </div>
    </div>
  )
}

export default ThreeBackdrop
