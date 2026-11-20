/* ax3d-bg.js
 * 旧零构建控制台 · three.js 3D 背景装饰层（离线 vendor / 独立 renderer / 零干扰）
 *
 * 与 index.html 已有的 WB3 沙盘、GV3 轨道视图并存：本模块只用自己独立的
 * canvas 与 WebGLRenderer，不改动任何既有 3D/2D 逻辑与事件绑定。
 * - 纯装饰：星尘、线框多面体、同心轨道，不代表任何业务数据；
 * - 离线：three.js 只从 /console/vendor/three.module.min.js 动态 import；
 * - 不拦截交互：canvas 由 CSS 设 pointer-events:none，且置于 .shell 之下；
 * - 可回退：WebGL/vendor 失败时移除 canvas，页面功能完全不受影响；
 * - 尊重 reduced-motion 与页面隐藏（暂停 RAF），pixelRatio 上限 1.5 控制开销。
 */
const VENDOR_URLS = [
  "/console/vendor/three.module.min.js",
  "./vendor/three.module.min.js",
  "/vendor/three.module.min.js",
];

async function loadThree() {
  for (const u of VENDOR_URLS) {
    try {
      const m = await import(u);
      if (m && m.WebGLRenderer) return m;
    } catch (e) { /* 换下一个候选路径 */ }
  }
  throw new Error("three.js 本地 vendor 不可用");
}

function prefersDark() {
  return !!(window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches);
}

async function boot() {
  const cv = document.getElementById("ax3dBg");
  if (!cv) return;
  if (!window.WebGLRenderingContext) { cv.remove(); return; }
  let T;
  try { T = await loadThree(); }
  catch (e) { cv.remove(); console.warn("[ax3d-bg] 回落：", e.message); return; }

  let renderer;
  try {
    renderer = new T.WebGLRenderer({ canvas: cv, antialias: true, alpha: true });
  } catch (e) { cv.remove(); console.warn("[ax3d-bg] WebGL 初始化失败：", e.message); return; }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.setClearColor(0x000000, 0);
  if (T.SRGBColorSpace) renderer.outputColorSpace = T.SRGBColorSpace;

  const scene = new T.Scene();
  const camera = new T.PerspectiveCamera(55, 1, 0.1, 600);
  camera.position.set(0, 0, 96);
  const world = new T.Group();
  scene.add(world);

  const dark = prefersDark();
  const cMain = dark ? 0x6f95ff : 0x2f6bff;
  const cDust = dark ? 0x93aede : 0x8399bb;

  /* 中央线框多面体 + 内核 */
  const shellGeo = new T.IcosahedronGeometry(23, 1);
  const shellMat = new T.MeshBasicMaterial({ color: cMain, wireframe: true, transparent: true, opacity: 0.20 });
  const shell = new T.Mesh(shellGeo, shellMat);
  world.add(shell);
  const coreGeo = new T.IcosahedronGeometry(7.5, 0);
  const coreMat = new T.MeshBasicMaterial({ color: cMain, transparent: true, opacity: 0.16 });
  const core = new T.Mesh(coreGeo, coreMat);
  world.add(core);

  /* 三条倾斜同心轨道 */
  const rings = [];
  [[36, 0x2f6bff, 0.32, 0.30], [46, 0x16a34a, 0.22, -0.55], [56, 0x6b4ea8, 0.18, 1.05]]
    .forEach(([r, col, op, tilt]) => {
      const m = new T.Mesh(
        new T.TorusGeometry(r, 0.09, 5, 128),
        new T.MeshBasicMaterial({ color: col, transparent: true, opacity: op }),
      );
      m.rotation.set(tilt, tilt * 0.6, 0);
      world.add(m);
      rings.push(m);
    });

  /* 装饰性星尘 + 稀疏连线（非业务数据） */
  const N = 560;
  const arr = new Float32Array(N * 3);
  for (let i = 0; i < N; i++) {
    const th = Math.random() * Math.PI * 2;
    const ph = Math.acos(2 * Math.random() - 1);
    const r = 62 + Math.random() * 120;
    arr[i * 3] = r * Math.sin(ph) * Math.cos(th);
    arr[i * 3 + 1] = r * Math.cos(ph) * 0.7;
    arr[i * 3 + 2] = r * Math.sin(ph) * Math.sin(th);
  }
  const dustGeo = new T.BufferGeometry();
  dustGeo.setAttribute("position", new T.BufferAttribute(arr, 3));
  const dustMat = new T.PointsMaterial({
    color: cDust, size: 0.62, transparent: true, opacity: 0.55,
    sizeAttenuation: true, depthWrite: false,
  });
  const dust = new T.Points(dustGeo, dustMat);
  scene.add(dust);

  const linkPos = [];
  for (let i = 0; i < 42; i++) {
    const a = i * 3, b = ((i * 7 + 13) % 42) * 3;
    linkPos.push(arr[a], arr[a + 1], arr[a + 2], arr[b], arr[b + 1], arr[b + 2]);
  }
  const linkGeo = new T.BufferGeometry();
  linkGeo.setAttribute("position", new T.Float32BufferAttribute(linkPos, 3));
  const linkMat = new T.LineBasicMaterial({ color: cMain, transparent: true, opacity: 0.12 });
  const links = new T.LineSegments(linkGeo, linkMat);
  scene.add(links);

  function resize() {
    const w = window.innerWidth, h = window.innerHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  resize();
  window.addEventListener("resize", resize);

  /* 明暗主题切换时同步材质颜色 */
  const mq = window.matchMedia ? matchMedia("(prefers-color-scheme: dark)") : null;
  const onTheme = () => {
    const d = prefersDark();
    shellMat.color.setHex(d ? 0x6f95ff : 0x2f6bff);
    coreMat.color.setHex(d ? 0x6f95ff : 0x2f6bff);
    dustMat.color.setHex(d ? 0x93aede : 0x8399bb);
    linkMat.color.setHex(d ? 0x6f95ff : 0x2f6bff);
  };
  if (mq && mq.addEventListener) mq.addEventListener("change", onTheme);

  const reduce = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  let raf = 0, last = 0, clock = 0, paused = false;
  function render() { renderer.render(scene, camera); }
  function frame(ts) {
    if (paused) return;
    const dt = last ? Math.min(0.05, (ts - last) / 1000) : 0.016;
    last = ts; clock += dt;
    world.rotation.y += dt * 0.055;
    world.rotation.x = Math.sin(clock * 0.12) * 0.10;
    core.scale.setScalar(1 + Math.sin(clock * 1.1) * 0.10);
    shell.rotation.z -= dt * 0.02;
    rings.forEach((m, i) => { m.rotation.z += dt * (0.05 + i * 0.02) * (i % 2 ? -1 : 1); });
    dust.rotation.y -= dt * 0.008;
    links.rotation.y = dust.rotation.y;
    render();
    raf = requestAnimationFrame(frame);
  }

  if (reduce) { render(); } else { raf = requestAnimationFrame(frame); }
  document.addEventListener("visibilitychange", () => {
    if (reduce) return;
    if (document.hidden) { paused = true; cancelAnimationFrame(raf); }
    else if (paused) { paused = false; last = 0; raf = requestAnimationFrame(frame); }
  });

  window.__ax3dBg = {
    revision: T.REVISION,
    rendererType: "WebGLRenderer",
    decorativeOnly: true,
    dispose() {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
      [shellGeo, coreGeo, dustGeo, linkGeo].forEach((g) => g.dispose());
      [shellMat, coreMat, dustMat, linkMat].forEach((m) => m.dispose());
      rings.forEach((m) => { m.geometry.dispose(); m.material.dispose(); });
      renderer.dispose();
      cv.remove();
    },
  };
}

boot();
