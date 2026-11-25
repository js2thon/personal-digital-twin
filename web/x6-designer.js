/* x6-designer.js — 对象关系图编辑器（AntV X6 图形引擎 + dagre 自动布局 + React 视图层）
 *
 * window.X6D = {
 *   designer(el, graph, {onEdit})   对象关系图：类节点（属性列表）+ 关系边
 *      · dagre 分层自动布局；拖动节点；Ctrl+滚轮缩放；框选
 *      · 点击类/边 → 右侧 React 属性表单编辑（名称/起点/终点/属性）
 *      · Delete 删除；编辑提交后 onEdit(ops) 自动保存重建
 * }
 * 依赖：/console/vendor/x6.js（window.X6）、/console/vendor/dagre.min.js（window.dagre）、
 *       React（右侧属性面板由 React 渲染挂载）
 */
(function () {
  const BASES = ["/console/vendor/", "./vendor/"];
  const load = (src) => new Promise((ok, bad) => { const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = () => bad(new Error("加载失败：" + src)); document.head.appendChild(s); });
  let loading = null;
  async function ready() {
    if (window.X6 && window.dagre && window.React && window.ReactDOM) return;
    if (loading) return loading;
    loading = (async () => {
      for (const b of BASES) {
        try {
          if (!window.React) await load(b + "react.production.min.js");
          if (!window.ReactDOM) await load(b + "react-dom.production.min.js");
          if (!window.dagre) await load(b + "dagre.min.js");
          if (!window.X6) await load(b + "x6.js");
          if (window.X6 && window.dagre) return;
        } catch (e) { /* next */ }
      }
      throw new Error("X6 / dagre 本地 vendor 加载失败");
    })();
    return loading;
  }
  const css = (n, fb) => (getComputedStyle(document.documentElement).getPropertyValue(n).trim() || fb);
  const dark = () => { const t = document.documentElement.dataset.theme; return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches; };
  const TH = () => ({ text: css("--ax-text", dark() ? "#f1f1f1" : "#111"), sub: css("--ax-text-secondary", dark() ? "#c6c6c6" : "#555"),
    surf: css("--ax-surface", dark() ? "#222223" : "#fff"), raised: css("--ax-surface-raised", dark() ? "#2b2b2d" : "#f7f7f8"),
    border: css("--ax-border", dark() ? "#3f3f46" : "#e4e4e7"), accent: css("--ax-info", dark() ? "#a1caff" : "#0067ce"),
    ok: dark() ? "#90ca90" : "#2f7d33", bad: dark() ? "#ffaea7" : "#c0262c" });
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  /* ---------------- dagre 分层自动布局 */
  function dagreLayout(nodes, edges, opts) {
    const o = opts || {};
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: "LR", ranksep: o.ranksep || 60, nodesep: o.nodesep || 40, marginx: 20, marginy: 20 });
    g.setDefaultEdgeLabel(function () { return {}; });
    nodes.forEach(function (n) { g.setNode(n.id, { width: n.width || 180, height: n.height || 100 }); });
    edges.forEach(function (e) { if (e.source !== e.target) g.setEdge(e.source, e.target); });
    dagre.layout(g);
    const pos = {};
    nodes.forEach(function (n) { const p = g.node(n.id); if (p) pos[n.id] = { x: p.x - (n.width || 180) / 2, y: p.y - (n.height || 100) / 2 }; });
    return pos;
  }

  /* ---------------- 右侧属性表单（React 视图层） */
  function PropPanel(props) {
    var graph = props.graph, sel = props.sel, onEdit = props.onEdit, onDel = props.onDel;
    var h = React.createElement, T = TH();
    var busyState = React.useState(false), busy = busyState[0], setBusy = busyState[1];
    var msgState = React.useState(""), msg = msgState[0], setMsg = msgState[1];
    var isClass = !!(sel && sel.kind === "class");
    var edge = (!isClass && sel) ? graph.edges.find(function (e) { return e.id === sel.ref; }) : null;
    var node = (isClass && sel) ? graph.nodes.find(function (n) { return n.id === sel.ref; }) : null;
    var labelState = React.useState(isClass ? (node ? node.data.label : "") : ""), label = labelState[0], setLabel = labelState[1];
    var propNameState = React.useState(""), propName = propNameState[0], setPropName = propNameState[1];
    var sourceColState = React.useState(""), sourceCol = sourceColState[0], setSourceCol = sourceColState[1];
    var newRelNameState = React.useState(""), newRelName = newRelNameState[0], setNewRelName = newRelNameState[1];
    var newRelSrcState = React.useState(""), newRelSrc = newRelSrcState[0], setNewRelSrc = newRelSrcState[1];
    var newRelTgtState = React.useState(""), newRelTgt = newRelTgtState[0], setNewRelTgt = newRelTgtState[1];
    var newRelSrcColState = React.useState(""), newRelSrcCol = newRelSrcColState[0], setNewRelSrcCol = newRelSrcColState[1];
    var newRelTgtColState = React.useState(""), newRelTgtCol = newRelTgtColState[0], setNewRelTgtCol = newRelTgtColState[1];
    var descState = React.useState(isClass ? (node ? node.data.desc : "") : (edge ? (edge.data && edge.data.desc || "") : "")), desc = descState[0], setDesc = descState[1];
    var relNameState = React.useState((!isClass && sel) ? sel.ref : ""), relName = relNameState[0], setRelName = relNameState[1];
    var relSrcState = React.useState(!isClass && edge ? edge.source.replace(/^CLASS:/, "") : ""), relSrc = relSrcState[0], setRelSrc = relSrcState[1];
    var relTgtState = React.useState(!isClass && edge ? edge.target.replace(/^CLASS:/, "") : ""), relTgt = relTgtState[0], setRelTgt = relTgtState[1];
    var srcColState = React.useState(!isClass && edge ? (edge.data && edge.data.source_property || "") : ""), srcCol = srcColState[0], setSrcCol = srcColState[1];
    var tgtColState = React.useState(!isClass && edge ? (edge.data && edge.data.target_property || "") : ""), tgtCol = tgtColState[0], setTgtCol = tgtColState[1];
    React.useEffect(function () { setMsg(""); }, [sel && sel.ref]);
    function clsName(id) { return (id || "").replace(/^CLASS:/, ""); }
    function clsProps(id) { var n = graph.nodes.find(function (n) { return n.id === "CLASS:" + clsName(id); }); return n ? (n.data.props || []) : []; }
    var clsOptions = graph.nodes.map(function (n) { return clsName(n.id); });
    function run(ops, okMsg) {
      if (!onEdit) return Promise.resolve();
      setBusy(true); setMsg("");
      return onEdit(ops).then(function (r) { setMsg(okMsg || ("已保存：" + (r.done || []).join("；"))); })
        .catch(function (e) { setMsg("失败：" + e.message); })
        .finally(function () { setBusy(false); });
    }
    if (!sel) return h("div", { style: { color: T.sub, fontSize: 12, lineHeight: 1.7 } },
      h("b", { style: { color: T.text, display:"block", marginBottom:6 } }, "新增业务关系"),
      h("input", { value:newRelName, placeholder:"关系标识（如 belongsTo）", onChange:function(e){setNewRelName(e.target.value);}, style:{width:"100%",marginBottom:6} }),
      h("select", { value:newRelSrc, onChange:function(e){setNewRelSrc(e.target.value);setNewRelSrcCol("");}, style:{width:"100%",marginBottom:6} },
        [h("option",{key:"",value:""},"选择关系起点")].concat(clsOptions.map(function(c){return h("option",{key:c,value:c},c);}))),
      h("select", { value:newRelSrcCol, onChange:function(e){setNewRelSrcCol(e.target.value);}, style:{width:"100%",marginBottom:6} },
        [h("option",{key:"",value:""},"选择起点字段")].concat(clsProps(newRelSrc).map(function(p){return h("option",{key:p,value:p},p);}))),
      h("select", { value:newRelTgt, onChange:function(e){setNewRelTgt(e.target.value);setNewRelTgtCol("");}, style:{width:"100%",marginBottom:6} },
        [h("option",{key:"",value:""},"选择关系终点")].concat(clsOptions.map(function(c){return h("option",{key:c,value:c},c);}))),
      h("select", { value:newRelTgtCol, onChange:function(e){setNewRelTgtCol(e.target.value);}, style:{width:"100%",marginBottom:8} },
        [h("option",{key:"",value:""},"选择终点字段")].concat(clsProps(newRelTgt).map(function(p){return h("option",{key:p,value:p},p);}))),
      h("button", { className:"primary", disabled:busy||!newRelName.trim()||!newRelSrc||!newRelTgt, onClick:function(){
        run([{op:"create_relation",new_name:newRelName.trim(),source:newRelSrc,target:newRelTgt,source_property:newRelSrcCol,target_property:newRelTgtCol,description:"由本体工作台创建"}],"关系已创建并重建实例边");
        setNewRelName("");
      } }, "创建关系"),
      h("div", { style:{borderTop:"1px solid "+T.border,marginTop:12,paddingTop:10} },
        h("b", { style:{color:T.text} }, "画布操作说明"), h("br"),
        "· 点击对象或关系，在此编辑", h("br"),
        "· Delete 删除选中元素", h("br"),
        "· 拖动节点调整布局；Ctrl+滚轮缩放", h("br"),
        "· " + graph.nodes.length + " 个对象 · " + graph.edges.length + " 个关系"));
    return h("div", null,
      h("div", { style: { fontWeight: 700, marginBottom: 8, color: T.text } }, (isClass ? "类：" : "关系：") + sel.ref),
      isClass && node && node.data.sourceInfo ? h("div", { style: { padding: 8, marginBottom: 8, borderRadius: 6, background: T.raised, fontSize: 11, color: T.sub } },
        h("b", { style: { color: T.text } }, "上游数据源"),
        h("div", null, "数据表：" + (node.data.sourceInfo.source_table || "—")),
        h("div", null, "记录数：" + (node.data.sourceInfo.row_count ?? "—")),
        h("div", null, "字段：" + (node.data.sourceInfo.columns || []).length + " 个")) : null,
      isClass ? h("div", { style: { marginBottom: 8 } },
        h("label", { style: { display: "block", fontSize: 11, color: T.sub } }, "中文名"),
        h("input", { value: label, onChange: function (e) { setLabel(e.target.value); }, style: { width: "100%", marginBottom: 6 } })) : null,
      h("div", { style: { marginBottom: 8 } },
        h("label", { style: { display: "block", fontSize: 11, color: T.sub } }, "描述"),
        h("textarea", { value: desc, onChange: function (e) { setDesc(e.target.value); }, rows: 3, style: { width: "100%" } })),
      !isClass && edge && edge.data && edge.data.source_binding ? h("div", { style: { padding: 8, marginBottom: 8, borderRadius: 6, background: T.raised, fontSize: 11, color: T.sub } },
        h("b", { style: { color: T.text } }, "关系来源映射"),
        h("div", null, (edge.data.source_binding.source_table || "—") + "." + (edge.data.source_binding.source_column || "—") + " → " + (edge.data.source_binding.target_table || "—") + "." + (edge.data.source_binding.target_column || "—")),
        h("div", null, edge.data.source_binding.mapped ? "已配置字段映射" : "尚未配置完整字段映射")) : null,
      !isClass ? h("div", { style: { marginBottom: 8 } },
        h("label", { style: { display: "block", fontSize: 11, color: T.sub } }, "关系名称"),
        h("input", { value: relName, onChange: function (e) { setRelName(e.target.value); }, style: { width: "100%" } })) : null,
      !isClass ? h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginBottom: 8 } },
        h("div", null,
          h("label", { style: { display: "block", fontSize: 11, color: T.sub } }, "关系起点（业务对象）"),
          h("select", { value: relSrc, onChange: function (e) { setRelSrc(e.target.value); setSrcCol(""); }, style: { width: "100%" } },
            clsOptions.map(function (c) { return h("option", { key: c, value: c }, c); }))),
        h("div", null,
          h("label", { style: { display: "block", fontSize: 11, color: T.sub } }, "关系终点（业务对象）"),
          h("select", { value: relTgt, onChange: function (e) { setRelTgt(e.target.value); setTgtCol(""); }, style: { width: "100%" } },
            clsOptions.map(function (c) { return h("option", { key: c, value: c }, c); })))) : null,
      !isClass ? h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginBottom: 8 } },
        h("div", null,
          h("label", { style: { display: "block", fontSize: 11, color: T.sub } }, "起点属性"),
          h("select", { value: srcCol, onChange: function (e) { setSrcCol(e.target.value); }, style: { width: "100%" } },
            [h("option", { key: "", value: "" }, "—")].concat(clsProps(relSrc).map(function (p) { return h("option", { key: p, value: p }, p); })))),
        h("div", null,
          h("label", { style: { display: "block", fontSize: 11, color: T.sub } }, "终点属性"),
          h("select", { value: tgtCol, onChange: function (e) { setTgtCol(e.target.value); }, style: { width: "100%" } },
            [h("option", { key: "", value: "" }, "—")].concat(clsProps(relTgt).map(function (p) { return h("option", { key: p, value: p }, p); }))))) : null,
      isClass ? h("div", { style: { borderTop: "1px solid " + T.border, paddingTop: 9, marginTop: 8, marginBottom: 9 } },
        h("b", { style: { display: "block", color: T.text, fontSize: 12, marginBottom: 6 } }, "属性与上游字段绑定"),
        h("div", { style: { display: "grid", gap: 5, marginBottom: 7 } }, (node.data.props || []).map(function (p) {
          var m=(node.data.propertySources||{})[p]||{};
          return h("div", { key:p, style:{display:"grid",gridTemplateColumns:"minmax(70px,1fr) minmax(100px,1.4fr) auto",gap:5,alignItems:"center"} },
            h("span", { style:{fontSize:11,color:T.text,overflowWrap:"anywhere"} }, p),
            h("select", { defaultValue:m.mapped?(m.source_column||p):"", style:{width:"100%",fontSize:11}, onChange:function(e){run([{op:"update_property",type_name:clsName(sel.ref),property_name:p,source_column:e.target.value,clear_source_mapping:!e.target.value}],"字段映射已更新")} },
              [h("option",{key:"",value:""},"未绑定")].concat(((node.data.sourceInfo||{}).columns||[]).map(function(col){return h("option",{key:col,value:col},col)}))),
            h("button",{className:"ghost",title:"删除属性",onClick:function(){if(confirm("删除属性 "+p+"？"))run([{op:"delete_property",type_name:clsName(sel.ref),property_name:p}],"属性已删除")}},"×"));
        })),
        h("input", { value: propName, placeholder: "新增属性名称", onChange: function (e) { setPropName(e.target.value); }, style: { width: "100%", marginBottom: 6 } }),
        h("select", { value: sourceCol, onChange: function (e) { setSourceCol(e.target.value); }, style: { width: "100%", marginBottom: 6 } },
          [h("option", { key: "", value: "" }, "不绑定上游字段")].concat(((node.data.sourceInfo || {}).columns || []).map(function (p) { return h("option", { key: p, value: p }, p); }))),
        h("button", { className: "ghost", disabled: busy || !propName.trim(), onClick: function () {
          run([{ op: "add_property", type_name: clsName(sel.ref), property_name: propName.trim(), source_column: sourceCol }], "属性已新增并保存");
          setPropName(""); setSourceCol("");
        } }, "新增属性")) : null,
      h("div", { style: { display: "flex", gap: 6 } },
        h("button", { className: "primary", disabled: busy, onClick: function () {
          var ops = [{ op: isClass ? "update_class" : "update_relation", ref: sel.ref,
            description: desc }].map(function (o) {
            if (isClass) { o.label = label; } else { o.new_name = relName.trim(); o.source = relSrc; o.target = relTgt; o.source_property = srcCol; o.target_property = tgtCol; }
            return o; });
          run(ops, "已保存并重建");
        } }, "保存"),
        h("button", { className: "ghost", disabled: busy, onClick: function () { if (confirm("删除 " + sel.ref + "？")) { onDel([sel]); } } }, "删除")),
      msg ? h("div", { style: { fontSize: 12, marginTop: 8, color: msg.indexOf("失败") === 0 ? T.bad : T.ok } }, msg) : null);
  }

  /* ---------------- 主入口：X6 画布 + React 属性面板 */
  async function designer(el, graph, opts) {
    var o = opts || {};
    await ready();
    var T = TH();
    el.innerHTML = "";
    var wrap = document.createElement("div");
    wrap.className = "x6-designer-layout";
    wrap.style.cssText = "display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:12px;height:min(72vh,760px);min-height:620px";
    var canvasHost = document.createElement("div");
    canvasHost.style.cssText = "position:relative;border:1px solid " + T.border + ";border-radius:10px;overflow:hidden;background:" + T.raised;
    var panelHost = document.createElement("div");
    panelHost.style.cssText = "border:1px solid " + T.border + ";border-radius:10px;padding:12px;overflow-y:auto;background:" + T.surf;
    wrap.append(canvasHost, panelHost);
    el.appendChild(wrap);

    var nodeSize = {};
    graph.nodes.forEach(function (n) { nodeSize[n.id] = { width: 180, height: 64 + (n.data.props || []).length * 18 }; });

    var pos = dagreLayout(
      graph.nodes.map(function (n) { return { id: n.id, width: nodeSize[n.id].width, height: nodeSize[n.id].height }; }),
      graph.edges.map(function (e) { return { source: e.source, target: e.target }; }));

    var graph6 = new X6.Graph({
      container: canvasHost,
      background: { color: T.raised },
      grid: { size: 16, visible: true, type: "dot", args: { color: T.border, thickness: 1 } },
      interacting: { nodeMovable: true, edgeMovable: false, magnetConnectable: false },
      selecting: { enabled: true, rubberband: true, showNodeSelectedBox: true },
      keyboard: true,
      mousewheel: { enabled: true, modifiers: "ctrl", factor: 1.1, maxScale: 4, minScale: 0.2 },
    });

    graph.nodes.forEach(function (n) {
      var props = (n.data.props || []).slice(0, 8);
      var saved = (o.positions || {})[n.id] || {};
      var markup = [
        { tagName: "rect", selector: "body" },
        { tagName: "text", selector: "title" },
        { tagName: "text", selector: "pk" },
      ];
      // 属性行（每行一个 text）
      props.forEach(function (p, i) {
        markup.push({ tagName: "text", selector: "prop" + i });
      });
      var attrs = {
        body: { width: nodeSize[n.id].width, height: nodeSize[n.id].height, rx: 10, ry: 10,
          fill: T.surf, stroke: T.border, strokeWidth: 1.5, style: "box-shadow:0 1px 3px rgba(0,0,0,.08)" },
        title: { text: n.data.label, fill: T.text, fontSize: 13, fontWeight: "bold", refX: 12, refY: 20 },
        pk: { text: "🔑 " + (n.data.pk || "—"), fill: T.sub, fontSize: 10, refX: 12, refY: 36 },
      };
      props.forEach(function (p, i) {
        attrs["prop" + i] = { text: "· " + p, fill: T.sub, fontSize: 11, refX: 12, refY: 52 + i * 16 };
      });
      graph6.addNode({
        id: n.id, x: Number.isFinite(saved.x) ? saved.x : (pos[n.id] || { x: 0 }).x, y: Number.isFinite(saved.y) ? saved.y : (pos[n.id] || { y: 0 }).y,
        width: nodeSize[n.id].width, height: nodeSize[n.id].height,
        markup: markup, attrs: attrs,
      });
    });
    graph.edges.forEach(function (e) {
      graph6.addEdge({
        id: e.id, source: { cell: e.source }, target: { cell: e.target },
        attrs: { line: { stroke: T.accent, strokeWidth: 1.8, targetMarker: { name: "block", size: 8 } } },
        labels: e.label ? [{ position: 0.5, attrs: { label: { text: e.label, fill: T.text, fontSize: 11, textAnchor: "middle" },
          rect: { fill: T.surf, stroke: "none", rx: 4, ry: 4 } } }] : [],
        connector: { name: "smooth" }, router: { name: "normal" },
      });
    });
    graph6.centerContent();

    var sel = null;
    var panelRoot = ReactDOM.createRoot(panelHost);
    function renderPanel() {
      panelRoot.render(React.createElement(PropPanel, {
        graph: graph, sel: sel, onEdit: o.onEdit,
        onDel: function (items) {
          var ops = items.map(function (i) { return { op: i.kind === "class" ? "delete_class" : "delete_relation", ref: i.ref }; });
          if (o.onEdit) o.onEdit(ops).then(function () { sel = null; renderPanel(); }).catch(function () {});
        },
      }));
    }
    renderPanel();
    function setSelection(next) {
      sel = next;
      renderPanel();
      if (typeof o.onSelect === "function") o.onSelect(sel ? sel.ref : null);
    }
    graph6.on("node:click", function (args) { setSelection({ kind: "class", ref: args.node.id }); });
    graph6.on("edge:click", function (args) { setSelection({ kind: "relation", ref: args.edge.id }); });
    graph6.on("blank:click", function () { setSelection(null); });
    function persistLayout() {
      if (typeof o.onLayoutChange !== "function") return;
      var positions = {};
      graph6.getNodes().forEach(function (node) {
        var p = node.position(); positions[node.id] = { x: p.x, y: p.y };
      });
      Promise.resolve(o.onLayoutChange(positions)).catch(function () {});
    }
    graph6.on("node:mouseup", persistLayout);
    // Delete 键删除选中（X6 2.x 无 bindKey，用原生 keydown）
    var host = canvasHost;
    host.tabIndex = 0;
    host.addEventListener("keydown", function (ev) {
      if (ev.key !== "Delete" && ev.key !== "Backspace") return;
      var cells = graph6.getSelectedCells();
      if (!cells.length || !confirm("删除 " + cells.map(function (c) { return c.id; }).join("、") + "？")) return;
      ev.preventDefault();
      var ops = cells.map(function (c) {
        return c.isNode() ? { op: "delete_class", ref: c.id } : { op: "delete_relation", ref: c.id };
      });
      if (o.onEdit) o.onEdit(ops).then(function () { sel = null; renderPanel(); }).catch(function () {});
    });

    var bar = document.createElement("div");
    bar.style.cssText = "position:absolute;top:8px;left:8px;z-index:10;display:flex;gap:6px;background:" + T.surf + ";padding:6px;border-radius:8px;border:1px solid " + T.border;
    function mkBtn(text, fn) { var b = document.createElement("button"); b.className = "ghost"; b.textContent = text; b.onclick = fn; return b; }
    bar.append(
      mkBtn("自动布局", function () {
        var p2 = dagreLayout(
          graph.nodes.map(function (n) { return { id: n.id, width: nodeSize[n.id].width, height: nodeSize[n.id].height }; }),
          graph.edges.map(function (e) { return { source: e.source, target: e.target }; }));
        graph.nodes.forEach(function (n) { var c = graph6.getCellById(n.id); if (c) c.position(p2[n.id].x, p2[n.id].y); });
        graph6.centerContent();
        persistLayout();
      }),
      mkBtn("缩放适配", function () { graph6.zoomToFit({ padding: 40, maxScale: 1.5 }); }),
      mkBtn("导出 PNG", function () {
        graph6.toPNG(function (dataUri) { var a = document.createElement("a"); a.href = dataUri; a.download = "object_relation_graph.png"; a.click(); }, { padding: 20, backgroundColor: T.surf });
      }));
    canvasHost.appendChild(bar);

    return {
      graph: graph6,
      select: function (id) {
        var cell = graph6.getCellById(id);
        if (!cell) return false;
        graph6.cleanSelection(); graph6.select(cell);
        if (typeof graph6.centerCell === "function") graph6.centerCell(cell);
        setSelection(cell.isNode() ? { kind: "class", ref: id } : { kind: "relation", ref: id });
        return true;
      },
      fit: function () { graph6.zoomToFit({ padding: 40, maxScale: 1.5 }); },
      dispose: function () { graph6.dispose(); panelRoot.unmount(); el.innerHTML = ""; }
    };
  }

  window.X6D = { ready: ready, designer: designer };
})();
