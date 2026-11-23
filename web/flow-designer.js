/* flow-designer.js — 本体工作台 React Flow 层（React 18 + reactflow 11 UMD，本地 vendor，零 CDN）
 *
 * window.RF = {
 *   pipeline(el, data, {onOpen})            建模流水线 DAG：阶段状态/耗时/产物；点击节点跳到对应标签
 *   designer(el, job, {api, onChanged})     本体设计图：类节点（含属性列表）+ 关系边
 *      增：拖拽类节点的连接点到另一个类 → 选外键列 → 新增关系
 *      删：选中节点/边按 Delete，或在侧栏点「删除」
 *      改：双击/侧栏编辑 中文名、描述、数据类型、改名
 *      查：搜索框高亮类/属性；小地图、缩放、自动布局、保存布局、导出 PNG/SVG/JSON
 * }
 */
(function () {
  const BASES = ["/console/vendor/", "./vendor/"];
  let loading = null;
  const roots = new WeakMap();
  const load = (src) => new Promise((ok, bad) => { const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = () => bad(new Error("加载失败：" + src)); document.head.appendChild(s); });
  async function ready() {
    if (window.ReactFlow && window.React && window.ReactDOM) return;
    if (loading) return loading;
    loading = (async () => {
      for (const b of BASES) {
        try {
          if (!document.querySelector('link[data-rf]')) { const l = document.createElement("link"); l.rel = "stylesheet"; l.href = b + "reactflow.css"; l.dataset.rf = 1; document.head.appendChild(l); }
          if (!window.React) await load(b + "react.production.min.js");
          if (!window.ReactDOM) await load(b + "react-dom.production.min.js");
          if (!window.ReactFlow) await load(b + "reactflow.umd.js");
          if (window.ReactFlow) return;
        } catch (e) { /* next */ }
      }
      throw new Error("React Flow 本地 vendor 加载失败（/console/vendor/reactflow.umd.js）");
    })();
    return loading;
  }
  const css = (n, fb) => (getComputedStyle(document.documentElement).getPropertyValue(n).trim() || fb);
  const dark = () => { const t = document.documentElement.dataset.theme; return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches; };
  const PAL = () => dark() ? ["#a1caff", "#ffaea7", "#90ca90", "#eec448", "#c4b5fd", "#f9a8d4", "#7dd3fc", "#fdba74"]
    : ["#0067ce", "#c0262c", "#2f7d33", "#836700", "#6d28d9", "#be185d", "#0369a1", "#c2410c"];
  const TH = () => ({ text: css("--ax-text", dark() ? "#f1f1f1" : "#111"), sub: css("--ax-text-secondary", dark() ? "#c6c6c6" : "#555"),
    surf: css("--ax-surface", dark() ? "#222223" : "#fff"), raised: css("--ax-surface-raised", dark() ? "#2b2b2d" : "#f7f7f8"),
    border: css("--ax-border", dark() ? "#3f3f46" : "#e4e4e7"), accent: css("--ax-accent", dark() ? "#a1caff" : "#0067ce"),
    ok: dark() ? "#90ca90" : "#2f7d33", warn: dark() ? "#eec448" : "#b45309", bad: dark() ? "#ffaea7" : "#c0262c" });
  const esc = s => String(s ?? "");
  function mount(el, node) {
    let entry = roots.get(el);
    if (entry && entry.host.parentNode !== el) {
      try { entry.root.unmount(); } catch (_) { /* detached root: discard safely */ }
      roots.delete(el); entry = null;
    }
    if (!entry) {
      const host = document.createElement("div");
      host.setAttribute("data-react-mount", "flow-designer");
      el.replaceChildren(host);
      entry = { host, root: ReactDOM.createRoot(host) };
      roots.set(el, entry);
    }
    entry.root.render(node);
  }
  function unmount(el) {
    const entry = roots.get(el); if (!entry) return;
    try { entry.root.unmount(); } catch (_) { /* host may already be detached */ }
    if (entry.host.parentNode === el) entry.host.remove();
    roots.delete(el);
  }

  /* ---------------- 简易分层布局（无 dagre 依赖）：按关系做拓扑分层 */
  function layered(ids, edges, { dx = 300, dy = 40, heights = {} } = {}) {
    const indeg = new Map(ids.map(i => [i, 0])), out = new Map(ids.map(i => [i, []]));
    edges.forEach(e => { if (e.source !== e.target && out.has(e.source) && indeg.has(e.target)) { out.get(e.source).push(e.target); indeg.set(e.target, indeg.get(e.target) + 1); } });
    const layer = new Map(); let q = ids.filter(i => indeg.get(i) === 0); if (!q.length) q = [ids[0]];
    q.forEach(i => layer.set(i, 0));
    const seen = new Set(q);
    while (q.length) { const n = q.shift(); for (const m of out.get(n) || []) { layer.set(m, Math.max(layer.get(m) || 0, (layer.get(n) || 0) + 1)); if (!seen.has(m)) { seen.add(m); q.push(m); } } }
    ids.forEach(i => { if (!layer.has(i)) layer.set(i, 0); });
    const cols = {}; ids.forEach(i => (cols[layer.get(i)] = cols[layer.get(i)] || []).push(i));
    const pos = {};
    Object.entries(cols).forEach(([L, arr]) => { let y = 0; arr.forEach(i => { pos[i] = { x: +L * dx, y }; y += (heights[i] || 80) + dy; }); });
    return pos;
  }

  /* ================================================================ 1. 流水线 DAG */
  function PipelineNode({ data }) {
    const h = React.createElement, T = TH(), RF = ReactFlow;
    const col = { done: T.ok, warn: T.warn, error: T.bad, todo: T.border }[data.state] || T.border;
    const icon = { done: "✓", warn: "!", error: "✕", todo: "○" }[data.state] || "○";
    return h("div", { style: { padding: "10px 12px", minWidth: 150, borderRadius: 10, background: T.surf, color: T.text,
      border: `1.5px solid ${col}`, boxShadow: "0 1px 3px rgba(0,0,0,.08)", cursor: data.tab ? "pointer" : "default" } },
      h(RF.Handle, { type: "target", position: RF.Position.Left, style: { background: col } }),
      h("div", { style: { display: "flex", alignItems: "center", gap: 6, fontWeight: 600, fontSize: 13 } },
        h("span", { style: { width: 18, height: 18, borderRadius: 9, background: col, color: "#fff", display: "inline-flex", alignItems: "center", justifyContent: "center", fontSize: 11 } }, icon), data.label),
      h("div", { style: { fontSize: 11, color: T.sub, marginTop: 4, maxWidth: 190 } }, data.detail),
      data.ms != null ? h("div", { style: { fontSize: 10, color: T.sub, marginTop: 2 } }, data.ms + " ms") : null,
      h(RF.Handle, { type: "source", position: RF.Position.Right, style: { background: col } }));
  }
  function PipelineView({ data, onOpen }) {
    const h = React.createElement, RF = ReactFlow, T = TH();
    const nodeTypes = React.useMemo(() => ({ stage: PipelineNode }), []);
    const nodes = data.nodes.map((n, i) => ({ id: n.id, type: "stage", data: n, position: { x: (i % 5) * 230, y: Math.floor(i / 5) * 140 } }));
    const edges = data.edges.map((e, i) => {
      const s = data.nodes.find(n => n.id === e.target);
      return { id: "p" + i, source: e.source, target: e.target, animated: s && s.state !== "done" && s.state !== "todo",
        type: "smoothstep", markerEnd: { type: RF.MarkerType.ArrowClosed, color: T.sub }, style: { stroke: s && s.state === "done" ? T.ok : T.border, strokeWidth: 1.6 } };
    });
    return h(RF.default, { nodes, edges, nodeTypes, fitView: true, nodesDraggable: true, nodesConnectable: false, proOptions: { hideAttribution: true },
      onNodeClick: (_, n) => n.data.tab && onOpen && onOpen(n.data.tab) },
      h(RF.Background, { color: T.border, gap: 16 }), h(RF.Controls, { showInteractive: false }));
  }

  /* ================================================================ 2. 本体设计图 */
  function ClassNode({ data, selected }) {
    const h = React.createElement, RF = ReactFlow, T = TH();
    const hit = data.hit;
    return h("div", { style: { minWidth: 210, maxWidth: 260, borderRadius: 10, overflow: "hidden", background: T.surf, color: T.text,
      border: `2px solid ${selected ? T.accent : hit ? T.warn : data.color}`, boxShadow: selected ? `0 0 0 3px ${T.accent}33` : "0 1px 4px rgba(0,0,0,.1)" } },
      h(RF.Handle, { type: "target", position: RF.Position.Left, style: { width: 10, height: 10, background: data.color } }),
      h("div", { style: { background: data.color, color: "#fff", padding: "6px 10px", fontWeight: 600, fontSize: 13 } },
        data.label, h("span", { style: { opacity: .8, fontWeight: 400, fontSize: 11, marginLeft: 6 } }, data.name),
        h("div", { style: { fontSize: 10, opacity: .85, fontWeight: 400 } }, `${data.count ?? "—"} 实例 · 主键 ${data.key || "—"}${data.designed ? " · 设计器新建" : ""}${data.parent ? " · 继承 " + data.parent : ""}`)),
      h("div", { style: { maxHeight: 220, overflowY: "auto", fontSize: 11 } },
        data.attrs.slice(0, data.expanded ? 999 : 8).map(a => h("div", { key: a.key, "data-attr": a.key, onClick: (e) => { e.stopPropagation(); data.onAttr(a.key); },
          className: "nodrag", style: { display: "flex", justifyContent: "space-between", gap: 8, padding: "3px 10px", cursor: "pointer",
            borderTop: `1px solid ${T.border}`, background: a.hit ? (dark() ? "#4a3b00" : "#fff7d6") : "transparent" } },
          h("span", null, (a.pk ? "🔑 " : "") + (a.label || a.key), a.virtual ? h("span", { style: { color: T.warn, marginLeft: 4 } }, "◇") : null),
          h("span", { style: { color: T.sub } }, a.range))),
        data.attrs.length > 8 ? h("div", { className: "nodrag", onClick: (e) => { e.stopPropagation(); data.onToggle(); },
          style: { padding: "3px 10px", color: T.accent, cursor: "pointer", borderTop: `1px solid ${T.border}` } },
          data.expanded ? "收起" : `展开全部 ${data.attrs.length} 个属性`) : null,
        data.readOnly ? null : h("div", { className: "nodrag", onClick: (e) => { e.stopPropagation(); data.onAddAttr(); },
          style: { padding: "4px 10px", color: T.accent, cursor: "pointer", borderTop: `1px dashed ${T.border}`, fontSize: 11 } }, "＋ 新建属性")),
      h(RF.Handle, { type: "source", position: RF.Position.Right, style: { width: 10, height: 10, background: data.color } }));
  }

  function Designer({ job, api, onChanged, readOnly }) {
    const h = React.createElement, RF = ReactFlow, T = TH(), C = PAL();
    const sc = job.draft.schema, cmap = job.draft.class_map || {};
    const classes = Object.keys(sc.entity_types);
    const colorOf = c => C[classes.indexOf(c) % C.length];
    const counts = job.counts || {};
    const [q, setQ] = React.useState("");
    const [exp, setExp] = React.useState({});
    const [sel, setSel] = React.useState(null);               // {kind, ref}
    const [pending, setPending] = React.useState(null);       // 新关系 {source, target}
    const [creating, setCreating] = React.useState(null);     // {kind:'class'} | {kind:'attribute', owner}
    const [busy, setBusy] = React.useState(false);
    const [msg, setMsg] = React.useState("");
    const ql = q.trim().toLowerCase();
    const attrsOf = c => Object.entries(sc.attributes).filter(([, a]) => a.owner === c).map(([k, a]) => ({ key: k, ...a,
      pk: (sc.entity_types[c].key || []).includes(a.column), hit: ql && (k + " " + (a.label || "")).toLowerCase().includes(ql) }));
    const heights = {}; classes.forEach(c => heights[c] = 60 + Math.min(attrsOf(c).length, 8) * 21 + 24);
    const relEdges = Object.entries(sc.relation_types).map(([r, v]) => ({ id: r, source: v.source, target: v.target }));
    const auto = React.useMemo(() => layered(classes, relEdges, { heights }), [classes.join(","), relEdges.length]);
    const saved = job.design_layout || {};
    const mkNodes = () => classes.map(c => ({ id: c, type: "cls", position: saved[c] || auto[c] || { x: 0, y: 0 },
      data: { name: c, label: sc.entity_types[c].label || c, color: colorOf(c), key: (sc.entity_types[c].key || []).join(","), count: counts[c],
        attrs: attrsOf(c), expanded: !!exp[c], hit: ql && (c + " " + (sc.entity_types[c].label || "")).toLowerCase().includes(ql),
        designed: sc.entity_types[c].origin === "designer", parent: sc.entity_types[c].is_a, readOnly,
        onAddAttr: () => { setPending(null); setCreating({ kind: "attribute", owner: c }); },
        onAttr: k => setSel({ kind: "attribute", ref: k }), onToggle: () => setExp(x => ({ ...x, [c]: !x[c] })) } }));
    const isaEdges = () => classes.filter(c => sc.entity_types[c].is_a && sc.entity_types[sc.entity_types[c].is_a]).map(c => ({
      id: "isa:" + c, source: c, target: sc.entity_types[c].is_a, label: "is_a", type: "straight", selectable: false, deletable: false,
      markerEnd: { type: RF.MarkerType.ArrowClosed, color: T.sub }, style: { stroke: T.sub, strokeDasharray: "2 4" }, labelStyle: { fill: T.sub, fontSize: 10 } }));
    const mkEdges = () => isaEdges().concat(Object.entries(sc.relation_types).map(([r, v]) => ({ id: r, source: v.source, target: v.target,
      label: (v.label || r) + ({ many_to_one: " N:1", many_to_many: " N:N", one_to_one: " 1:1" }[v.cardinality] || ""),
      type: v.source === v.target ? "default" : "smoothstep", animated: sel && sel.kind === "relation" && sel.ref === r,
      markerEnd: { type: RF.MarkerType.ArrowClosed, color: colorOf(v.source) },
      style: { stroke: colorOf(v.source), strokeWidth: sel && sel.ref === r ? 3 : 1.8, strokeDasharray: v.cardinality === "many_to_many" ? "6 4" : undefined },
      labelStyle: { fill: T.text, fontSize: 11 }, labelBgStyle: { fill: T.surf, fillOpacity: .9 }, labelBgPadding: [4, 2], labelBgBorderRadius: 4 })));
    const [nodes, setNodes, onNodesChange] = RF.useNodesState(mkNodes());
    const [edges, setEdges, onEdgesChange] = RF.useEdgesState(mkEdges());
    React.useEffect(() => { setNodes(ns => { const p = Object.fromEntries(ns.map(n => [n.id, n.position])); return mkNodes().map(n => ({ ...n, position: p[n.id] || n.position, selected: sel && sel.kind === "class" && sel.ref === n.id })); }); }, [q, exp, job, sel]);
    React.useEffect(() => { setEdges(mkEdges()); }, [job, sel]);
    const inst = React.useRef(null);

    async function run(ops, okMsg) {
      if (readOnly) { setMsg("已发布的本体只读"); return; }
      setBusy(true); setMsg("");
      try { const r = await api(ops); setMsg(okMsg || ("已完成：" + (r.done || []).join("；"))); onChanged && await onChanged(r); }
      catch (e) { setMsg("失败：" + e.message); } finally { setBusy(false); }
    }
    const onConnect = (c) => { if (readOnly) return;
      if (c.source && c.target) {
        if (!Object.values(cmap).includes(c.source)) { setMsg(`失败：${c.source} 没有源表，无法按外键建立关系（可从有源表的类连向它）`); return; }
        setCreating(null); setPending({ source: c.source, target: c.target }); } };
    const onDel = (items) => {
      if (readOnly || !items.length) return;
      if (!confirm(`删除 ${items.map(i => i.ref).join("、")}？（删除类会同时删除它的属性与关系）`)) return;
      run(items.map(i => ({ op: "delete", kind: i.kind, ref: i.ref })));
    };
    const saveLayout = async () => {
      const pos = Object.fromEntries((inst.current ? inst.current.getNodes() : nodes).map(n => [n.id, n.position]));
      try { await fetch(job._base + `/automodel/jobs/${job.id}/design/layout`, { method: "POST", headers: job._hdr, body: JSON.stringify({ positions: pos }) }); setMsg("布局已保存"); }
      catch (e) { setMsg("保存失败：" + e.message); }
    };
    const relayout = () => { setNodes(ns => ns.map(n => ({ ...n, position: auto[n.id] || n.position }))); setTimeout(() => inst.current && inst.current.fitView({ padding: .15 }), 50); };
    const exportSvg = () => {
      const svg = buildSvg(sc, (inst.current ? inst.current.getNodes() : nodes), colorOf, T);
      dl(new Blob([svg], { type: "image/svg+xml" }), `ontology_${job.source}.svg`);
    };
    const exportPng = () => {
      const svg = buildSvg(sc, (inst.current ? inst.current.getNodes() : nodes), colorOf, T);
      const img = new Image(); const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
      img.onload = () => { const c = document.createElement("canvas"); c.width = img.width * 2; c.height = img.height * 2; const g = c.getContext("2d");
        g.fillStyle = T.surf; g.fillRect(0, 0, c.width, c.height); g.scale(2, 2); g.drawImage(img, 0, 0); c.toBlob(b => dl(b, `ontology_${job.source}.png`)); URL.revokeObjectURL(url); };
      img.src = url;
    };
    const exportJson = () => dl(new Blob([JSON.stringify({ schema: sc, layout: Object.fromEntries(nodes.map(n => [n.id, n.position])) }, null, 2)], { type: "application/json" }), `ontology_design_${job.source}.json`);

    /* -------- 侧栏 */
    const side = (() => {
      if (creating) {
        const tab = creating.owner ? Object.keys(cmap).find(t => cmap[t] === creating.owner) : null;
        const usedCols = new Set(Object.values(sc.attributes).filter(a => a.owner === creating.owner).map(a => a.column));
        const free = tab ? Object.keys((job.profiles[tab] || {}).columns || {}).filter(c => !usedCols.has(c)) : [];
        return h(Creator, { creating, classes, sc, free, busy, onCancel: () => setCreating(null),
          onOk: (op, okMsg) => run([op], okMsg).then(() => { setCreating(null);
            if (op.op === "add_class") setSel({ kind: "class", ref: op.new_name }); else setSel({ kind: "attribute", ref: op.new_name }); }) });
      }
      if (pending) {
        const tab = Object.keys(cmap).find(t => cmap[t] === pending.source);
        const cols = Object.keys((job.profiles[tab] || {}).columns || {});
        return h(NewRelation, { pending, cols, tab, sc, busy, onCancel: () => setPending(null),
          onOk: (col, name, label) => run([{ op: "add_relation", source_table: tab, column: col, target_class: pending.target, new_name: name || null, label }], "关系已新增，契约与物化已重建").then(() => setPending(null)) });
      }
      if (!sel) return h("div", { style: { color: T.sub, fontSize: 12, lineHeight: 1.8 } },
        h("b", { style: { color: T.text } }, "操作说明"), h("br"),
        "· 工具栏「＋ 新建类」；类卡片底部「＋ 新建属性」", h("br"),
        "· 拖动类节点右侧圆点到另一个类 → 新增关系", h("br"), "· 点击类 / 属性 / 关系 → 在此编辑", h("br"),
        "· 选中后按 Delete → 删除", h("br"), "· 搜索框高亮类与属性；拖动节点后「保存布局」", h("br"),
        h("b", { style: { color: T.text } }, `共 ${classes.length} 个类 · ${Object.keys(sc.relation_types).length} 种关系 · ${Object.keys(sc.attributes).length} 个属性`));
      const sec = { class: "entity_types", relation: "relation_types", attribute: "attributes" }[sel.kind];
      const obj = sc[sec][sel.ref];
      if (!obj) return h("div", null, "已删除");
      let colOpts = null;
      if (sel.kind === "attribute") {
        const tab = Object.keys(cmap).find(t => cmap[t] === obj.owner);
        const P = (job.profiles[tab] || {}).columns || {};
        const taken = new Set(Object.entries(sc.attributes).filter(([k, a]) => k !== sel.ref && a.owner === obj.owner && a.column).map(([, a]) => a.column));
        Object.values(sc.relation_types).forEach(r => { if (r.table === tab && r.column) taken.add(r.column); });
        colOpts = tab ? Object.keys(P).map(c => ({ c, taken: taken.has(c), p: P[c] })) : [];
      }
      return h(Editor, { key: sel.kind + sel.ref, sel, obj, sc, busy, readOnly, colOpts,
        onRebind: (col, keepType) => run([{ op: "rebind", kind: "attribute", ref: sel.ref, column: col, ...(keepType ? { datatype: obj.range } : {}) }],
          col ? `已改绑到列 ${col}，映射与物化已重建` : "已改为虚拟属性"),
        onSave: (patch) => run([{ op: "update", kind: sel.kind, ref: sel.ref, ...patch }], "已保存"),
        onRename: (nn) => run([{ op: "rename", kind: sel.kind, ref: sel.ref, new_name: nn }], "已改名").then(() => setSel({ ...sel, ref: nn })),
        onDelete: () => { onDel([sel]); setSel(null); } });
    })();

    return h("div", { style: { display: "grid", gridTemplateColumns: "1fr 300px", gap: 12, height: "100%" } },
      h("div", { style: { position: "relative", border: `1px solid ${T.border}`, borderRadius: 10, overflow: "hidden", background: T.raised } },
        h(RF.default, { nodes, edges, nodeTypes: NT, onNodesChange, onEdgesChange, onConnect, fitView: true, fitViewOptions: { padding: .15 },
          minZoom: .1, maxZoom: 2, deleteKeyCode: readOnly ? null : ["Delete", "Backspace"], proOptions: { hideAttribution: true },
          onInit: i => inst.current = i,
          onNodeClick: (ev, n) => { if (ev.target.closest && ev.target.closest("[data-attr], .nodrag")) return; setSel({ kind: "class", ref: n.id }); }, onEdgeClick: (_, e) => setSel({ kind: "relation", ref: e.id }),
          onPaneClick: () => setSel(null),
          onNodesDelete: ns => onDel(ns.map(n => ({ kind: "class", ref: n.id }))),
          onEdgesDelete: es => onDel(es.map(e => ({ kind: "relation", ref: e.id }))) },
          h(RF.Background, { color: T.border, gap: 18 }), h(RF.Controls, null),
          h(RF.MiniMap, { pannable: true, zoomable: true, nodeColor: n => n.data && n.data.color, maskColor: dark() ? "rgba(0,0,0,.5)" : "rgba(240,240,240,.6)", style: { background: T.surf } }),
          h(RF.Panel, { position: "top-left" },
            h("div", { style: { display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", maxWidth: "calc(100% - 20px)", background: T.surf, padding: 6, borderRadius: 8, border: `1px solid ${T.border}` } },
              readOnly ? null : h("button", { className: "primary", onClick: () => { setPending(null); setCreating({ kind: "class" }); } }, "＋ 新建类"),
              h("input", { placeholder: "查找类 / 属性", value: q, onChange: e => setQ(e.target.value), style: { flex: "0 0 150px", width: 150, minWidth: 150 } }),
              h("button", { className: "ghost", onClick: relayout }, "自动布局"),
              h("button", { className: "ghost", onClick: saveLayout }, "保存布局"),
              h("button", { className: "ghost", onClick: exportPng }, "PNG"),
              h("button", { className: "ghost", onClick: exportSvg }, "SVG"),
              h("button", { className: "ghost", onClick: exportJson }, "JSON"))),
          busy ? h(RF.Panel, { position: "top-right" }, h("span", { className: "tag" }, "处理中…")) : null)),
      h("div", { style: { border: `1px solid ${T.border}`, borderRadius: 10, padding: 12, overflowY: "auto", background: T.surf } },
        readOnly ? h("div", { className: "tag warn", style: { marginBottom: 8 } }, "已发布：只读") : null,
        msg ? h("div", { style: { fontSize: 12, marginBottom: 8, color: msg.startsWith("失败") ? T.bad : T.ok } }, msg) : null, side));
  }
  const NT = { cls: ClassNode };

  function Editor({ sel, obj, sc, onSave, onRename, onDelete, onRebind, colOpts, busy, readOnly }) {
    const h = React.createElement, T = TH();
    const [label, setLabel] = React.useState(obj.label || "");
    const [desc, setDesc] = React.useState(obj.description || "");
    const [dt, setDt] = React.useState(obj.range || "string");
    const [nn, setNn] = React.useState(sel.ref);
    const [col, setCol] = React.useState(obj.column || "");
    const [keepType, setKeepType] = React.useState(false);
    const isPk = sel.kind === "attribute" && (obj.identifier || ((sc.entity_types[obj.owner] || {}).key || []).includes(obj.column));
    const cp = colOpts && col ? (colOpts.find(x => x.c === col) || {}).p : null;
    const K = { class: "类", attribute: "属性", relation: "关系" }[sel.kind];
    const row = (l, el) => h("div", { style: { marginBottom: 8 } }, h("label", { className: "f" }, l), el);
    const info = sel.kind === "relation" ? `${obj.source} → ${obj.target} · ${obj.cardinality}${obj.column ? " · 外键 " + obj.table + "." + obj.column : ""}${obj.via_table ? " · 经 " + obj.via_table : ""}`
      : sel.kind === "attribute" ? `所属 ${obj.owner} · 列 ${obj.column} · 角色 ${obj.role}` : `主键 ${(obj.key || []).join(",") || "—"}`;
    return h("div", null,
      h("div", { style: { fontWeight: 600, marginBottom: 4 } }, `${K}：${sel.ref}`),
      h("div", { style: { fontSize: 11, color: T.sub, marginBottom: 10 } }, info),
      row("中文名", h("input", { value: label, disabled: readOnly, onChange: e => setLabel(e.target.value) })),
      row("描述", h("textarea", { value: desc, rows: 3, disabled: readOnly, onChange: e => setDesc(e.target.value), style: { width: "100%" } })),
      sel.kind === "attribute" ? row("数据类型", h("select", { value: dt, disabled: readOnly, onChange: e => setDt(e.target.value) },
        ["string", "integer", "float", "boolean", "date", "datetime"].map(t => h("option", { key: t }, t)))) : null,
      h("button", { className: "primary", disabled: busy || readOnly, onClick: () => onSave({ label, description: desc, ...(sel.kind === "attribute" ? { datatype: dt } : {}) }) }, "保存"),
      sel.kind === "attribute" ? h("div", null,
        h("hr", { style: { margin: "14px 0", border: 0, borderTop: `1px solid ${T.border}` } }),
        row(`绑定源列（当前：${obj.column || "虚拟 ◇"}）`, isPk ? h("div", { style: { fontSize: 11, color: T.sub } }, "主键属性不能改绑")
          : !colOpts || !colOpts.length ? h("div", { style: { fontSize: 11, color: T.sub } }, "所属类没有源表，只能是虚拟属性")
          : h("select", { value: col, disabled: readOnly, onChange: e => setCol(e.target.value) },
            h("option", { value: "" }, "— 不绑定（虚拟属性）—"),
            colOpts.map(o => h("option", { key: o.c, value: o.c, disabled: o.taken }, o.c + (o.taken ? "（已占用）" : "") + (o.p ? ` · ${o.p.dtype}/${o.p.role}` : ""))))),
        cp ? h("div", { style: { fontSize: 11, color: T.sub, marginTop: -4, marginBottom: 6 } },
          `空值率 ${Math.round((cp.null_ratio || 0) * 100)}% · 去重 ${cp.distinct} · 样例 ${(cp.samples || []).slice(0, 3).join(" / ")}`) : null,
        !isPk && colOpts && colOpts.length && col ? h("label", { className: "hint", style: { display: "block", margin: "0 0 6px" } },
          h("input", { type: "checkbox", checked: keepType, onChange: e => setKeepType(e.target.checked), style: { width: "auto" } }), " 保留当前数据类型（否则按新列推断）") : null,
        !isPk && colOpts && colOpts.length ? h("button", { className: "ghost", disabled: busy || readOnly || col === (obj.column || ""),
          onClick: () => { if (confirm(col ? `把 ${sel.ref} 改绑到列 ${col}？将重建映射与物化` : `把 ${sel.ref} 改为虚拟属性？源数据值将不再映射`)) onRebind(col, keepType); } }, "应用改绑") : null) : null,
      h("hr", { style: { margin: "14px 0", border: 0, borderTop: `1px solid ${T.border}` } }),
      row("改名（英文标识符，会重建映射）", h("div", { style: { display: "flex", gap: 6 } },
        h("input", { value: nn, disabled: readOnly, onChange: e => setNn(e.target.value) }),
        h("button", { className: "ghost", disabled: busy || readOnly || nn === sel.ref || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(nn), onClick: () => onRename(nn) }, "改名"))),
      h("button", { className: "ghost", disabled: busy || readOnly, style: { color: T.bad, marginTop: 6 }, onClick: onDelete }, `删除${K}`));
  }

  function Creator({ creating, classes, sc, free, busy, onOk, onCancel }) {
    const h = React.createElement, T = TH();
    const isCls = creating.kind === "class";
    const [name, setName] = React.useState("");
    const [label, setLabel] = React.useState("");
    const [desc, setDesc] = React.useState("");
    const [parent, setParent] = React.useState("");
    const [dt, setDt] = React.useState("string");
    const [role, setRole] = React.useState("");
    const [col, setCol] = React.useState("");
    const ROLES = [["", "按类型自动"], ["text", "文本"], ["name", "名称"], ["enum", "枚举（分组维度）"], ["measure", "度量（可聚合）"], ["temporal", "时间"], ["identifier", "标识"], ["boolean", "布尔"]];
    const okName = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(name) && !(isCls ? sc.entity_types[name] : sc.attributes[name]);
    const row = (l, el) => h("div", { style: { marginBottom: 8 } }, h("label", { className: "f" }, l), el);
    const suggest = s => s.replace(/[^A-Za-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
    return h("div", null,
      h("div", { style: { fontWeight: 600, marginBottom: 4 } }, isCls ? "新建类" : `新建属性 · ${creating.owner}`),
      h("div", { style: { fontSize: 11, color: T.sub, marginBottom: 10 } }, isCls
        ? "新类没有源表：不参与自动物化，可在「4 实例与关系」用 Action 新建实例；也可作为其他类的父类。"
        : free.length ? "可绑定源表中尚未映射的列（重建后自动有值），或留空建为虚拟属性（◇，由 Action 填写）。" : "该类没有可绑定的空闲列：将建为虚拟属性（◇，由 Action 填写）。"),
      !isCls && free.length ? row("绑定源列（可选）", h("select", { value: col, onChange: e => { setCol(e.target.value); if (!name && e.target.value) setName(suggest(e.target.value)); } },
        h("option", { value: "" }, "— 不绑定（虚拟属性）—"), free.map(c => h("option", { key: c, value: c }, c)))) : null,
      row(isCls ? "类名（英文，PascalCase）" : "属性名（英文，snake_case）", h("input", { value: name, placeholder: isCls ? "如 Skill" : "如 skill_level", onChange: e => setName(e.target.value),
        style: { borderColor: name && !okName ? T.bad : undefined } })),
      name && !okName ? h("div", { style: { color: T.bad, fontSize: 11, marginTop: -4, marginBottom: 6 } }, "需字母/下划线开头的英文标识符，且不能重名") : null,
      row("中文名", h("input", { value: label, onChange: e => setLabel(e.target.value) })),
      row("描述", h("textarea", { value: desc, rows: 2, onChange: e => setDesc(e.target.value), style: { width: "100%" } })),
      isCls ? row("父类（可选，is_a）", h("select", { value: parent, onChange: e => setParent(e.target.value) },
        h("option", { value: "" }, "— 无 —"), classes.map(c => h("option", { key: c, value: c }, (sc.entity_types[c].label || c) + " · " + c)))) : null,
      !isCls ? row("数据类型", h("select", { value: dt, onChange: e => setDt(e.target.value) }, ["string", "integer", "float", "boolean", "date", "datetime"].map(t => h("option", { key: t }, t)))) : null,
      !isCls ? row("角色", h("select", { value: role, onChange: e => setRole(e.target.value) }, ROLES.map(([v, l]) => h("option", { key: v, value: v }, l)))) : null,
      h("div", { style: { display: "flex", gap: 6, marginTop: 6 } },
        h("button", { className: "primary", disabled: busy || !okName, onClick: () => onOk(isCls
          ? { op: "add_class", new_name: name, label: label || null, description: desc || null, parent: parent || null }
          : { op: "add_attribute", owner: creating.owner, new_name: name, label: label || null, description: desc || null, datatype: dt, role: role || null, column: col || null },
          isCls ? `已新建类 ${name}` : `已新建属性 ${name}`) }, "创建"),
        h("button", { className: "ghost", onClick: onCancel }, "取消")));
  }

  function NewRelation({ pending, cols, tab, sc, busy, onOk, onCancel }) {
    const h = React.createElement, T = TH();
    const used = new Set(Object.values(sc.relation_types).filter(r => r.table === tab).map(r => r.column));
    const guess = cols.find(c => !used.has(c) && c.toLowerCase().includes(pending.target.toLowerCase().slice(0, 4))) || cols.find(c => /_id$|_no$|code$/i.test(c) && !used.has(c)) || cols[0];
    const [col, setCol] = React.useState(guess);
    const [name, setName] = React.useState("");
    const [label, setLabel] = React.useState("");
    return h("div", null,
      h("div", { style: { fontWeight: 600, marginBottom: 6 } }, "新增关系"),
      h("div", { style: { fontSize: 12, color: T.sub, marginBottom: 10 } }, `${pending.source} → ${pending.target}（多对一，按外键列关联）`),
      h("label", { className: "f" }, `外键列（${tab} 表）`),
      h("select", { value: col, onChange: e => setCol(e.target.value) }, cols.map(c => h("option", { key: c, value: c }, c + (used.has(c) ? "（已用）" : "")))),
      h("label", { className: "f", style: { marginTop: 8 } }, "关系名（英文，可留空自动生成）"), h("input", { value: name, onChange: e => setName(e.target.value) }),
      h("label", { className: "f", style: { marginTop: 8 } }, "中文名"), h("input", { value: label, onChange: e => setLabel(e.target.value) }),
      h("div", { style: { marginTop: 10, display: "flex", gap: 6 } },
        h("button", { className: "primary", disabled: busy || !col, onClick: () => onOk(col, name, label) }, "创建"),
        h("button", { className: "ghost", onClick: onCancel }, "取消")));
  }

  /* ---------------- 本体设计图导出（独立 SVG，不依赖 DOM 截图） */
  function buildSvg(sc, nodes, colorOf, T) {
    const W = 230, pos = {}; let maxX = 0, maxY = 0, minX = 1e9, minY = 1e9;
    const attrs = c => Object.entries(sc.attributes).filter(([, a]) => a.owner === c);
    nodes.forEach(n => { const hh = 44 + Math.min(attrs(n.id).length, 12) * 18 + 8; pos[n.id] = { ...n.position, h: hh };
      minX = Math.min(minX, n.position.x); minY = Math.min(minY, n.position.y); maxX = Math.max(maxX, n.position.x + W); maxY = Math.max(maxY, n.position.y + hh); });
    const ox = 40 - minX, oy = 40 - minY, X = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    let out = `<svg xmlns="http://www.w3.org/2000/svg" width="${maxX - minX + 80}" height="${maxY - minY + 80}" font-family="Figtree,'Microsoft YaHei',sans-serif">`;
    out += `<defs><marker id="a" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0L10,5L0,10z" fill="${T.sub}"/></marker></defs>`;
    out += `<rect width="100%" height="100%" fill="${T.surf}"/>`;
    Object.entries(sc.relation_types).forEach(([r, v]) => { const a = pos[v.source], b = pos[v.target]; if (!a || !b) return;
      const x1 = a.x + ox + W, y1 = a.y + oy + 22, x2 = b.x + ox, y2 = b.y + oy + 22, mx = (x1 + x2) / 2;
      out += `<path d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}" fill="none" stroke="${colorOf(v.source)}" stroke-width="1.8" marker-end="url(#a)"${v.cardinality === "many_to_many" ? ' stroke-dasharray="6 4"' : ""}/>`;
      out += `<text x="${mx}" y="${(y1 + y2) / 2 - 4}" font-size="11" text-anchor="middle" fill="${T.text}">${X(v.label || r)}</text>`; });
    nodes.forEach(n => { const p = pos[n.id], c = colorOf(n.id), et = sc.entity_types[n.id] || {}, x = p.x + ox, y = p.y + oy;
      out += `<rect x="${x}" y="${y}" width="${W}" height="${p.h}" rx="8" fill="${T.surf}" stroke="${c}" stroke-width="2"/>`;
      out += `<path d="M${x},${y + 8} a8,8 0 0 1 8,-8 h${W - 16} a8,8 0 0 1 8,8 v30 h-${W} z" fill="${c}"/>`;
      out += `<text x="${x + 10}" y="${y + 18}" font-size="13" font-weight="600" fill="#fff">${X(et.label || n.id)}</text>`;
      out += `<text x="${x + 10}" y="${y + 32}" font-size="10" fill="#fff" opacity=".85">${X(n.id)} · 主键 ${X((et.key || []).join(",") || "—")}</text>`;
      attrs(n.id).slice(0, 12).forEach(([k, a], i) => { out += `<text x="${x + 10}" y="${y + 56 + i * 18}" font-size="11" fill="${T.text}">${X(a.label || k)}</text><text x="${x + W - 10}" y="${y + 56 + i * 18}" font-size="10" text-anchor="end" fill="${T.sub}">${X(a.range || "")}</text>`; });
      const more = attrs(n.id).length - 12; if (more > 0) out += `<text x="${x + 10}" y="${y + p.h - 6}" font-size="10" fill="${T.sub}">… 另有 ${more} 个属性</text>`; });
    return out + "</svg>";
  }
  function dl(blob, name) { const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); }

  async function pipeline(el, data, opts = {}) { await ready(); mount(el, React.createElement(RFWrap, null, React.createElement(PipelineView, { data, ...opts }))); }
  async function designer(el, job, opts = {}) { await ready(); mount(el, React.createElement(RFWrap, null, React.createElement(Designer, { job, ...opts }))); }
  function RFWrap({ children }) { return React.createElement(ReactFlow.ReactFlowProvider, null, children); }
  window.RF = { ready, pipeline, designer, unmount, buildSvg };
})();
