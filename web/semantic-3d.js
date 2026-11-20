/* semantic-3d.js
 * 本体语义层工作台 · three.js 3D 知识图谱（离线自包含 / 真实数据 / 可回退）
 *
 * 设计约定（与项目既有口径一致）：
 * - three.js 只从本地 vendor/three.module.min.js 动态 import，零 CDN、零外网依赖；
 * - 所有节点与边都来自真实后端 API（/schema /inheritance /instances /relations /violations）；
 *   星尘粒子是纯装饰，HUD 明确标注，不冒充业务数据；
 * - 数据为空时照实显示「0 个真实实例」，只渲染 Schema 类型层，绝不编造节点；
 * - WebGL 不可用 / vendor 缺失 → 抛出明确错误，页面保留 2D SVG 视图兜底；
 * - 尊重 prefers-reduced-motion：停用自转、脉冲与流动光点，交互仍可用。
 *
 * 对外接口：mount(opts) / refresh() / setFilter(f) / setVersion(v) / ready() / dispose()
 */
const VENDOR_URLS = [
  "/console/vendor/three.module.min.js",
  "./vendor/three.module.min.js",
  "/vendor/three.module.min.js",
];

let T = null;
let haloTexture = null;

const S = {
  canvas: null, hudEl: null, detailEl: null,
  api: "/api/v1/ontology/semantic",
  renderer: null, scene: null, camera: null, root: null, dust: null,
  structure: null, dynamic: null,
  raf: 0, lastTs: 0, clock: 0,
  disposed: false, initialized: false,
  nodeMeshes: [], flows: [], labels: [],
  yaw: 0.35, pitch: 0.16, dist: 86,
  drag: false, lastX: 0, lastY: 0, lastInteract: 0,
  reduce: false, ro: null,
  data: { types: [], inheritance: {}, inheritancePairs: [],
          instances: [], relations: [], violations: [] },
  filter: "all", version: "",
};

async function loadThree() {
  if (T) return T;
  for (const u of VENDOR_URLS) {
    try {
      const m = await import(u);
      if (m && m.WebGLRenderer) { T = m; return T; }
    } catch (e) { /* 换下一个候选路径 */ }
  }
  throw new Error("three.js 本地 vendor 加载失败：请确认 /console/vendor/three.module.min.js 存在");
}

async function jget(path) {
  const r = await fetch(S.api + path);
  if (!r.ok) throw new Error(path + " HTTP " + r.status);
  return r.json();
}
function hud(text) { if (S.hudEl) S.hudEl.textContent = text; }
function detail(text) { if (S.detailEl) S.detailEl.textContent = text; }

function typeColor(t) {
  if (/Person|Employee|Manager/.test(t)) return 0x185fa5;
  if (/Organization|Department|Team/.test(t)) return 0x0f6e56;
  if (/Project/.test(t)) return 0xba7517;
  return 0x6b4ea8;
}

function makeHaloTexture() {
  if (haloTexture) return haloTexture;
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(64, 64, 2, 64, 64, 62);
  grad.addColorStop(0, "rgba(255,255,255,0.95)");
  grad.addColorStop(0.25, "rgba(255,255,255,0.35)");
  grad.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  haloTexture = new T.CanvasTexture(c);
  return haloTexture;
}

function makeLabel(text, tint) {
  const c = document.createElement("canvas");
  c.width = 512; c.height = 96;
  const g = c.getContext("2d");
  g.clearRect(0, 0, 512, 96);
  g.fillStyle = "rgba(255,255,255,0.88)";
  g.strokeStyle = "rgba(120,135,155,0.55)";
  g.lineWidth = 2;
  const w = Math.min(500, 22 + String(text).length * 15);
  const x = (512 - w) / 2;
  g.beginPath();
  if (g.roundRect) g.roundRect(x, 12, w, 70, 16);
  else g.rect(x, 12, w, 70);
  g.fill(); g.stroke();
  g.fillStyle = tint;
  g.font = "600 34px -apple-system,'PingFang SC','Microsoft YaHei',sans-serif";
  g.textAlign = "center"; g.textBaseline = "middle";
  g.fillText(String(text).slice(0, 24), 256, 48);
  const tex = new T.CanvasTexture(c);
  const sp = new T.Sprite(new T.SpriteMaterial({ map: tex, transparent: true, depthTest: false }));
  sp.scale.set(w / 512 * 14, 2.6, 1);
  sp.userData.ownedTexture = tex;
  return sp;
}

function addLine(group, a, b, color, opacity, dashed) {
  const geo = new T.BufferGeometry().setFromPoints([a, b]);
  const mat = dashed
    ? new T.LineDashedMaterial({ color, transparent: true, opacity, dashSize: 1.4, gapSize: 1.0 })
    : new T.LineBasicMaterial({ color, transparent: true, opacity });
  const line = new T.Line(geo, mat);
  if (dashed) line.computeLineDistances();
  group.add(line);
  return line;
}

function addTube(group, a, b, color, radius) {
  const curve = new T.LineCurve3(a, b);
  const geo = new T.TubeGeometry(curve, 1, radius, 6, false);
  const mat = new T.MeshBasicMaterial({ color, transparent: true, opacity: 0.92 });
  const mesh = new T.Mesh(geo, mat);
  group.add(mesh);
  return mesh;
}

function addNode(group, p, color, radius, node, labelText, labelTint) {
  const geo = new T.SphereGeometry(radius, 24, 16);
  const mat = new T.MeshStandardMaterial({
    color, emissive: color, emissiveIntensity: 0.32,
    roughness: 0.42, metalness: 0.12, transparent: true, opacity: 0.96,
  });
  const mesh = new T.Mesh(geo, mat);
  mesh.position.copy(p);
  mesh.userData.node = node;
  group.add(mesh);
  S.nodeMeshes.push(mesh);

  const halo = new T.Sprite(new T.SpriteMaterial({
    map: makeHaloTexture(), color, transparent: true,
    opacity: 0.42, depthWrite: false, blending: T.AdditiveBlending,
  }));
  halo.position.copy(p);
  halo.scale.setScalar(radius * 4.2);
  group.add(halo);

  if (labelText) {
    const lab = makeLabel(labelText, labelTint || "#1c2330");
    lab.position.copy(p).add(new T.Vector3(0, radius + 2.0, 0));
    group.add(lab);
    S.labels.push(lab);
  }
  return mesh;
}

function disposeGroup(g) {
  if (!g) return;
  g.traverse((o) => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) {
      const ms = Array.isArray(o.material) ? o.material : [o.material];
      ms.forEach((m) => {
        if (m.map && m.map !== haloTexture) m.map.dispose();
        m.dispose();
      });
    }
  });
  while (g.children.length) g.remove(g.children[0]);
}

/* 确定性球面分层布局：根节点在内圈，子类逐层外扩；实例围绕所属类型分布。 */
function layout() {
  const d = S.data, pos = {};
  const golden = Math.PI * (3 - Math.sqrt(5));
  const layers = {};
  d.types.forEach((t) => {
    const a = (d.inheritance[t] || {}).ancestors || [t];
    const depth = Math.max(0, a.length - 1);
    (layers[depth] = layers[depth] || []).push(t);
  });
  Object.keys(layers).forEach((k) => {
    const arr = layers[k], n = arr.length, R = 10 + Number(k) * 9.5;
    arr.forEach((t, i) => {
      const y = n === 1 ? 0 : 1 - (i / (n - 1)) * 2;
      const rr = Math.sqrt(Math.max(0, 1 - y * y));
      const th = golden * i + Number(k) * 0.8;
      pos["T:" + t] = new T.Vector3(Math.cos(th) * rr * R, y * R * 0.78, Math.sin(th) * rr * R);
    });
  });
  const seen = {};
  const ver = S.version;
  const insts = d.instances.filter((i) => !ver || i.ontology_version === ver);
  insts.forEach((inst) => {
    const tn = inst.type_name || "";
    const base = pos["T:" + tn] || new T.Vector3(0, 34, 0);
    const idx = (seen[tn] = (seen[tn] || 0) + 1);
    const a = idx * golden + 0.9;
    const rr = 5.6 + (idx % 3) * 1.3;
    pos[inst.id] = new T.Vector3(
      base.x + Math.cos(a) * rr,
      base.y + Math.sin(a * 1.4) * rr * 0.72,
      base.z + Math.sin(a) * rr,
    );
  });
  return { pos, insts };
}

async function fetchData() {
  const [i, r, v, s, inh] = await Promise.all([
    jget("/instances"), jget("/relations"), jget("/violations"),
    jget("/schema"), jget("/inheritance"),
  ]);
  const pairs = [];
  (s.inheritance || []).forEach((row) => {
    Object.entries(row || {}).forEach(([child, parent]) => pairs.push([child, parent]));
  });
  S.data = {
    types: s.entity_types || [],
    inheritance: inh.inheritance || {},
    inheritancePairs: pairs,
    instances: i.items || [],
    relations: r.items || [],
    violations: v.violations || [],
  };
}

function rebuild() {
  if (!S.initialized) return;
  disposeGroup(S.structure);
  disposeGroup(S.dynamic);
  S.nodeMeshes = [];
  S.flows = [];
  S.labels = [];

  const d = S.data;
  const { pos, insts } = layout();
  const violIds = new Set(d.violations.map((v) => v.id));

  /* 结构层：类型节点 + 继承边（真实 Schema 定义） */
  d.types.forEach((t) => {
    const p = pos["T:" + t];
    if (!p) return;
    const col = typeColor(t);
    addNode(S.structure, p, col, 2.15,
      { kind: "type", id: t, name: t, type_name: t }, t, "#183153");
  });
  d.inheritancePairs.forEach(([child, parent]) => {
    const a = pos["T:" + child], b = pos["T:" + parent];
    if (a && b) addLine(S.structure, a, b, 0x9db4cd, 0.55, false);
  });

  /* 实例层：真实实例节点 + 归属边 */
  insts.forEach((inst) => {
    const p = pos[inst.id];
    if (!p) return;
    const col = typeColor(inst.type_name || "");
    addNode(S.structure, p, col, 1.35,
      { kind: "instance", ...inst },
      inst.canonical_name || inst.type_name, "#1c2330");
    const tp = pos["T:" + (inst.type_name || "")];
    if (tp) addLine(S.structure, p, tp, 0xb9c4d2, 0.24, false);
  });

  /* 关系层：真实关系边，违规红色加粗、推理衍生灰色虚线、正常蓝色 */
  let edges = d.relations.filter((r) => !S.version || r.ontology_version === S.version);
  if (S.filter === "violation") edges = edges.filter((r) => violIds.has(r.id));
  if (S.filter === "derived") edges = edges.filter((r) => r.derived);
  edges.forEach((e) => {
    const a = pos[e.source_id], b = pos[e.target_id];
    if (!a || !b) return;
    const bad = violIds.has(e.id);
    if (bad) addTube(S.dynamic, a, b, 0xa32d2d, 0.3);
    else if (e.derived) addLine(S.dynamic, a, b, 0x9aa5b5, 0.85, true);
    else addLine(S.dynamic, a, b, 0x5b7fa6, 0.8, false);

    /* 沿边流动的光点（纯动画，不改变数据语义） */
    const gm = new T.SphereGeometry(bad ? 0.52 : 0.38, 10, 8);
    const mm = new T.MeshBasicMaterial({ color: bad ? 0xff5a5a : (e.derived ? 0xb6c0ce : 0x7fb2ff) });
    const dot = new T.Mesh(gm, mm);
    S.dynamic.add(dot);
    S.flows.push({ dot, a: a.clone(), b: b.clone(), offset: (S.flows.length * 0.37) % 1, speed: 0.22 });
  });

  const nViol = edges.filter((e) => violIds.has(e.id)).length;
  const nDer = edges.filter((e) => e.derived).length;
  hud([
    `类型 ${d.types.length} · 真实实例 ${insts.length} · 关系边 ${edges.length}`,
    `违规红边 ${nViol} · 推理衍生虚线 ${nDer} · 版本 ${S.version || "全部"}`,
    "拖拽旋转 · 滚轮缩放 · 点击节点看详情 · 星尘为装饰粒子",
  ].join("   |   "));
  return { insts: insts.length, edges: edges.length, violations: nViol };
}

function resize() {
  if (!S.renderer || !S.canvas) return;
  const w = S.canvas.clientWidth || 1000, h = S.canvas.clientHeight || 460;
  S.renderer.setSize(w, h, false);
  S.camera.aspect = w / h;
  S.camera.updateProjectionMatrix();
}

function frame(ts) {
  if (S.disposed) return;
  const dt = S.lastTs ? Math.min(0.05, (ts - S.lastTs) / 1000) : 0.016;
  S.lastTs = ts;
  S.clock += dt;
  if (!S.reduce) {
    if (!S.drag && ts - S.lastInteract > 2600) S.yaw += dt * 0.11;
    S.nodeMeshes.forEach((m, i) => m.scale.setScalar(1 + Math.sin(S.clock * 1.7 + i * 0.7) * 0.05));
    S.flows.forEach((f) => {
      const t = (S.clock * f.speed + f.offset) % 1;
      f.dot.position.lerpVectors(f.a, f.b, t);
      f.dot.scale.setScalar(0.75 + Math.sin(t * Math.PI) * 0.6);
    });
    if (S.dust) S.dust.rotation.y -= dt * 0.014;
  }
  S.root.rotation.y = S.yaw;
  S.root.rotation.x = S.pitch;
  S.camera.position.set(0, 0, S.dist);
  S.camera.lookAt(0, 0, 0);
  S.renderer.render(S.scene, S.camera);
  S.raf = requestAnimationFrame(frame);
}

function bindInteractions() {
  const cv = S.canvas;
  const down = (e) => {
    S.drag = true; S.lastX = e.clientX; S.lastY = e.clientY;
    S.lastInteract = performance.now();
    cv.style.cursor = "grabbing";
  };
  const move = (e) => {
    if (!S.drag) return;
    S.yaw += (e.clientX - S.lastX) * 0.006;
    S.pitch = Math.max(-1.2, Math.min(1.2, S.pitch + (e.clientY - S.lastY) * 0.005));
    S.lastX = e.clientX; S.lastY = e.clientY;
    S.lastInteract = performance.now();
  };
  const up = () => { S.drag = false; cv.style.cursor = "grab"; S.lastInteract = performance.now(); };
  const wheel = (e) => {
    e.preventDefault();
    S.dist = Math.max(34, Math.min(190, S.dist + e.deltaY * 0.08));
    S.lastInteract = performance.now();
  };
  const click = (e) => {
    if (Math.abs(e.clientX - S.lastX) + Math.abs(e.clientY - S.lastY) > 6) return;
    const rect = cv.getBoundingClientRect();
    const mouse = new T.Vector2(
      ((e.clientX - rect.left) / rect.width) * 2 - 1,
      -((e.clientY - rect.top) / rect.height) * 2 + 1,
    );
    const rc = new T.Raycaster();
    rc.setFromCamera(mouse, S.camera);
    const hits = rc.intersectObjects(S.nodeMeshes, false);
    if (!hits.length) { detail("未选中节点；点击球体可查看真实实例/类型详情。"); return; }
    const n = hits[0].object.userData.node || {};
    if (n.kind === "type") {
      const anc = (S.data.inheritance[n.id] || {}).ancestors || [n.id];
      detail(`[类型] ${n.id}\n继承链：${anc.join(" → ")}\n来源：GET /ontology/semantic/schema + /inheritance`);
    } else {
      detail([
        `[实例] ${n.canonical_name || "(未命名)"}`,
        `id：${n.id}`,
        `类型：${n.type_name}`,
        `版本：${n.ontology_version || "—"}`,
        `空间：${n.space_id || "—"}`,
        `关联 Fact：${(n.source_fact_ids || []).length} 个`,
        `来源：GET /ontology/semantic/instances`,
      ].join("\n"));
    }
  };
  cv.addEventListener("pointerdown", down);
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  cv.addEventListener("wheel", wheel, { passive: false });
  cv.addEventListener("click", click);
  cv.addEventListener("webglcontextlost", (e) => { e.preventDefault(); hud("WebGL 上下文丢失，请切回 2D 视图或刷新页面"); });
  S._off = () => {
    cv.removeEventListener("pointerdown", down);
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    cv.removeEventListener("wheel", wheel);
    cv.removeEventListener("click", click);
  };
}

export async function mount(opts = {}) {
  S.canvas = opts.canvas;
  S.hudEl = opts.hud || null;
  S.detailEl = opts.detail || null;
  S.api = opts.api || S.api;
  S.reduce = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  if (!S.canvas) throw new Error("3D 画布不存在");
  if (!window.WebGLRenderingContext) throw new Error("当前浏览器不支持 WebGL");
  await loadThree();

  S.renderer = new T.WebGLRenderer({ canvas: S.canvas, antialias: true, alpha: true });
  S.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  S.renderer.setClearColor(0x000000, 0);
  if (T.SRGBColorSpace) S.renderer.outputColorSpace = T.SRGBColorSpace;
  S.scene = new T.Scene();
  S.camera = new T.PerspectiveCamera(50, 2, 0.1, 600);
  S.root = new T.Group();
  S.structure = new T.Group();
  S.dynamic = new T.Group();
  S.root.add(S.structure, S.dynamic);
  S.scene.add(S.root);

  S.scene.add(new T.AmbientLight(0xffffff, 1.15));
  const key = new T.PointLight(0xffffff, 1.5, 400);
  key.position.set(40, 55, 80);
  S.scene.add(key);
  const fill = new T.DirectionalLight(0x9fc4ff, 0.6);
  fill.position.set(-60, -20, -40);
  S.scene.add(fill);

  /* 装饰性星尘（明确标注，非业务数据） */
  const N = 420, arr = new Float32Array(N * 3);
  for (let i = 0; i < N; i++) {
    const th = Math.random() * Math.PI * 2, ph = Math.acos(2 * Math.random() - 1);
    const r = 70 + Math.random() * 90;
    arr[i * 3] = r * Math.sin(ph) * Math.cos(th);
    arr[i * 3 + 1] = r * Math.cos(ph) * 0.65;
    arr[i * 3 + 2] = r * Math.sin(ph) * Math.sin(th);
  }
  const dg = new T.BufferGeometry();
  dg.setAttribute("position", new T.BufferAttribute(arr, 3));
  S.dust = new T.Points(dg, new T.PointsMaterial({
    color: 0x8ea8c8, size: 0.55, transparent: true, opacity: 0.5,
    sizeAttenuation: true, depthWrite: false,
  }));
  S.scene.add(S.dust);

  S.initialized = true;
  S.disposed = false;
  resize();
  if (window.ResizeObserver) {
    S.ro = new ResizeObserver(resize);
    S.ro.observe(S.canvas);
  } else {
    window.addEventListener("resize", resize);
  }
  bindInteractions();
  S.canvas.style.cursor = "grab";
  S.raf = requestAnimationFrame(frame);
  detail("3D 视图已就绪；点击球体节点查看真实数据详情。");
  return true;
}

export async function refresh(opts = {}) {
  if (S.disposed) return null;
  if (!S.initialized) throw new Error("3D 尚未 mount");
  if (opts.filter !== undefined) S.filter = opts.filter || "all";
  if (opts.version !== undefined) S.version = opts.version || "";
  await fetchData();
  return rebuild();
}

export function setFilter(f) { S.filter = f || "all"; return rebuild(); }
export function setVersion(v) { S.version = v || ""; return rebuild(); }
export function ready() { return S.initialized && !S.disposed; }

export function dispose() {
  if (S.disposed) return;
  S.disposed = true; S.initialized = false;
  cancelAnimationFrame(S.raf);
  if (S._off) S._off();
  if (S.ro) S.ro.disconnect();
  else window.removeEventListener("resize", resize);
  disposeGroup(S.structure);
  disposeGroup(S.dynamic);
  if (S.dust) { S.dust.geometry.dispose(); S.dust.material.dispose(); }
  if (S.renderer) S.renderer.dispose();
  S.nodeMeshes = []; S.flows = []; S.labels = [];
}
