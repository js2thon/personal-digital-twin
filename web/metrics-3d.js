/* metrics-3d.js — 本体工作台 three.js 三维可视化（指标血缘/柱/矩阵 + 通用三维力导向图 graph3d；离线、零 CDN）
 *
 * 三个视图，全部来自真实接口数据：
 *   lineage(el, data, opts)  指标血缘三维 DAG：类 → 度量 → 原子 → 派生 → 复合 按层沿 Z 轴排开，可旋转/缩放/点选
 *   bars(el, spec)           单指标三维柱：按维度分组；时间序列加折线脊
 *   matrix(el, spec)         多指标 × 维度 三维柱阵：每个指标按自身最大值归一（单位不同不可直接比高），悬停显示原值
 * 约定（与 semantic-3d.js 一致）：
 *   - three.js 只从本地 vendor 动态 import；WebGL 不可用时抛错，由调用方回落 2D；
 *   - 颜色取自页面 CSS 令牌，跟随明暗主题；尊重 prefers-reduced-motion（停用自转）。
 */
const VENDOR_URLS = ["/console/vendor/three.module.min.js", "./vendor/three.module.min.js"];
let T = null;

export async function ready() {
  if (T) return T;
  for (const u of VENDOR_URLS) {
    try { const m = await import(u); if (m && m.WebGLRenderer) { T = m; return T; } } catch (e) { /* next */ }
  }
  throw new Error("three.js 本地 vendor 加载失败（/console/vendor/three.module.min.js）");
}

const css = (n, fb) => (getComputedStyle(document.documentElement).getPropertyValue(n).trim() || fb);
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const isDark = () => {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
};
function palette() {
  return isDark()
    ? { cat: [0x9aa3af, 0xeec448, 0xa1caff, 0x90ca90, 0xd8a8ff], text: "#f1f1f1", sub: "#c6c6c6",
        grid: 0x3f3f46, edge: 0x71717a, hi: 0xffaea7, bg: "rgba(34,34,35,0.92)", line: "rgba(255,255,255,0.18)" }
    : { cat: [0x6b7280, 0x836700, 0x0067ce, 0x2f7d33, 0x7c3aed], text: "#111111", sub: "#474747",
        grid: 0xd4d4d8, edge: 0x9ca3af, hi: 0xc0262c, bg: "rgba(255,255,255,0.94)", line: "rgba(0,0,0,0.14)" };
}

/* ------------------------------------------------------------------ 公共舞台 */
class Stage {
  constructor(el, { dist = 60, pitch = 0.42, yaw = 0.6, target = [0, 0, 0], autoRotate = true } = {}) {
    this.el = el; el.innerHTML = ""; el.style.position = "relative";
    this.renderer = new T.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    this.renderer.setClearColor(0x000000, 0);
    el.appendChild(this.renderer.domElement);
    this.renderer.domElement.style.cssText = "width:100%;height:100%;display:block;cursor:grab;touch-action:none";
    this.scene = new T.Scene();
    this.camera = new T.PerspectiveCamera(42, 1, 0.1, 2000);
    this.root = new T.Group(); this.scene.add(this.root);
    this.scene.add(new T.AmbientLight(0xffffff, isDark() ? 0.55 : 0.75));
    const d = new T.DirectionalLight(0xffffff, isDark() ? 0.9 : 0.8); d.position.set(30, 60, 40); this.scene.add(d);
    const d2 = new T.DirectionalLight(0xffffff, 0.25); d2.position.set(-40, 20, -30); this.scene.add(d2);
    Object.assign(this, { dist, pitch, yaw, target: new T.Vector3(...target), auto: autoRotate && !reduceMotion(),
      drag: null, last: 0, pickables: [], hoverCb: null, clickCb: null, raf: 0, dead: false });
    this.tip = document.createElement("div");
    this.tip.style.cssText = "position:absolute;pointer-events:none;display:none;z-index:5;padding:6px 9px;border-radius:8px;" +
      "font-size:12px;max-width:320px;box-shadow:0 2px 8px rgba(0,0,0,.18);background:" + palette().bg + ";color:" + palette().text +
      ";border:1px solid " + palette().line;
    el.appendChild(this.tip);
    this.ray = new T.Raycaster(); this.mouse = new T.Vector2(); this.hovered = null;
    const c = this.renderer.domElement;
    c.addEventListener("pointerdown", e => { this.drag = { x: e.clientX, y: e.clientY, moved: 0, pan: e.button === 2 || e.shiftKey }; c.setPointerCapture(e.pointerId); c.style.cursor = "grabbing"; });
    c.addEventListener("pointermove", e => this._move(e));
    c.addEventListener("pointerup", e => { const d = this.drag; this.drag = null; c.style.cursor = "grab"; if (d && d.moved < 4) this._click(e); });
    c.addEventListener("pointerleave", () => { this.tip.style.display = "none"; });
    c.addEventListener("contextmenu", e => e.preventDefault());
    c.addEventListener("wheel", e => { e.preventDefault(); this.dist = Math.max(8, Math.min(600, this.dist * (e.deltaY > 0 ? 1.1 : 0.9))); this.last = performance.now(); }, { passive: false });
    c.addEventListener("dblclick", () => { this.auto = !this.auto && !reduceMotion(); });
    this.ro = new ResizeObserver(() => this.resize()); this.ro.observe(el);
    this.resize();
    const loop = () => { if (this.dead) return; this.raf = requestAnimationFrame(loop); this.frame(); };
    loop();
  }
  resize() {
    const w = this.el.clientWidth || 600, h = this.el.clientHeight || 420;
    this.renderer.setSize(w, h, false); this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
  }
  _ndc(e) { const r = this.renderer.domElement.getBoundingClientRect(); this.mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1); return r; }
  _pick(e) { this._ndc(e); this.ray.setFromCamera(this.mouse, this.camera); const h = this.ray.intersectObjects(this.pickables, false); return h.length ? h[0].object : null; }
  _move(e) {
    if (this.drag) {
      const dx = e.clientX - this.drag.x, dy = e.clientY - this.drag.y; this.drag.moved += Math.abs(dx) + Math.abs(dy);
      if (this.drag.pan) {
        const s = this.dist / 600, right = new T.Vector3(Math.cos(this.yaw), 0, -Math.sin(this.yaw));
        this.target.addScaledVector(right, -dx * s); this.target.y += dy * s;
      } else { this.yaw -= dx * 0.008; this.pitch = Math.max(-1.3, Math.min(1.35, this.pitch + dy * 0.006)); }
      this.drag.x = e.clientX; this.drag.y = e.clientY; this.last = performance.now(); return;
    }
    const o = this._pick(e); const r = this.renderer.domElement.getBoundingClientRect();
    if (o !== this.hovered) { if (this.hoverCb) this.hoverCb(o, this.hovered); this.hovered = o; }
    if (o && o.userData.tip) {
      this.tip.innerHTML = o.userData.tip; this.tip.style.display = "block";
      this.tip.style.left = Math.min(r.width - 330, e.clientX - r.left + 14) + "px"; this.tip.style.top = (e.clientY - r.top + 14) + "px";
      this.renderer.domElement.style.cursor = o.userData.pick ? "pointer" : "grab";
    } else { this.tip.style.display = "none"; this.renderer.domElement.style.cursor = "grab"; }
  }
  _click(e) { const o = this._pick(e); if (o && this.clickCb) this.clickCb(o); }
  frame() {
    if (this.auto && performance.now() - this.last > 2500) this.yaw += 0.0018;
    const cp = Math.cos(this.pitch);
    this.camera.position.set(this.target.x + this.dist * cp * Math.sin(this.yaw), this.target.y + this.dist * Math.sin(this.pitch),
      this.target.z + this.dist * cp * Math.cos(this.yaw));
    this.camera.lookAt(this.target);
    if (this.onFrame) this.onFrame();
    this.renderer.render(this.scene, this.camera);
  }
  dispose() {
    this.dead = true; cancelAnimationFrame(this.raf); this.ro.disconnect();
    this.scene.traverse(o => { if (o.geometry) o.geometry.dispose(); if (o.material) { (Array.isArray(o.material) ? o.material : [o.material]).forEach(m => { if (m.map) m.map.dispose(); m.dispose(); }); } });
    this.renderer.dispose(); this.el.innerHTML = "";
  }
}

function label(text, { size = 26, color, bg = true, maxW = 520 } = {}) {
  const P = palette(), c = document.createElement("canvas"), g = c.getContext("2d"), ratio = 2;
  g.font = `600 ${size}px Figtree,"PingFang SC","Microsoft YaHei",sans-serif`;
  const s = String(text); const w = Math.min(maxW, Math.ceil(g.measureText(s).width) + 28), h = size + 22;
  c.width = w * ratio; c.height = h * ratio; g.scale(ratio, ratio);
  g.font = `600 ${size}px Figtree,"PingFang SC","Microsoft YaHei",sans-serif`;
  if (bg) { g.fillStyle = P.bg; g.strokeStyle = P.line; g.lineWidth = 1.5; g.beginPath(); (g.roundRect ? g.roundRect(1, 1, w - 2, h - 2, 9) : g.rect(1, 1, w - 2, h - 2)); g.fill(); g.stroke(); }
  g.fillStyle = color || P.text; g.textBaseline = "middle"; g.textAlign = "center";
  let t = s; while (g.measureText(t).width > w - 24 && t.length > 2) t = t.slice(0, -2) + "…";
  g.fillText(t, w / 2, h / 2 + 1);
  const tex = new T.CanvasTexture(c); tex.colorSpace = T.SRGBColorSpace; tex.anisotropy = 4;
  const sp = new T.Sprite(new T.SpriteMaterial({ map: tex, transparent: true, depthWrite: false }));
  sp.scale.set(w / 30, h / 30, 1); sp.renderOrder = 10;
  return sp;
}
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = v => v == null || isNaN(v) ? "—" : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });

/* ------------------------------------------------------------------ 1. 指标血缘三维 DAG */
export async function lineage(el, data, { onPick, focus, hide = id => /_(mom|yoy|by_month)$/.test(id) } = {}) {
  await ready();
  const P = palette();
  const nodes = data.nodes.filter(n => !hide(n.id));
  const ids = new Set(nodes.map(n => n.id));
  const links = [...new Map(data.links.filter(l => ids.has(l.source) && ids.has(l.target) && l.source !== l.target)
    .map(l => [l.source + "→" + l.target, l])).values()];
  const used = new Set(links.flatMap(l => [l.source, l.target]));
  const N = nodes.filter(n => used.has(n.id));
  // 层：按类别（类0 / 度量1 / 原子2 / 派生3 / 复合4），层内按网格铺开
  const layers = {}; N.forEach(n => (layers[n.category] = layers[n.category] || []).push(n));
  const pos = new Map(), gap = 6, layerZ = 18;
  Object.entries(layers).forEach(([k, arr]) => {
    const cols = Math.ceil(Math.sqrt(arr.length * 1.6)); arr.sort((a, b) => a.name.localeCompare(b.name, "zh"));
    arr.forEach((n, i) => {
      const r = Math.floor(i / cols), c = i % cols, rows = Math.ceil(arr.length / cols);
      pos.set(n.id, new T.Vector3((c - (cols - 1) / 2) * gap, ((rows - 1) / 2 - r) * gap * 0.75, (Number(k) - 2) * layerZ));
    });
  });
  const box = new T.Box3(); pos.forEach(p => box.expandByPoint(p));
  const ctr = box.getCenter(new T.Vector3()), ext = box.getSize(new T.Vector3());
  const st = new Stage(el, { dist: Math.max(70, Math.max(ext.x, ext.y, ext.z) * 1.55 + 20), pitch: 0.28, yaw: 0.9,
    target: [ctr.x, ctr.y, ctr.z] });
  const geo = { 0: new T.BoxGeometry(3.4, 3.4, 3.4), 1: new T.OctahedronGeometry(1.7), 2: new T.SphereGeometry(1.5, 24, 16),
    3: new T.ConeGeometry(1.5, 3, 20), 4: new T.IcosahedronGeometry(1.9, 0) };
  const meshes = new Map();
  N.forEach(n => {
    const col = P.cat[n.category % P.cat.length];
    const m = new T.Mesh(geo[n.category] || geo[2], new T.MeshStandardMaterial({ color: col, roughness: 0.45, metalness: 0.15,
      emissive: col, emissiveIntensity: 0.08 }));
    m.position.copy(pos.get(n.id));
    m.userData = { id: n.id, pick: n.category >= 2, base: col,
      tip: `<b>${esc(n.name)}</b><br><span style="opacity:.75">${esc(data.categories[n.category]?.name || "")}${n.expr ? " · " + esc(n.expr) : ""}</span>` +
        (n.reason ? `<br><span style="opacity:.7">${esc(n.reason)}</span>` : "") };
    st.root.add(m); st.pickables.push(m); meshes.set(n.id, m);
    const lb = label(n.name, { size: 20 }); lb.position.copy(m.position).add(new T.Vector3(0, 2.9, 0)); st.root.add(lb);
  });
  // 层标签
  Object.keys(layers).forEach(k => {
    const lb = label(data.categories[k]?.name || "", { size: 30, color: "#" + P.cat[k % P.cat.length].toString(16).padStart(6, "0") });
    const zs = layers[k].map(n => pos.get(n.id)); const maxY = Math.max(...zs.map(p => p.y));
    lb.position.set(0, maxY + 8, (Number(k) - 2) * layerZ); st.root.add(lb);
  });
  // 边：二次曲线 + 流动光点
  const flows = [];
  const lineMat = new T.LineBasicMaterial({ color: P.edge, transparent: true, opacity: 0.45 });
  links.forEach(l => {
    const a = pos.get(l.source), b = pos.get(l.target); if (!a || !b) return;
    const mid = a.clone().add(b).multiplyScalar(0.5).add(new T.Vector3(0, 3 + a.distanceTo(b) * 0.08, 0));
    const curve = new T.QuadraticBezierCurve3(a, mid, b);
    const line = new T.Line(new T.BufferGeometry().setFromPoints(curve.getPoints(24)), lineMat.clone());
    line.userData = { s: l.source, t: l.target }; st.root.add(line);
    if (!reduceMotion()) {
      const dot = new T.Mesh(new T.SphereGeometry(0.32, 8, 8), new T.MeshBasicMaterial({ color: P.cat[3], transparent: true, opacity: 0.9 }));
      st.root.add(dot); flows.push({ curve, dot, t: Math.random(), line });
    } else flows.push({ curve, line });
  });
  st.onFrame = () => flows.forEach(f => { if (f.dot) { f.t = (f.t + 0.006) % 1; f.dot.position.copy(f.curve.getPoint(f.t)); } });
  // 选中高亮：上游 + 下游
  const up = new Map(), down = new Map();
  links.forEach(l => { (down.get(l.source) || down.set(l.source, []).get(l.source)).push(l.target); (up.get(l.target) || up.set(l.target, []).get(l.target)).push(l.source); });
  const closure = (id, g) => { const s = new Set([id]), q = [id]; while (q.length) for (const x of g.get(q.pop()) || []) if (!s.has(x)) { s.add(x); q.push(x); } return s; };
  function highlight(id) {
    const keep = id ? new Set([...closure(id, up), ...closure(id, down)]) : null;
    meshes.forEach((m, k) => { const on = !keep || keep.has(k); m.material.opacity = on ? 1 : 0.18; m.material.transparent = !on;
      m.material.emissiveIntensity = k === id ? 0.55 : 0.08; m.scale.setScalar(k === id ? 1.35 : 1); });
    flows.forEach(f => { const on = !keep || (keep.has(f.line.userData.s) && keep.has(f.line.userData.t));
      f.line.material.opacity = on ? (keep ? 0.9 : 0.45) : 0.06; if (f.dot) f.dot.visible = on; });
  }
  st.clickCb = o => { highlight(o.userData.id); if (o.userData.pick && onPick) onPick(o.userData.id); };
  if (focus) highlight(focus);
  st.highlight = highlight;
  return st;
}

/* ------------------------------------------------------------------ 2. 单指标三维柱 */
export async function bars(el, { title, categories, values, series = false, unit = "" }) {
  await ready();
  const P = palette(); const n = categories.length;
  const st = new Stage(el, { dist: Math.max(46, n * 4.2), pitch: 0.48, yaw: 0.55, target: [0, 6, 0] });
  const max = Math.max(...values.map(v => Math.abs(v || 0)), 1), H = 18, w = 2.6, gap = 4.2;
  const grid = new T.GridHelper(Math.max(30, n * gap + 10), Math.max(10, n + 4), P.grid, P.grid);
  grid.material.transparent = true; grid.material.opacity = 0.6; st.root.add(grid);
  const order = values.map((v, i) => i).sort((a, b) => (values[b] || 0) - (values[a] || 0));
  const rank = new Map(order.map((i, r) => [i, r]));
  const tops = [];
  categories.forEach((c, i) => {
    const v = values[i] || 0, h = Math.max(0.15, Math.abs(v) / max * H), x = (i - (n - 1) / 2) * gap;
    const col = series ? P.cat[2] : (rank.get(i) === 0 ? P.cat[2] : P.cat[0]);
    const m = new T.Mesh(new T.BoxGeometry(w, 1, w), new T.MeshStandardMaterial({ color: col, roughness: 0.4, metalness: 0.1 }));
    m.scale.y = 0.001; m.position.set(x, 0, 0); m.userData = { tip: `<b>${esc(c)}</b><br>${esc(title)}：${fmt(v)}${esc(unit)}`, h };
    st.root.add(m); st.pickables.push(m); tops.push(new T.Vector3(x, h, 0));
    const lb = label(c, { size: 18, bg: false, color: P.sub }); lb.position.set(x, -1.2, w); st.root.add(lb);
    const vl = label(fmt(v), { size: 18 }); vl.position.set(x, h + 1.4, 0); vl.userData.vh = h; st.root.add(vl);
    m.userData.vl = vl;
  });
  if (series && tops.length > 1) {
    const curve = new T.CatmullRomCurve3(tops.map(p => p.clone().add(new T.Vector3(0, 0.4, 0))));
    st.root.add(new T.Mesh(new T.TubeGeometry(curve, tops.length * 12, 0.22, 8, false),
      new T.MeshStandardMaterial({ color: P.cat[1], emissive: P.cat[1], emissiveIntensity: 0.2 })));
  }
  const tl = label(title, { size: 30 }); tl.position.set(0, H + 6, 0); st.root.add(tl);
  // 生长动画
  const t0 = performance.now(), dur = reduceMotion() ? 0 : 700;
  st.onFrame = () => { const k = dur ? Math.min(1, (performance.now() - t0) / dur) : 1, e = 1 - Math.pow(1 - k, 3);
    st.pickables.forEach(m => { const h = m.userData.h * e; m.scale.y = Math.max(0.001, h); m.position.y = h / 2; m.userData.vl.position.y = h + 1.4; }); };
  return st;
}

/* ------------------------------------------------------------------ 3. 多指标 × 维度 三维柱阵 */
export async function matrix(el, { rows, cols, values, units = [] }) {
  // rows: 指标名[]；cols: 维度取值[]；values[r][c]
  await ready();
  const P = palette(), R = rows.length, C = cols.length, gx = 4, gz = 5, H = 14, w = 2.4;
  const st = new Stage(el, { dist: Math.max(70, Math.max(C * gx, R * gz) * 1.7 + 30), pitch: 0.55, yaw: 0.6, target: [0, 5, 0] });
  const grid = new T.GridHelper(Math.max(C * gx, R * gz) + 12, Math.max(C, R) + 6, P.grid, P.grid);
  grid.material.transparent = true; grid.material.opacity = 0.5; st.root.add(grid);
  const all = [];
  rows.forEach((rname, r) => {
    const rowMax = Math.max(...values[r].map(v => Math.abs(v || 0)), 1e-9);
    const z = (r - (R - 1) / 2) * gz, col = P.cat[(r % 4) + 1 > 4 ? 2 : (r % 4) + 1];
    values[r].forEach((v, c) => {
      if (v == null || isNaN(v)) return;
      const h = Math.max(0.12, Math.abs(v) / rowMax * H), x = (c - (C - 1) / 2) * gx;
      const m = new T.Mesh(new T.BoxGeometry(w, 1, w), new T.MeshStandardMaterial({ color: col, roughness: 0.4, transparent: true, opacity: 0.92 }));
      m.position.set(x, 0, z); m.userData = { h, tip: `<b>${esc(rname)}</b> · ${esc(cols[c])}<br>${fmt(v)}${esc(units[r] || "")}<br><span style="opacity:.7">本行最大值归一：${(Math.abs(v) / rowMax * 100).toFixed(0)}%</span>` };
      st.root.add(m); st.pickables.push(m); all.push(m);
    });
    const lb = label(rname, { size: 20 }); lb.position.set(-(C - 1) / 2 * gx - 7, 1.5, z); st.root.add(lb);
  });
  cols.forEach((cname, c) => { const lb = label(cname, { size: 18, bg: false, color: P.sub }); lb.position.set((c - (C - 1) / 2) * gx, -0.8, (R - 1) / 2 * gz + 4); st.root.add(lb); });
  const note = label("每个指标按自身最大值归一（单位不同，高度只在行内可比）", { size: 18, bg: false, color: P.sub });
  note.position.set(0, H + 5, 0); st.root.add(note);
  const t0 = performance.now(), dur = reduceMotion() ? 0 : 800;
  st.onFrame = () => { const k = dur ? Math.min(1, (performance.now() - t0) / dur) : 1, e = 1 - Math.pow(1 - k, 3);
    all.forEach(m => { const h = m.userData.h * e; m.scale.y = Math.max(0.001, h); m.position.y = h / 2; }); };
  // 悬停同列高亮
  st.hoverCb = (o) => all.forEach(m => { m.material.opacity = !o || Math.abs(m.position.x - o.position.x) < 0.1 || Math.abs(m.position.z - o.position.z) < 0.1 ? 0.95 : 0.22; });
  return st;
}

/* ------------------------------------------------------------------ 4. 通用三维力导向图（本体图 / 实例图） */
// data: {nodes:[{id,label,kind,group,title,focus}], edges:[{from,to,label,kind}]}
export async function graph3d(el, data, { onClick, onDblClick, colors } = {}) {
  await ready();
  const P = palette(), N = data.nodes.length;
  const groups = [...new Set(data.nodes.map(n => n.group))];
  const gcol = g => colors ? new T.Color(colors(g)).getHex() : P.cat[1 + (groups.indexOf(g) % 4)];
  const st = new Stage(el, { dist: Math.max(70, Math.sqrt(N) * 16), pitch: 0.3, yaw: 0.4 });
  const R = Math.max(20, Math.cbrt(N) * 14);
  const nodes = data.nodes.map((n, i) => {
    const phi = Math.acos(1 - 2 * (i + 0.5) / N), th = Math.PI * (1 + Math.sqrt(5)) * i;
    return { ...n, p: new T.Vector3(R * Math.sin(phi) * Math.cos(th), R * Math.cos(phi), R * Math.sin(phi) * Math.sin(th)), v: new T.Vector3() };
  });
  const idx = new Map(nodes.map((n, i) => [n.id, i]));
  const edges = data.edges.map(e => ({ ...e, s: idx.get(e.from), t: idx.get(e.to) })).filter(e => e.s != null && e.t != null);
  // 节点
  const geoC = new T.BoxGeometry(4.4, 2.4, 2.4), geoP = new T.SphereGeometry(0.75, 12, 10), geoI = new T.SphereGeometry(1.25, 16, 12);
  const showLabels = N <= 260;
  const k = Math.max(8, R / Math.cbrt(Math.max(N, 1)) * 1.6), tmp = new T.Vector3(), SC = N > 150 ? 0.8 : 1.3;
  nodes.forEach(n => {
    const col = n.kind === "property" ? P.cat[0] : gcol(n.group);
    n.mesh = new T.Mesh(n.kind === "class" ? geoC : n.kind === "property" ? geoP : geoI,
      new T.MeshStandardMaterial({ color: col, roughness: 0.45, metalness: 0.1, emissive: col, emissiveIntensity: n.focus ? 0.6 : 0.08 }));
    n.mesh.userData = { node: n, pick: true, tip: `<b>${esc(n.label)}</b>${n.title ? "<br><span style='opacity:.75'>" + esc(n.title) + "</span>" : ""}` };
    n.mesh.scale.setScalar(SC * (n.focus ? 1.6 : 1));
    st.root.add(n.mesh); st.pickables.push(n.mesh);
    if (showLabels || n.kind === "class" || n.focus) {
      n.lb = label(n.label, { size: n.kind === "class" ? 22 : 16, bg: n.kind === "class" });
      n.lb.scale.multiplyScalar(SC);
      st.root.add(n.lb);
    }
  });
  // 边（一个 LineSegments 批量绘制）
  const pos = new Float32Array(edges.length * 6), colArr = new Float32Array(edges.length * 6);
  const cObj = new T.Color(P.edge), cHas = new T.Color(P.grid), cRel = new T.Color(P.cat[2]);
  edges.forEach((e, i) => { const c = e.kind === "has_property" ? cHas : e.kind === "object_property" ? cRel : cObj;
    for (let k = 0; k < 2; k++) { colArr[i * 6 + k * 3] = c.r; colArr[i * 6 + k * 3 + 1] = c.g; colArr[i * 6 + k * 3 + 2] = c.b; } });
  const lg = new T.BufferGeometry(); lg.setAttribute("position", new T.BufferAttribute(pos, 3)); lg.setAttribute("color", new T.BufferAttribute(colArr, 3));
  const lines = new T.LineSegments(lg, new T.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.6 }));
  st.root.add(lines);
  const elabels = edges.filter(e => e.kind === "object_property" && e.label && edges.length < 200).map(e => { const l = label(e.label, { size: 15, bg: false, color: "#" + P.cat[2].toString(16).padStart(6, "0") }); st.root.add(l); return [e, l]; });
  // 力模拟（O(n²) 斥力，大图降频）
  let heat = 1;
  function step() {
    for (let i = 0; i < N; i++) { const a = nodes[i];
      for (let j = i + 1; j < N; j++) { const b = nodes[j]; tmp.subVectors(a.p, b.p); const d2 = tmp.lengthSq() + 0.01; if (d2 > k * k * 25) continue;
        tmp.multiplyScalar(k * k / d2 * 0.05); a.v.add(tmp); b.v.sub(tmp); } }
    edges.forEach(e => { const a = nodes[e.s], b = nodes[e.t]; tmp.subVectors(b.p, a.p); const d = tmp.length() || 1;
      const want = e.kind === "has_property" ? k * 0.5 : k * 1.2; tmp.multiplyScalar((d - want) / d * 0.04); a.v.add(tmp); b.v.sub(tmp); });
    nodes.forEach(n => { if (n.fixed) { n.v.set(0, 0, 0); return; } n.v.addScaledVector(n.p, -0.004); n.v.clampLength(0, 3); n.p.addScaledVector(n.v, heat); n.v.multiplyScalar(0.6); });
    heat = Math.max(0.02, heat * 0.985);
  }
  function sync() {
    // 展示坐标归一：布局半径会漂移，显示时缩放到固定半径 RD，节点大小与间距保持比例
    const c = new T.Vector3(); nodes.forEach(n => c.add(n.p)); c.divideScalar(Math.max(N, 1));
    let r = 1; nodes.forEach(n => { r = Math.max(r, n.p.distanceTo(c)); });
    const RD = 14 + Math.cbrt(N) * 7, f = RD / r;
    nodes.forEach(n => { n.d = (n.d || new T.Vector3()).copy(n.p).sub(c).multiplyScalar(f); });
    nodes.forEach(n => { n.mesh.position.copy(n.d); if (n.lb) n.lb.position.copy(n.d).add(new T.Vector3(0, (n.kind === "class" ? 2.8 : 1.8) * SC, 0)); });
    edges.forEach((e, i) => { const a = nodes[e.s].d, b = nodes[e.t].d; pos.set([a.x, a.y, a.z, b.x, b.y, b.z], i * 6); });
    lg.attributes.position.needsUpdate = true; lg.computeBoundingSphere();
    elabels.forEach(([e, l]) => l.position.copy(nodes[e.s].d).add(nodes[e.t].d).multiplyScalar(0.5));
  }
  let frames = 0;
  const sphere = () => { const s = new T.Sphere(new T.Vector3(), 1); nodes.forEach(n => { if (n.d) s.radius = Math.max(s.radius, n.d.length()); }); return s; };
  const camDist = s => Math.max(30, (s.radius + 4) / Math.tan((st.camera.fov * Math.PI / 180) / 2) * 1.05);
  const fit = () => {                       // 用户未交互前，镜头持续对准整张图
    if (st.last) return;
    const s = sphere();
    st.target.lerp(s.center, 0.25);
    st.dist += (camDist(s) - st.dist) * 0.25;
  };
  st.onFrame = () => { if (heat > 0.03 || frames < 30) { const it = N > 600 ? 1 : N > 200 ? 2 : 4; for (let i = 0; i < it; i++) step(); sync(); frames++; } fit(); };
  for (let i = 0; i < 120; i++) step(); sync();
  { st.last = 0; const s = sphere(); st.target.copy(s.center); st.dist = camDist(s); }
  // 交互：单击选中（高亮邻居），双击回调（展开/编辑）
  const adj = new Map(); edges.forEach(e => { (adj.get(e.s) || adj.set(e.s, new Set()).get(e.s)).add(e.t); (adj.get(e.t) || adj.set(e.t, new Set()).get(e.t)).add(e.s); });
  let lastClick = { t: 0, id: null };
  st.clickCb = o => {
    const n = o.userData.node, i = idx.get(n.id), keep = new Set([i, ...(adj.get(i) || [])]);
    nodes.forEach((m, j) => { const on = keep.has(j); m.mesh.material.transparent = !on; m.mesh.material.opacity = on ? 1 : 0.15; m.mesh.material.emissiveIntensity = j === i ? 0.6 : 0.08; });
    const now = performance.now();
    if (lastClick.id === n.id && now - lastClick.t < 350) { if (onDblClick) onDblClick(n); } else if (onClick) onClick(n);
    lastClick = { t: now, id: n.id };
  };
  st.reset = () => nodes.forEach(m => { m.mesh.material.transparent = false; m.mesh.material.opacity = 1; m.mesh.material.emissiveIntensity = m.focus ? 0.6 : 0.08; });
  return { stage: st, groups, col: g => "#" + gcol(g).toString(16).padStart(6, "0"), dispose: () => st.dispose() };
}
