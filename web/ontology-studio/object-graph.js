/** 对象驱动四列画布：Object → Property → Type → Function → Action */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
  const svgEl = (tag, attrs = {}, text = '') => {
    const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, String(v)));
    if (text && tag === 'text') n.textContent = text;
    return n;
  };

  const COL = {
    properties: { x: 320, title: '属性', color: '#38bdf8' },
    types: { x: 560, title: '类型', color: '#94a3b8' },
    functions: { x: 800, title: '函数', color: '#eab308' },
    actions: { x: 1040, title: '对象行动', color: '#34d399' },
  };
  const OBJ_X = 120;

  function layoutColumn(items, x, top = 90, gap = 56) {
    return items.map((it, i) => ({ ...it, x, y: top + i * gap }));
  }

  function renderObjectGraph(payload, opts = {}) {
    const root = $('objectGraphSvg');
    const detail = $('objectGraphDetail');
    const summary = $('objectGraphSummary');
    if (!root || !payload) return;
    root.replaceChildren();
    const obj = payload.object || {};
    const cols = payload.columns || {};
    const edges = payload.edges || [];

    const propN = layoutColumn(cols.properties || [], COL.properties.x);
    const typeN = layoutColumn(cols.types || [], COL.types.x);
    const fnN = layoutColumn(cols.functions || [], COL.functions.x);
    const actN = layoutColumn(cols.actions || [], COL.actions.x);
    const pos = new Map();
    pos.set('object', { x: OBJ_X, y: 200, node: { id: 'object', label: obj.label || obj.id } });
    [...propN, ...typeN, ...fnN, ...actN].forEach((n) => pos.set(n.id, { x: n.x, y: n.y, node: n }));

    const maxY = Math.max(280, ...[...pos.values()].map((p) => p.y + 40));
    const W = 1180, H = Math.max(420, maxY + 60);
    root.setAttribute('viewBox', `0 0 ${W} ${H}`);
    root.setAttribute('height', String(Math.min(H, 720)));

    const header = svgEl('g');
    const edgeLayer = svgEl('g');
    const nodeLayer = svgEl('g');

    Object.entries(COL).forEach(([, c]) => {
      header.append(svgEl('text', {
        x: c.x, y: 36, 'text-anchor': 'middle', fill: '#94a3b8',
        'font-size': 13, 'font-weight': 700,
      }, c.title));
      header.append(svgEl('line', {
        x1: c.x - 70, y1: 48, x2: c.x + 70, y2: 48,
        stroke: c.color, 'stroke-width': 2, opacity: 0.85,
      }));
    });

    const strokeOf = (kind) => (
      kind === 'derives' ? '#60a5fa'
        : kind === 'action' ? '#34d399'
          : kind === 'typed' || kind === 'returns' ? '#94a3b8'
            : '#64748b'
    );
    const dashOf = (kind) => (
      kind === 'derives' || kind === 'action' ? '6 5' : 'none'
    );

    edges.forEach((e) => {
      const a = pos.get(e.from), b = pos.get(e.to);
      if (!a || !b) return;
      const sx = a.x + (e.from === 'object' ? 88 : 78);
      const tx = b.x - 78;
      const mid = (sx + tx) / 2;
      edgeLayer.append(svgEl('path', {
        d: `M ${sx} ${a.y} C ${mid} ${a.y}, ${mid} ${b.y}, ${tx} ${b.y}`,
        fill: 'none', stroke: strokeOf(e.kind), 'stroke-width': 1.6,
        'stroke-dasharray': dashOf(e.kind), opacity: 0.8,
      }));
    });

    // center object
    const og = svgEl('g', { class: 'og-node', role: 'button', tabindex: '0', 'data-id': 'object' });
    og.append(svgEl('rect', {
      x: OBJ_X - 88, y: 200 - 32, width: 176, height: 64, rx: 10,
      fill: '#1e3a5f', stroke: '#3b82f6', 'stroke-width': 2,
    }));
    og.append(svgEl('text', {
      x: OBJ_X, y: 196, 'text-anchor': 'middle', fill: '#e2e8f0',
      'font-size': 14, 'font-weight': 700,
    }, String(obj.label || obj.id || 'Object').slice(0, 14)));
    og.append(svgEl('text', {
      x: OBJ_X, y: 216, 'text-anchor': 'middle', fill: '#93c5fd', 'font-size': 11,
    }, String(obj.id || '')));
    og.addEventListener('click', () => inspect(obj, 'object'));
    nodeLayer.append(og);

    const paint = (list, color) => {
      list.forEach((n) => {
        const g = svgEl('g', { class: 'og-node', role: 'button', tabindex: '0', 'data-id': n.id });
        g.append(svgEl('rect', {
          x: n.x - 78, y: n.y - 20, width: 156, height: 40, rx: 8,
          fill: '#111c2e', stroke: color, 'stroke-width': 1.5,
        }));
        g.append(svgEl('rect', {
          x: n.x - 78, y: n.y - 20, width: 4, height: 40, rx: 2, fill: color, stroke: 'none',
        }));
        const label = String(n.label || n.name || n.id);
        g.append(svgEl('text', {
          x: n.x - 68, y: n.y + 5, 'text-anchor': 'start', fill: '#e2e8f0', 'font-size': 12,
        }, label.length > 16 ? `${label.slice(0, 15)}…` : label));
        g.addEventListener('click', () => inspect(n, n.id.split(':')[0]));
        nodeLayer.append(g);
      });
    };
    paint(propN, COL.properties.color);
    paint(typeN, COL.types.color);
    paint(fnN, COL.functions.color);
    paint(actN, COL.actions.color);

    root.append(header, edgeLayer, nodeLayer);

    if (summary) {
      summary.textContent =
        `${propN.length} 属性 · ${typeN.length} 类型 · ${fnN.length} 函数 · ${actN.length} 行动`
        + (payload.source ? ` · 来源 ${payload.source}` : '');
    }
    if (detail && !opts.keepDetail) {
      inspect(obj, 'object');
    }

    function inspect(node, kind) {
      if (!detail) return;
      const q = obj.quality || {};
      if (kind === 'object') {
        detail.innerHTML =
          `<b>${esc(obj.label || obj.id)}</b>`
          + `<p class="smg-kind">${esc(obj.id)} · ${esc(obj.domain || '')}</p>`
          + `<p class="hint">${esc(obj.description || '')}</p>`
          + `<dl><dt>属性</dt><dd>${q.property_count ?? propN.length}</dd>`
          + `<dt>函数</dt><dd>${q.function_count ?? fnN.length}</dd>`
          + `<dt>行动</dt><dd>${q.action_count ?? actN.length}</dd>`
          + `<dt>未绑定源</dt><dd>${q.unbound_properties ?? '—'}</dd></dl>`;
        return;
      }
      const rows = Object.entries(node)
        .filter(([k]) => !['x', 'y', 'id'].includes(k))
        .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</dd>`)
        .join('');
      detail.innerHTML = `<b>${esc(node.label || node.name || node.id)}</b>`
        + `<p class="smg-kind">${esc(kind)}</p><dl>${rows}</dl>`;
    }
  }

  async function loadObjectCatalog(api, { demo = true } = {}) {
    const q = demo ? '?demo=1' : '';
    return api(`/ontology/objects${q}`);
  }

  async function loadObjectGraph(api, objectId, { demo = true } = {}) {
    const q = demo ? '?demo=1' : '';
    return api(`/ontology/objects/${encodeURIComponent(objectId)}/graph${q}`);
  }

  function renderObjectList(items, onSelect) {
    const el = $('objectCatalog');
    if (!el) return;
    if (!items?.length) {
      el.innerHTML = '<div class="empty">暂无对象</div>';
      return;
    }
    el.innerHTML = items.map((x) => {
      const q = x.quality || {};
      return `<button type="button" class="obj-item" data-id="${esc(x.id)}">`
        + `<b>${esc(x.label || x.id)}</b>`
        + `<span>${esc(x.id)}</span>`
        + `<small>${q.property_count || 0} 属性 · ${q.function_count || 0} 函数 · ${q.action_count || 0} 行动</small>`
        + `</button>`;
    }).join('');
    el.querySelectorAll('.obj-item').forEach((btn) => {
      btn.onclick = () => onSelect(btn.dataset.id);
    });
  }


  function fromSchema(schema) {
    if (!schema || typeof schema !== 'object') return null;
    const entityTypes = schema.entity_types || schema.classes || {};
    const attributes = schema.attributes || {};
    const relationTypes = schema.relation_types || {};
    const items = [];
    for (const [key, ent0] of Object.entries(entityTypes)) {
      const ent = (ent0 && typeof ent0 === 'object') ? ent0 : { label: String(ent0) };
      const eid = String(key);
      const properties = {};
      for (const [pk, pv0] of Object.entries(attributes)) {
        const pv = (pv0 && typeof pv0 === 'object') ? pv0 : {};
        const owner = String(pv.owner || pv.domain || '');
        if (owner && owner !== eid) continue;
        if (!owner) continue;
        properties[String(pk)] = {
          label: pv.label || pk,
          value_type: pv.range || pv.value_type || 'String',
          role: pv.role || 'text',
          source: pv.source || (pv.column ? { table: pv.table, column: pv.column } : undefined),
        };
      }
      for (const k of (ent.key || ent.identity || [])) {
        const ks = String(k);
        if (!properties[ks]) properties[ks] = { label: ks, value_type: 'String', role: 'identity' };
      }
      const relations = {};
      for (const [rk, rv0] of Object.entries(relationTypes)) {
        const rv = (rv0 && typeof rv0 === 'object') ? rv0 : {};
        const src = String(rv.source || rv.domain || '');
        if (!src || src !== eid) continue;
        relations[String(rk)] = {
          label: rv.label || rk,
          target: rv.target || rv.range || '',
          cardinality: rv.cardinality || '1:N',
        };
      }
      const unbound = Object.values(properties).filter((p) => !p.source).length;
      items.push({
        id: eid,
        label: String(ent.label || eid),
        description: String(ent.description || ''),
        domain: String(ent.domain || ent.source_table || ''),
        identity: [...(ent.key || ent.identity || [])].map(String),
        properties, relations, functions: {}, actions: {},
        quality: {
          property_count: Object.keys(properties).length,
          relation_count: Object.keys(relations).length,
          function_count: 0,
          action_count: 0,
          unbound_properties: unbound,
        },
      });
    }
    if (!items.length) return null;
    return { source: 'job-schema', items };
  }

  function graphFromObject(obj, source) {
    if (!obj) return null;
    const props = [], types = [], fns = [], acts = [], edges = [];
    const typeSeen = new Set();
    for (const [name, p] of Object.entries(obj.properties || {})) {
      const pid = 'prop:' + name;
      props.push({ id: pid, name, label: p.label || name, role: p.role, value_type: p.value_type, source: p.source });
      edges.push({ from: 'object', to: pid, kind: 'owns' });
      const vt = String(p.value_type || 'String');
      const tid = 'type:' + vt;
      if (!typeSeen.has(vt)) { typeSeen.add(vt); types.push({ id: tid, label: vt }); }
      edges.push({ from: pid, to: tid, kind: 'typed' });
    }
    for (const [name, f] of Object.entries(obj.functions || {})) {
      const fid = 'fn:' + name;
      fns.push({ id: fid, name, label: f.label || name, kind: f.kind, op: f.op || f.expr, on: f.on, output_type: f.output_type });
      const on = String(f.on || '');
      let linked = false;
      for (const part of on.replace(/\s/g, '').split(',')) {
        if (part && props.some((p) => p.id === 'prop:' + part)) {
          edges.push({ from: 'prop:' + part, to: fid, kind: 'derives' });
          linked = true;
        }
      }
      if (!linked) edges.push({ from: 'object', to: fid, kind: 'owns' });
    }
    for (const [name, a] of Object.entries(obj.actions || {})) {
      const aid = 'act:' + name;
      acts.push({ id: aid, name, label: a.label || name, policy: a.policy, effects: a.effects || [], params: a.params || [] });
      edges.push({ from: 'object', to: aid, kind: 'action' });
    }
    return {
      object: {
        id: obj.id, label: obj.label, description: obj.description, domain: obj.domain, quality: obj.quality,
      },
      columns: { properties: props, types, functions: fns, actions: acts },
      edges,
      source: source || obj.source || 'schema',
    };
  }

  window.ObjectGraph = {
    render: renderObjectGraph,
    loadCatalog: loadObjectCatalog,
    loadGraph: loadObjectGraph,
    renderList: renderObjectList,
    fromSchema,
    graphFromObject,
  };
})();
