/* charts-recharts.js — 本体工作台 Recharts 图表层（React 18 + Recharts 2.12，UMD 本地 vendor，零 CDN）
 *
 * 对外：window.RC = { ready(), metric(el, spec), meta(el, profiles, opts), unmount(el) }
 *   metric：指标值可视化。自动选型——时间序列→面积+折线；类别≤8 且是占比/计数→饼图+柱；否则横向柱；
 *           支持切换（柱/横柱/折线/面积/饼/雷达）、Top N、排序、Brush 缩放、导出 CSV。
 *   meta：数据源元数据可视化。每表：列角色分布（饼）、空值率/唯一度（柱，可点击选中列）、
 *         外键与主键摘要；点击列回调 onPick(table, column) 供编辑面板使用。
 * 颜色取页面 CSS 令牌，跟随明暗主题。
 */
(function () {
  const VENDOR = ["/console/vendor/", "./vendor/"];
  const FILES = ["react.production.min.js", "react-dom.production.min.js", "prop-types.min.js", "Recharts.js"];
  let loading = null;
  const roots = new WeakMap();

  function loadScript(src) {
    return new Promise((ok, bad) => {
      const s = document.createElement("script");
      s.src = src; s.onload = ok; s.onerror = () => bad(new Error("加载失败：" + src));
      document.head.appendChild(s);
    });
  }
  async function ready() {
    if (window.Recharts && window.React && window.ReactDOM) return;
    if (loading) return loading;
    loading = (async () => {
      for (const base of VENDOR) {
        try {
          for (const f of FILES) {
            const g = { "react.production.min.js": "React", "react-dom.production.min.js": "ReactDOM",
              "prop-types.min.js": "PropTypes", "Recharts.js": "Recharts" }[f];
            if (!window[g]) await loadScript(base + f);
          }
          if (window.Recharts) return;
        } catch (e) { /* 换下一个路径 */ }
      }
      throw new Error("Recharts 本地 vendor 加载失败（/console/vendor/Recharts.js）");
    })();
    return loading;
  }

  const css = (n, fb) => (getComputedStyle(document.documentElement).getPropertyValue(n).trim() || fb);
  const dark = () => {
    const t = document.documentElement.dataset.theme;
    return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  };
  const PAL = () => dark()
    ? ["#a1caff", "#ffaea7", "#90ca90", "#eec448", "#c4b5fd", "#f9a8d4", "#7dd3fc", "#fdba74"]
    : ["#0067ce", "#c0262c", "#2f7d33", "#836700", "#6d28d9", "#be185d", "#0369a1", "#c2410c"];
  const theme = () => ({
    text: css("--ax-text", dark() ? "#f1f1f1" : "#111"),
    sub: css("--ax-text-secondary", dark() ? "#c6c6c6" : "#474747"),
    grid: css("--ax-border", dark() ? "#3f3f46" : "#e4e4e7"),
    surf: css("--ax-surface", dark() ? "#222223" : "#fff"),
    accent: css("--ax-accent", dark() ? "#a1caff" : "#0067ce"),
    dim: dark() ? "#71717a" : "#9ca3af",
  });
  const fmt = v => v == null || isNaN(v) ? "—" : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });

  function mount(el, node) {
    let r = roots.get(el);
    if (!r) { r = ReactDOM.createRoot(el); roots.set(el, r); }
    r.render(node);
  }
  function unmount(el) { const r = roots.get(el); if (r) { r.unmount(); roots.delete(el); } }

  /* ================================================================ 指标图 */
  function MetricChart({ spec }) {
    const h = React.createElement, R = Recharts, T = theme(), C = PAL();
    const isSeries = !!spec.series;
    const auto = isSeries ? "area" : (spec.share || (spec.categories.length <= 6 && spec.count)) ? "pie"
      : spec.categories.length > 10 ? "hbar" : "bar";
    const [type, setType] = React.useState(auto);
    const [topN, setTopN] = React.useState(isSeries ? 0 : 20);
    const [sort, setSort] = React.useState(isSeries ? "none" : "desc");
    let rows = spec.categories.map((c, i) => ({ name: String(c ?? "空值"), value: spec.values[i] == null ? null : +spec.values[i] }));
    if (sort !== "none") rows = rows.slice().sort((a, b) => sort === "desc" ? (b.value ?? -Infinity) - (a.value ?? -Infinity) : (a.value ?? Infinity) - (b.value ?? Infinity));
    if (topN) rows = rows.slice(0, topN);
    const total = rows.reduce((s, r) => s + (r.value || 0), 0);
    const tip = h(R.Tooltip, {
      contentStyle: { background: T.surf, border: "1px solid " + T.grid, borderRadius: 8, color: T.text, fontSize: 12 },
      formatter: (v) => [fmt(v) + (spec.unit || "") + (total && type === "pie" ? `（${(v / total * 100).toFixed(1)}%）` : ""), spec.title],
      cursor: { fill: dark() ? "rgba(255,255,255,.06)" : "rgba(0,0,0,.04)" },
    });
    const axis = { stroke: T.sub, tick: { fill: T.sub, fontSize: 12 }, tickLine: false };
    const grid = h(R.CartesianGrid, { stroke: T.grid, strokeDasharray: "3 3", vertical: false });
    const many = rows.length > 12;
    const cell = (r, i) => h(R.Cell, { key: i, fill: type === "pie" ? C[i % C.length] : (i === 0 && sort === "desc" ? T.accent : T.dim) });
    let chart;
    if (type === "pie") {
      chart = h(R.PieChart, null, tip, h(R.Legend, { wrapperStyle: { fontSize: 12, color: T.sub } }),
        h(R.Pie, { data: rows, dataKey: "value", nameKey: "name", innerRadius: "48%", outerRadius: "78%", paddingAngle: 1,
          label: ({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`, labelLine: false, isAnimationActive: true },
          rows.map(cell)));
    } else if (type === "radar") {
      chart = h(R.RadarChart, { data: rows, outerRadius: "72%" }, tip,
        h(R.PolarGrid, { stroke: T.grid }), h(R.PolarAngleAxis, { dataKey: "name", tick: { fill: T.sub, fontSize: 11 } }),
        h(R.PolarRadiusAxis, { tick: { fill: T.sub, fontSize: 10 }, stroke: T.grid }),
        h(R.Radar, { dataKey: "value", stroke: T.accent, fill: T.accent, fillOpacity: 0.3 }));
    } else if (type === "hbar") {
      chart = h(R.BarChart, { data: rows, layout: "vertical", margin: { left: 8, right: 40 } }, tip,
        h(R.CartesianGrid, { stroke: T.grid, strokeDasharray: "3 3", horizontal: false }),
        h(R.XAxis, { type: "number", ...axis, tickFormatter: fmt }),
        h(R.YAxis, { type: "category", dataKey: "name", width: 130, ...axis, interval: 0 }),
        h(R.Bar, { dataKey: "value", radius: [0, 4, 4, 0], label: { position: "right", fill: T.sub, fontSize: 11, formatter: fmt } }, rows.map(cell)));
    } else if (type === "line" || type === "area") {
      const Comp = type === "line" ? R.LineChart : R.AreaChart, Ser = type === "line" ? R.Line : R.Area;
      chart = h(Comp, { data: rows, margin: { right: 16 } }, tip, grid,
        h("defs", null, h("linearGradient", { id: "rcg", x1: 0, y1: 0, x2: 0, y2: 1 },
          h("stop", { offset: "5%", stopColor: T.accent, stopOpacity: 0.35 }), h("stop", { offset: "95%", stopColor: T.accent, stopOpacity: 0 }))),
        h(R.XAxis, { dataKey: "name", ...axis, minTickGap: 16 }), h(R.YAxis, { ...axis, tickFormatter: fmt, width: 64 }),
        h(Ser, { type: "monotone", dataKey: "value", stroke: T.accent, strokeWidth: 2, fill: "url(#rcg)", dot: rows.length < 40, connectNulls: true }),
        rows.length > 24 ? h(R.Brush, { dataKey: "name", height: 22, stroke: T.accent, fill: T.surf, travellerWidth: 8 }) : null);
    } else {
      chart = h(R.BarChart, { data: rows, margin: { bottom: many ? 40 : 8 } }, tip, grid,
        h(R.XAxis, { dataKey: "name", ...axis, interval: 0, angle: many ? -35 : 0, textAnchor: many ? "end" : "middle", height: many ? 60 : 30 }),
        h(R.YAxis, { ...axis, tickFormatter: fmt, width: 64 }),
        h(R.Bar, { dataKey: "value", radius: [4, 4, 0, 0], label: rows.length <= 16 ? { position: "top", fill: T.sub, fontSize: 11, formatter: fmt } : false }, rows.map(cell)),
        rows.length > 30 ? h(R.Brush, { dataKey: "name", height: 22, stroke: T.accent, fill: T.surf }) : null);
    }
    const btn = (v, l) => h("button", { key: v, className: type === v ? "on" : "", onClick: () => setType(v) }, l);
    const csv = () => {
      const t = "name,value\n" + rows.map(r => `"${String(r.name).replace(/"/g, '""')}",${r.value ?? ""}`).join("\n");
      const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob(["\ufeff" + t], { type: "text/csv" }));
      a.download = (spec.title || "metric") + ".csv"; a.click();
    };
    return h("div", null,
      h("div", { className: "row", style: { alignItems: "center", gap: 8, marginBottom: 6 } },
        h("span", { className: "seg" }, btn("bar", "柱"), btn("hbar", "横柱"), btn("line", "折线"), btn("area", "面积"), btn("pie", "饼"), btn("radar", "雷达")),
        !isSeries && h("select", { value: sort, onChange: e => setSort(e.target.value), style: { flex: "0 0 110px" } },
          h("option", { value: "desc" }, "降序"), h("option", { value: "asc" }, "升序"), h("option", { value: "none" }, "原始顺序")),
        h("select", { value: topN, onChange: e => setTopN(+e.target.value), style: { flex: "0 0 110px" } },
          [0, 5, 10, 20, 50].map(n => h("option", { key: n, value: n }, n ? "Top " + n : "全部"))),
        h("span", { className: "muted auto" }, `${rows.length} 项 · 合计 ${fmt(total)}${spec.unit || ""}`),
        h("button", { className: "ghost", onClick: csv }, "导出 CSV")),
      h("div", { style: { width: "100%", height: spec.height || 360 } }, h(R.ResponsiveContainer, null, chart)));
  }

  /* ================================================================ 元数据图 */
  const ROLE_CN = { identifier: "标识", name: "名称", enum: "枚举", measure: "度量", temporal: "时间", text: "文本", boolean: "布尔" };
  function MetaView({ profiles, onPick, selected }) {
    const h = React.createElement, R = Recharts, T = theme(), C = PAL();
    const tables = Object.values(profiles);
    const [tab, setTab] = React.useState(tables[0] && tables[0].name);
    const [metric, setMetric] = React.useState("null");
    const p = profiles[tab] || tables[0];
    if (!p) return h("div", { className: "muted" }, "没有元数据");
    const cols = Object.values(p.columns);
    const fk = new Set((p.foreign_keys || []).map(f => f.column)), pk = new Set(p.primary_key || []);
    const roleCnt = {}; cols.forEach(c => roleCnt[c.role] = (roleCnt[c.role] || 0) + 1);
    const roleData = Object.entries(roleCnt).map(([k, v]) => ({ name: ROLE_CN[k] || k, value: v, key: k }));
    const data = cols.map(c => ({ name: c.name, label: c.label || c.comment || c.name, role: c.role,
      null: +(c.null_ratio * 100).toFixed(1), distinct: +(c.distinct_ratio * 100).toFixed(1), pk: pk.has(c.name), fk: fk.has(c.name) }));
    const sorted = data.slice().sort((a, b) => metric === "null" ? b.null - a.null : b.distinct - a.distinct);
    const roleColor = k => C[Object.keys(ROLE_CN).indexOf(k) % C.length] || T.dim;
    const tip = h(R.Tooltip, { contentStyle: { background: T.surf, border: "1px solid " + T.grid, borderRadius: 8, color: T.text, fontSize: 12 },
      formatter: (v, n, x) => [v + "%", metric === "null" ? "空值率" : "唯一度"],
      labelFormatter: (l) => { const d = data.find(x => x.name === l); return d ? `${l}${d.label !== l ? "（" + d.label + "）" : ""} · ${ROLE_CN[d.role] || d.role}${d.pk ? " · 主键" : ""}${d.fk ? " · 外键" : ""}` : l; } });
    const kpi = (n, l) => h("div", { className: "kpi", key: l }, h("div", { className: "n" }, n), h("div", { className: "l" }, l));
    const empty = cols.filter(c => c.null_ratio >= 1).length;
    const H = Math.max(260, Math.min(sorted.length, 60) * 18 + 40);
    return h("div", null,
      tables.length > 1 && h("div", { className: "seg", style: { marginBottom: 10 } },
        tables.map(t => h("button", { key: t.name, className: t.name === p.name ? "on" : "", onClick: () => setTab(t.name) }, t.name))),
      h("div", { className: "kpis" }, kpi(fmt(p.rows), "行数"), kpi(cols.length, "列数"), kpi((p.primary_key || []).join(",") || "无", "主键" + (p.pk_inferred ? "（推断）" : "")),
        kpi((p.foreign_keys || []).length, "外键"), kpi(empty, "全空列")),
      h("div", { style: { display: "grid", gridTemplateColumns: "minmax(240px,1fr) 2.2fr", gap: 16, alignItems: "start" } },
        h("div", null, h("div", { className: "muted", style: { marginBottom: 4 } }, "列角色分布"),
          h("div", { style: { height: 260 } }, h(R.ResponsiveContainer, null,
            h(R.PieChart, null, h(R.Tooltip, { contentStyle: { background: T.surf, border: "1px solid " + T.grid, borderRadius: 8, color: T.text, fontSize: 12 } }),
              h(R.Legend, { wrapperStyle: { fontSize: 12, color: T.sub } }),
              h(R.Pie, { data: roleData, dataKey: "value", nameKey: "name", innerRadius: "45%", outerRadius: "75%", paddingAngle: 1 },
                roleData.map((r, i) => h(R.Cell, { key: i, fill: roleColor(r.key) })))))),
          (p.foreign_keys || []).length ? h("div", { style: { marginTop: 8 } }, h("div", { className: "muted" }, "外键"),
            (p.foreign_keys || []).map(f => h("div", { key: f.column, style: { fontSize: 12 } },
              `${f.column} → ${f.ref_table}.${f.ref_column}`, h("span", { className: "muted" }, f.declared ? " 声明" : ` 推断 ${Math.round((f.containment || 0) * 100)}%`)))) : null),
        h("div", null,
          h("div", { className: "row", style: { alignItems: "center", marginBottom: 4 } },
            h("span", { className: "muted" }, "列质量（点击柱子编辑该列）"),
            h("span", { className: "seg auto" },
              h("button", { className: metric === "null" ? "on" : "", onClick: () => setMetric("null") }, "空值率"),
              h("button", { className: metric === "distinct" ? "on" : "", onClick: () => setMetric("distinct") }, "唯一度"))),
          h("div", { style: { height: H, maxHeight: 1120, overflowY: "auto" } },
            h("div", { style: { height: H } }, h(R.ResponsiveContainer, null,
              h(R.BarChart, { data: sorted.slice(0, 60), layout: "vertical", margin: { left: 4, right: 36 },
                onClick: (e) => e && e.activeLabel && onPick && onPick(p.name, e.activeLabel) }, tip,
                h(R.CartesianGrid, { stroke: T.grid, strokeDasharray: "3 3", horizontal: false }),
                h(R.XAxis, { type: "number", domain: [0, 100], unit: "%", stroke: T.sub, tick: { fill: T.sub, fontSize: 11 } }),
                h(R.YAxis, { type: "category", dataKey: "name", width: 170, interval: 0, stroke: T.sub,
                  tick: ({ x, y, payload }) => { const d = data.find(q => q.name === payload.value) || {};
                    return h("text", { x, y, dy: 4, textAnchor: "end", fontSize: 11, cursor: "pointer",
                      fill: selected && selected.column === payload.value ? T.accent : T.sub, fontWeight: d.pk ? 700 : 400 },
                      (d.pk ? "🔑 " : d.fk ? "🔗 " : "") + String(payload.value).slice(0, 24)); } }),
                h(R.Bar, { dataKey: metric, radius: [0, 4, 4, 0], cursor: "pointer",
                  label: { position: "right", fill: T.sub, fontSize: 10, formatter: v => v + "%" } },
                  sorted.slice(0, 60).map((d, i) => h(R.Cell, { key: i, fill: selected && selected.column === d.name ? T.accent : roleColor(d.role) })))))),
            sorted.length > 60 ? h("div", { className: "muted" }, `仅显示前 60 列（共 ${sorted.length} 列），可在下方表格查看全部`) : null))));
  }

  async function metric(el, spec) { await ready(); mount(el, React.createElement(MetricChart, { spec, key: spec.title + "|" + spec.categories.length })); }
  async function meta(el, profiles, opts = {}) { await ready(); mount(el, React.createElement(MetaView, { profiles, ...opts })); }
  window.RC = { ready, metric, meta, unmount };
})();
