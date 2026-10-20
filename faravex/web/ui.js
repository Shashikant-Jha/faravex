/* Faravex UI. Talks only to the local server (/api/*). No libraries. */
(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const NS = 'http://www.w3.org/2000/svg';
  const NODE_W = 184, NODE_H = 26, COL = 250, ROW = 42, PAD = 40;

  const ui = {
    form: $('[data-pathform]'), path: $('[data-path]'), recent: $('[data-recent]'), status: $('[data-status]'),
    svg: $('[data-map]'), view: $('[data-view]'), edges: $('[data-edges]'), nodes: $('[data-nodes]'), empty: $('[data-empty]'),
    side: $('[data-side]'), kind: $('[data-kind]'), sel: $('[data-sel]'), info: $('[data-info]'),
    code: $('[data-code]'), gutter: $('[data-gutter]'), prevnote: $('[data-prevnote]'),
    filter: $('[data-filter]'), relayout: $('[data-relayout]'), search: $('[data-search]'), copy: $('[data-copy]'), changes: $('[data-changes]'),
    results: $('[data-results]'), rhead: $('[data-rhead]'), rlist: $('[data-rlist]'), indexBtn: $('[data-index]'), indexInfo: $('[data-indexinfo]'),
    port: $('[data-port]'), trace: $('[data-trace]'), rcopy: $('[data-rcopy]'),
    sessions: $('[data-sessions]'), recinfo: $('[data-recinfo]'), reccopy: $('[data-reccopy]'), runs: $('[data-runs]'),
    compare: $('[data-compare]'), pin: $('[data-pin]'), cmp: $('[data-cmp]'),
    runsEmpty: $('[data-runsempty]'), recPane: $('[data-viewpane="record"]'),
    picker: $('[data-picker]'), crumb: $('[data-crumb]'), dirs: $('[data-dirs]'),
  };
  const state = {
    data: null, byId: new Map(), edges: [], show: { import: true, ref: true, semantic: true, unlinked: false },
    sel: null, view: { x: 0, y: 0, w: 1000, h: 600 }, bounds: null, files: new Map(),
    pins: new Map(), // id -> {x, y}: nodes the user dragged; they override the automatic layout
    search: null, // {q, hits, rank: Map(file -> rank), nodes: Set(bridge ids), edges: Set(edge items)}
    jump: null, // {path, start, end}: line range to show in the preview after a result is picked
  };

  const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const el = (tag, attrs = {}) => { const n = document.createElementNS(NS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); return n; };
  const base = (id) => id.slice(id.lastIndexOf('/') + 1);
  const dir = (id) => (id.includes('/') ? id.slice(0, id.lastIndexOf('/') + 1) : './');
  const fit = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);
  function status(msg, err = false) { ui.status.textContent = msg; ui.status.classList.toggle('err', err); }
  function showUnlinked() { state.show.unlinked = true; $('[data-show="unlinked"]').checked = true; }

  async function api(url, opts) {
    const r = await fetch(url, opts);
    const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  }

  // ---------- Recent folders (per-browser convenience) ----------
  const RECENT = 'faravex.recent';
  const recent = () => { try { return JSON.parse(localStorage.getItem(RECENT)) || []; } catch { return []; } };
  const showRecent = () => { ui.recent.innerHTML = recent().map((p) => `<option value="${esc(p)}">`).join(''); };
  function remember(p) {
    try { localStorage.setItem(RECENT, JSON.stringify([p, ...recent().filter((x) => x !== p)].slice(0, 10))); } catch { /* storage off */ }
    showRecent();
  }

  // ---------- Dragged node positions (per project, per browser) ----------
  const pinKey = (root) => 'faravex.pins:' + root;
  function loadPins(root) {
    try { return new Map(Object.entries(JSON.parse(localStorage.getItem(pinKey(root))) || {})); } catch { return new Map(); }
  }
  function savePins() {
    if (!state.data) return;
    try {
      if (state.pins.size) localStorage.setItem(pinKey(state.data.root), JSON.stringify(Object.fromEntries(state.pins)));
      else localStorage.removeItem(pinKey(state.data.root));
    } catch { /* storage off: positions last until reload */ }
    ui.relayout.disabled = !state.pins.size;
    ui.relayout.textContent = state.pins.size ? `Reset layout (${state.pins.size})` : 'Reset layout';
  }

  // ---------- Load ----------
  async function mapFolder(path) {
    if (!path) return status('Pick a project folder.', true);
    if (/^(https?|file):/i.test(path)) { // a page URL belongs in Record's box; the folder maps the page's scripts to files
      ui.port.value = path;
      ui.path.value = state.data?.root || recent()[0] || '';
      setView('record');
      return status(state.data ? 'Page URL moved to the Record box: press Record.'
        : 'Page URL moved to the Record box. First map the app\'s folder here (Browse), then press Record.', true);
    }
    status('Reading files…');
    try {
      const d = await api('/api/graph?path=' + encodeURIComponent(path));
      state.data = d;
      state.files.clear();
      ui.path.value = d.root;
      remember(d.root);
      state.pins = loadPins(d.root);
      savePins();
      prepare(d);
      draw(true);
      const counts = state.edges.reduce((c, e) => ((c[e.type] = (c[e.type] || 0) + 1), c), {});
      status(`${d.nodes.length} files · ${d.edges.length + d.semantic.length} links · read in ${d.ms} ms`);
      for (const k of ['import', 'ref', 'semantic']) $(`[data-count="${k}"]`).textContent = counts[k] || 0;
      $('[data-count="unlinked"]').textContent = d.nodes.filter((n) => !n.out.length && !n.in.length).length;
      const ix = d.index;
      ui.indexInfo.textContent = ix.built
        ? `index ${ix.chunks} chunks · ${ix.updated_at}${ix.stale_config ? ' · stale' : ''}`
        : 'not indexed: semantic links off';
      ui.indexBtn.textContent = ix.built ? 'Update index' : 'Index';
      ui.search.disabled = !ix.built;
      ui.changes.disabled = !ix.built;
      ui.search.placeholder = ix.built ? 'search: meaning or name' : 'index first to search';
      ui.indexBtn.disabled = false;
      initRecord();
    } catch (e) {
      status(e.message, true);
    }
  }

  function prepare(d) {
    state.byId = new Map(d.nodes.map((n) => [n.id, Object.assign(n, { out: [], in: [] })]));
    state.edges = [
      ...d.edges.map(([a, b, type]) => ({ a, b, type })),
      ...d.semantic.map(([a, b, score]) => ({ a, b, type: 'semantic', score })),
    ].filter((e) => state.byId.has(e.a) && state.byId.has(e.b));
    for (const e of state.edges) { state.byId.get(e.a).out.push(e); state.byId.get(e.b).in.push(e); }
    state.sel = null;
    clearSearch();
  }

  // ---------- Layout: break cycles, longest-path layers, barycenter ordering ----------
  function layout(nodes, edges) {
    const out = new Map(nodes.map((n) => [n.id, []]));
    edges.forEach((e) => out.get(e.a).push(e.b));
    const color = new Map(), order = [], keep = new Set();
    for (const n of nodes) {
      if (color.get(n.id)) continue;
      const stack = [[n.id, 0]];
      color.set(n.id, 1);
      while (stack.length) {
        const top = stack[stack.length - 1], [v, i] = top, kids = out.get(v);
        if (i < kids.length) {
          top[1]++;
          const w = kids[i];
          if (!color.get(w)) { color.set(w, 1); keep.add(v + '\n' + w); stack.push([w, 0]); }
          else if (color.get(w) === 2) keep.add(v + '\n' + w); // forward/cross edge; gray = back edge, dropped
        } else { color.set(v, 2); order.push(v); stack.pop(); }
      }
    }
    const layer = new Map(nodes.map((n) => [n.id, 0]));
    for (const v of order.reverse()) for (const w of out.get(v)) if (keep.has(v + '\n' + w)) layer.set(w, Math.max(layer.get(w), layer.get(v) + 1));

    const cols = [];
    for (const n of nodes) (cols[layer.get(n.id)] ||= []).push(n.id);
    cols.forEach((c) => c.sort());
    const pos = new Map();
    const index = () => cols.forEach((c) => c.forEach((id, i) => pos.set(id, i)));
    index();
    const preds = new Map(nodes.map((n) => [n.id, []])), succs = new Map(nodes.map((n) => [n.id, []]));
    edges.forEach((e) => { preds.get(e.b).push(e.a); succs.get(e.a).push(e.b); });
    const sweep = (list, nb) => list.forEach((c) => {
      const bc = new Map(c.map((id) => { const ns = nb.get(id); return [id, ns.length ? ns.reduce((s, x) => s + pos.get(x), 0) / ns.length : pos.get(id)]; }));
      c.sort((a, b) => bc.get(a) - bc.get(b)); c.forEach((id, i) => pos.set(id, i));
    });
    for (let k = 0; k < 2; k++) { sweep(cols.slice(1), preds); sweep(cols.slice(0, -1).reverse(), succs); }

    const tallest = Math.max(1, ...cols.map((c) => c.length));
    const xy = new Map();
    cols.forEach((c, li) => c.forEach((id, i) => xy.set(id, { x: PAD + li * COL, y: PAD + ((tallest - c.length) / 2 + i) * ROW, layer: li })));
    return { xy, w: PAD * 2 + (cols.length - 1) * COL + NODE_W, h: PAD * 2 + tallest * ROW };
  }

  // ---------- Draw ----------
  function visible() {
    const edges = state.edges.filter((e) => state.show[e.type]);
    const linked = new Set(edges.flatMap((e) => [e.a, e.b]));
    const nodes = [...state.byId.values()].filter((n) => state.show.unlinked || linked.has(n.id));
    return { nodes, edges };
  }

  function draw(refit) {
    if (!state.data) return;
    const { nodes, edges } = visible();
    const L = layout(nodes, edges);
    for (const [id, p] of state.pins) { const q = L.xy.get(id); if (q) L.xy.set(id, { ...q, x: p.x, y: p.y }); }
    state.xy = L.xy;
    state.bounds = bounds();
    ui.empty.hidden = nodes.length > 0;
    if (!nodes.length) ui.empty.firstElementChild.textContent = 'No links with the current filters. Turn on unlinked files to see everything.';

    const ef = document.createDocumentFragment();
    state.edgesOf = new Map(nodes.map((n) => [n.id, []]));
    state.edgeEls = edges.map((e) => {
      const p = el('path', { class: `edge ${e.type}`, d: edgePath(L.xy.get(e.a), L.xy.get(e.b)) });
      ef.append(p);
      const item = { e, el: p };
      state.edgesOf.get(e.a).push(item);
      if (e.b !== e.a) state.edgesOf.get(e.b).push(item);
      return item;
    });
    const nf = document.createDocumentFragment();
    state.nodeEls = new Map();
    for (const n of nodes) {
      const { x, y } = L.xy.get(n.id);
      const g = el('g', { class: `node ${n.kind}`, transform: `translate(${x} ${y})`, 'data-id': n.id });
      const title = el('title'); title.textContent = n.id;
      const t = el('text', { x: 10, y: 17 }); t.textContent = fit(base(n.id), 23);
      const dd = el('text', { class: 'dir', x: 1, y: -4 }); dd.textContent = fit(dir(n.id), 34);
      g.append(title, el('rect', { width: NODE_W, height: NODE_H }), t, dd);
      nf.append(g);
      state.nodeEls.set(n.id, g);
    }
    ui.edges.replaceChildren(ef);
    ui.nodes.replaceChildren(nf);
    if (refit) fitView(); else applyView();
    if (state.sel && state.nodeEls.has(state.sel)) select(state.sel, false); else clearSel();
    applyFilter();
    if (state.search) { linkHits(); applySearch(); }
  }

  // Routed from positions, not layers, so it stays right after a node is dragged.
  function edgePath(A, B) {
    const ay = A.y + NODE_H / 2, by = B.y + NODE_H / 2;
    if (B.x >= A.x + NODE_W + 8) { // target to the right: S-curve from right side to left side
      const x1 = A.x + NODE_W, x2 = B.x, mx = (x1 + x2) / 2;
      return `M${x1} ${ay}C${mx} ${ay} ${mx} ${by} ${x2} ${by}`;
    }
    // target left or in the same column: loop around the right side (keeps A->B and B->A apart)
    const x = Math.max(A.x, B.x) + NODE_W, bow = 40 + Math.abs(by - ay) * 0.15;
    return `M${A.x + NODE_W} ${ay}C${x + bow} ${ay} ${x + bow} ${by} ${B.x + NODE_W} ${by}`;
  }
  function bounds() {
    if (!state.xy.size) return { x: 0, y: 0, w: 1000, h: 600 };
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const p of state.xy.values()) { x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x); y1 = Math.max(y1, p.y); }
    return { x: x0 - PAD, y: y0 - PAD, w: x1 - x0 + NODE_W + PAD * 2, h: y1 - y0 + NODE_H + PAD * 2 };
  }
  function moveNode(id, x, y) {
    state.xy.set(id, { ...state.xy.get(id), x, y });
    state.nodeEls.get(id).setAttribute('transform', `translate(${x} ${y})`);
    for (const { e, el: p } of state.edgesOf.get(id)) p.setAttribute('d', edgePath(state.xy.get(e.a), state.xy.get(e.b)));
  }

  // ---------- View (pan / zoom) ----------
  function applyView() {
    const v = state.view;
    ui.svg.setAttribute('viewBox', `${v.x} ${v.y} ${v.w} ${v.h}`);
  }
  function aspect() { return ui.svg.clientHeight / Math.max(1, ui.svg.clientWidth); }
  // Fit box b into the canvas; leftPx keeps that many pixels on the left free (e.g. under the results panel).
  function fitView(b = state.bounds || { x: 0, y: 0, w: 1000, h: 600 }, leftPx = 0) {
    if (!ui.svg.clientWidth) { state.fitPending = true; return; } // map view hidden: fit when it is shown
    const W = Math.max(1, ui.svg.clientWidth), H = ui.svg.clientHeight, avail = Math.max(200, W - leftPx);
    let w = Math.max(b.w, 600), h = w * H / avail;
    if (h < b.h) { h = b.h; w = h * avail / H; }
    const vw = w * W / avail; // view width in units; its rightmost w units sit in the free area
    state.view = { x: b.x + (b.w - w) / 2 - (vw - w), y: b.y + (b.h - h) / 2, w: vw, h };
    applyView();
  }
  function center(id) {
    const p = state.xy.get(id);
    if (!p) return;
    const v = state.view, w = Math.min(v.w, 1600), h = w * aspect(); // zoom in to readable size if far out
    state.view = { x: p.x + NODE_W / 2 - w / 2, y: p.y + NODE_H / 2 - h / 2, w, h };
    applyView();
  }
  ui.svg.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const v = state.view, r = ui.svg.getBoundingClientRect();
    const k = Math.min(4, Math.max(0.25, Math.exp(ev.deltaY * 0.0015)));
    const w = Math.min(Math.max(v.w * k, 200), 60000), real = w / v.w;
    const fx = (ev.clientX - r.left) / r.width, fy = (ev.clientY - r.top) / r.height;
    state.view = { x: v.x + fx * v.w * (1 - real), y: v.y + fy * v.h * (1 - real), w, h: v.h * real };
    applyView();
  }, { passive: false });
  // Drag on a node moves it (and selects it, so its links stay lit); drag on empty space pans.
  let drag = null;
  ui.svg.addEventListener('pointerdown', (ev) => {
    if (ev.button !== 0) return;
    const node = ev.target.closest('.node');
    drag = { x: ev.clientX, y: ev.clientY, view: { ...state.view }, moved: false, node, start: node && { ...state.xy.get(node.dataset.id) } };
    ui.svg.setPointerCapture(ev.pointerId);
  });
  ui.svg.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    const k = drag.view.w / ui.svg.clientWidth;
    if (drag.node) {
      const id = drag.node.dataset.id;
      if (!drag.moved) {
        if (state.sel !== id) select(id, false);
        drag.node.parentNode.append(drag.node); // draw on top of its neighbours
        ui.svg.classList.add('is-dragging');
      }
      drag.moved = true;
      moveNode(id, drag.start.x + dx * k, drag.start.y + dy * k);
      return;
    }
    drag.moved = true;
    ui.svg.classList.add('is-panning');
    state.view = { ...drag.view, x: drag.view.x - dx * k, y: drag.view.y - dy * k };
    applyView();
  });
  function endDrag() {
    if (!drag) return;
    if (!drag.moved) { state.jump = null; drag.node ? select(drag.node.dataset.id) : clearSel(); }
    else if (drag.node) {
      const { x, y } = state.xy.get(drag.node.dataset.id);
      moveNode(drag.node.dataset.id, Math.round(x), Math.round(y)); // whole units: same as what is saved
      state.pins.set(drag.node.dataset.id, { x: Math.round(x), y: Math.round(y) });
      state.bounds = bounds();
      savePins();
    }
    drag = null;
    ui.svg.classList.remove('is-panning', 'is-dragging');
  }
  ui.svg.addEventListener('pointerup', endDrag);
  ui.svg.addEventListener('pointercancel', endDrag);
  addEventListener('resize', () => { state.view.h = state.view.w * aspect(); applyView(); });

  // ---------- Selection ----------
  function clearSel() {
    state.sel = null;
    ui.side.hidden = true;
    state.nodeEls?.forEach((g) => g.classList.remove('sel', 'rel', 'dim'));
    state.edgeEls?.forEach(({ el: p }) => p.classList.remove('lit', 'dim'));
  }

  const reach = (id, dirn) => { // transitive closure over import edges
    const seen = new Set(), stack = [id];
    while (stack.length) {
      const n = state.byId.get(stack.pop());
      for (const e of n[dirn]) if (e.type === 'import') { const x = dirn === 'out' ? e.b : e.a; if (!seen.has(x)) { seen.add(x); stack.push(x); } }
    }
    seen.delete(id);
    return seen;
  };

  function select(id, scroll = true) {
    const n = state.byId.get(id);
    if (!n) return;
    state.sel = id;
    const near = new Set([id]);
    state.edgeEls.forEach(({ e, el: p }) => {
      const hit = e.a === id || e.b === id;
      if (hit) near.add(e.a === id ? e.b : e.a);
      p.classList.toggle('lit', hit);
      p.classList.toggle('dim', !hit);
    });
    state.nodeEls.forEach((g, gid) => {
      g.classList.toggle('sel', gid === id);
      g.classList.toggle('rel', gid !== id && near.has(gid));
      g.classList.toggle('dim', !near.has(gid));
    });
    if (scroll && (!inView(id) || state.view.w > 1600)) center(id);
    showInfo(n);
    showFile(n);
  }
  function inView(id) {
    const p = state.xy.get(id), v = state.view;
    return p && p.x > v.x && p.x + NODE_W < v.x + v.w && p.y > v.y && p.y + NODE_H < v.y + v.h;
  }

  const link = (id, extra = '') => `<button type="button" data-go="${esc(id)}" title="${esc(id)}">${esc(base(id))}</button>${extra}`;
  const list = (items) => (items.length ? `<span class="links">${items.join('')}</span>` : '<span class="muted">—</span>');

  function showInfo(n) {
    ui.side.hidden = false;
    ui.kind.textContent = n.kind === 'doc' ? 'doc / spec' : n.kind;
    ui.sel.textContent = n.id;
    const by = (arr, type, key) => arr.filter((e) => e.type === type).map((e) => e[key]);
    const sem = [...n.out, ...n.in].filter((e) => e.type === 'semantic')
      .sort((a, b) => b.score - a.score).map((e) => link(e.a === n.id ? e.b : e.a, ` <small>${e.score.toFixed(2)}</small>`));
    const down = reach(n.id, 'out'), up = reach(n.id, 'in');
    const sliceLines = [n.id, ...down].reduce((s, id) => s + state.byId.get(id).lines, 0);
    const rows = [
      ['about', n.summary ? esc(n.summary) : '<span class="muted">—</span>'],
      ['lines', n.lines],
      ['imports', list(by(n.out, 'import', 'b').map((id) => link(id)))],
      ['used by', list(by(n.in, 'import', 'a').map((id) => link(id)))],
    ];
    if (n.kind === 'doc') rows.push(['mentions', list(by(n.out, 'ref', 'b').map((id) => link(id)))]);
    rows.push(['named in', list(by(n.in, 'ref', 'a').map((id) => link(id)))]);
    if (sem.length) rows.push(['semantic', list(sem)]);
    if (n.external.length) rows.push(['packages', esc(n.external.join(', '))]);
    if (n.kind === 'code') {
      rows.push(['affects', up.size ? `${up.size} file${up.size > 1 ? 's' : ''} import it (directly or not)` : '<span class="muted">nothing imports it</span>']);
      rows.push(['slice', `${down.size + 1} file${down.size ? 's' : ''} · ${sliceLines} lines`]);
    }
    ui.info.innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
  }
  ui.info.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-go]');
    if (!b) return;
    if (!state.nodeEls.has(b.dataset.go)) { showUnlinked(); draw(false); }
    select(b.dataset.go);
  });

  // ---------- File preview ----------
  const HASH = /\.(py|sh|bash|ps1|psm1|rb|pl|r|ya?ml|toml|ini|cfg|conf|dockerfile|makefile)$|(^|\/)(dockerfile|makefile)$/i;
  function highlight(path, text) {
    const t = esc(text);
    if (/\.(md|mdx|rst|txt|adoc)$/i.test(path)) return t;
    const rx = HASH.test(path)
      ? /("""[\s\S]*?(?:"""|(?![\s\S]))|#.*$)|("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')/gm
      : /(\/\*[\s\S]*?(?:\*\/|(?![\s\S]))|\/\/.*$|&lt;!--[\s\S]*?(?:--&gt;|(?![\s\S])))|("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`[^`]*`)/gm;
    return t.replace(rx, (m, c, s) => (c ? `<span class="tk-c">${c}</span>` : `<span class="tk-s">${s}</span>`));
  }
  async function showFile(n) {
    const key = n.id;
    ui.prevnote.textContent = '';
    try {
      let f = state.files.get(key);
      if (!f) {
        f = await api(`/api/file?root=${encodeURIComponent(state.data.root)}&path=${encodeURIComponent(n.id)}`);
        state.files.set(key, f);
      }
      if (state.sel !== key) return;
      ui.code.innerHTML = highlight(n.id, f.text);
      const jump = state.jump && state.jump.path === key ? state.jump : null;
      const count = f.text.split('\n').length;
      ui.gutter.innerHTML = Array.from({ length: count }, (_, i) => (jump && i + 1 >= jump.start && i + 1 <= jump.end ? `<span class="mk">${i + 1}</span>` : i + 1)).join('\n');
      ui.prevnote.textContent = f.truncated ? `first 400 of ${f.lines} lines` : '';
      if (jump && jump.start > count) ui.prevnote.textContent = `match at line ${jump.start}, past the first 400 lines`;
      const box = ui.code.closest('.code');
      box.scrollTo(0, jump ? Math.max(0, (jump.start - 1) * (ui.gutter.scrollHeight / Math.max(1, count)) - 48) : 0);
    } catch (e) {
      ui.code.textContent = e.message;
      ui.gutter.textContent = '';
    }
  }

  // ---------- Filter ----------
  function applyFilter() {
    const q = ui.filter.value.trim().toLowerCase();
    state.nodeEls?.forEach((g, id) => g.classList.toggle('hit', !!q && id.toLowerCase().includes(q)));
  }
  ui.filter.addEventListener('input', applyFilter);
  ui.filter.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' || !state.nodeEls) return;
    const q = ui.filter.value.trim().toLowerCase();
    const id = q && [...state.nodeEls.keys()].find((x) => x.toLowerCase().includes(q));
    if (id) { select(id, false); center(id); }
  });

  // ---------- Semantic search overlay ----------
  async function runSearch(q) {
    if (!state.data || !q) return;
    status('Searching (re-embeds changed files first)…');
    try {
      const r = await api(`/api/search?root=${encodeURIComponent(state.data.root)}&q=${encodeURIComponent(q)}`);
      const rank = new Map();
      for (const h of r.hits) if (!rank.has(h.path) && state.byId.has(h.path)) rank.set(h.path, rank.size + 1);
      const items = r.hits.map((h) => ({ path: h.path, start: h.start, end: h.end,
        html: `<b>${rank.get(h.path) || '·'}</b>${esc(fit(h.path, 44))}:${h.start}-${h.end}<small>${h.score.toFixed(3)}</small>`
          + (h.match !== 'semantic' ? `<small class="kw" title="${h.match === 'both' ? 'exact text and meaning' : 'exact text'}">${h.match === 'both' ? 'text+meaning' : 'text'}</small>` : '')
          + `<code>${esc(h.scope || h.preview.split('\n').find((l) => l.trim()) || '')}</code>` }));
      showOverlay({ kind: 'search', items, rank, changed: new Set() }, `${r.hits.length} matches in ${rank.size} files for “${q}”`);
    } catch (e) { status(e.message, true); }
  }
  // Changes mode: uncommitted edits (Δ) plus the files that use the changed names or hold similar code (numbered).
  async function runChanges() {
    if (!state.data) return;
    status('Reading git changes (re-embeds changed files first)…');
    try {
      const r = await api(`/api/diff?root=${encodeURIComponent(state.data.root)}`);
      if (!r.changes.length) { clearSearch(); return status(`No changes: ${r.what}`); }
      const rank = new Map(), changed = new Set(r.changes.map((c) => c.path).filter((p) => state.byId.has(p)));
      const num = (p) => { if (state.byId.has(p) && !changed.has(p) && !rank.has(p)) rank.set(p, rank.size + 1); return rank.get(p) || '·'; };
      changed.forEach((p) => rank.set(p, 'Δ'));
      const items = [];
      for (const c of r.changes) {
        items.push({ path: c.path, start: c.start || 1, end: c.end || 1, cls: 'chg',
          html: `<b class="delta">Δ</b>${esc(fit(c.path, 40))}${c.status === 'deleted' ? ' (deleted)' : `:${c.start}-${c.end}`}`
            + `<small>+${c.added} −${c.removed}${c.status !== 'modified' ? ' · ' + c.status : ''}</small><code>${esc(c.scope || '')}</code>` });
        for (const u of c.usages) items.push({ path: u.path, start: u.lines[0], end: u.lines[0], cls: 'sub',
          html: `<b>${num(u.path)}</b><span class="tag">uses ${esc(c.names.join(', ').slice(0, 30))}</span>${esc(fit(u.path, 36))}:${u.lines.slice(0, 6).join(',')}` });
        for (const m of c.similar) items.push({ path: m.path, start: m.start, end: m.end, cls: 'sub',
          html: `<b>${num(m.path)}</b><span class="tag">similar</span>${esc(fit(m.path, 36))}:${m.start}-${m.end}<small>${m.score.toFixed(2)}</small>`
            + (m.scope ? `<code>${esc(m.scope)}</code>` : '') });
      }
      for (const t of r.tests) items.push({ path: t, start: 1, end: 1, cls: 'sub', html: `<b>${num(t)}</b><span class="tag test">test</span>${esc(fit(t, 44))}` });
      showOverlay({ kind: 'changes', items, rank, changed },
        `${r.changes.length} changes in ${changed.size} files · ${rank.size - changed.size} related files · ${r.tests.length} tests likely affected`);
    } catch (e) { status(e.message, true); }
  }
  function showOverlay(s, msg) {
    state.search = { ...s, nodes: new Set(), edges: new Set() };
    if ([...s.rank.keys()].some((id) => !state.nodeEls.has(id))) showUnlinked(); // a hit hides among unlinked files
    clearSel();
    draw(false); // re-applies the overlay
    renderResults();
    const ids = [...s.rank.keys()].filter((id) => state.xy.has(id));
    if (ids.length) fitView(boxOf(ids), ui.results.offsetWidth + 24);
    status(msg);
  }
  // Light the shortest import/doc paths (up to 3 links) between hit files: shows whether they form one subsystem.
  function linkHits() {
    const s = state.search, adj = new Map();
    s.nodes = new Set(); s.edges = new Set();
    for (const item of state.edgeEls) {
      for (const [x, y] of [[item.e.a, item.e.b], [item.e.b, item.e.a]]) (adj.get(x) || adj.set(x, []).get(x)).push([y, item]);
    }
    const hits = [...s.rank.keys()].filter((id) => state.nodeEls.has(id)).slice(0, 12), want = new Set(hits);
    for (const src of hits) {
      const prev = new Map([[src, null]]);
      let layer = [src];
      for (let d = 0; d < 3 && layer.length; d++) {
        const next = [];
        for (const v of layer) for (const [w, item] of adj.get(v) || []) if (!prev.has(w)) { prev.set(w, [v, item]); next.push(w); }
        layer = next;
      }
      for (const dst of want) {
        if (dst === src || !prev.has(dst)) continue;
        for (let v = dst; prev.get(v); v = prev.get(v)[0]) {
          s.edges.add(prev.get(v)[1]);
          if (!want.has(v)) s.nodes.add(v);
        }
      }
    }
  }
  function applySearch() {
    const s = state.search;
    ui.svg.classList.toggle('searching', !!s);
    state.nodeEls?.forEach((g, id) => {
      g.classList.toggle('found', !!s && s.rank.has(id));
      g.classList.toggle('changed', !!s && s.changed.has(id));
      g.classList.toggle('raised', !!s && !!s.raised?.has(id));
      g.classList.toggle('bridge', !!s && s.nodes.has(id));
      g.querySelector('.rank')?.remove();
      if (s && s.rank.has(id)) {
        const t = el('text', { class: 'rank', x: NODE_W + 6, y: 17 });
        t.textContent = s.rank.get(id);
        g.append(t);
      }
    });
    state.edgeEls?.forEach((item) => item.el.classList.toggle('path', !!s && s.edges.has(item)));
  }
  function renderResults() {
    const s = state.search;
    ui.results.hidden = !s;
    if (!s) return;
    const linked = s.edges.size ? ` · ${s.edges.size} connecting links` : s.rank.size > 1 ? ' · not connected' : '';
    ui.rhead.textContent = ({ changes: 'Changes · ', trace: 'Trace · ' }[s.kind] || '') + `${s.rank.size} files${linked}`;
    ui.rcopy.hidden = s.kind !== 'trace';
    ui.rlist.innerHTML = s.items.map((it, i) => `<li class="${it.cls || ''}"><button type="button" data-hit="${i}" title="${esc(it.path)}">${it.html}</button></li>`).join('');
  }
  function clearSearch() {
    if (!state.search) return;
    state.search = null;
    state.jump = null;
    applySearch();
    renderResults();
  }
  function boxOf(ids) {
    const ps = ids.map((id) => state.xy.get(id));
    const x0 = Math.min(...ps.map((p) => p.x)), y0 = Math.min(...ps.map((p) => p.y));
    const x1 = Math.max(...ps.map((p) => p.x)), y1 = Math.max(...ps.map((p) => p.y));
    return { x: x0 - PAD * 3, y: y0 - PAD * 2, w: x1 - x0 + NODE_W + PAD * 6, h: y1 - y0 + NODE_H + PAD * 4 };
  }
  ui.search.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') runSearch(ui.search.value.trim()); });
  ui.search.addEventListener('search', () => { if (!ui.search.value) clearSearch(); }); // the field's clear (x) button
  $('[data-rclear]').addEventListener('click', clearSearch);
  ui.changes.addEventListener('click', runChanges);
  ui.rlist.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-hit]');
    if (!b) return;
    const h = state.search.items[+b.dataset.hit];
    if (!state.byId.has(h.path)) return status(`${h.path} is not on the map (deleted, or not a project file)`, true);
    state.jump = { path: h.path, start: h.start, end: h.end };
    if (!state.nodeEls.has(h.path)) { showUnlinked(); draw(false); }
    select(h.path);
  });

  // ---------- Copy context (for chats and agents without the MCP tools) ----------
  async function copyText(text, btn) {
    try { await navigator.clipboard.writeText(text); } catch {
      const ta = Object.assign(document.createElement('textarea'), { value: text });
      document.body.append(ta); ta.select(); document.execCommand('copy'); ta.remove();
    }
    const label = btn.textContent;
    btn.textContent = 'Copied ✓';
    setTimeout(() => { btn.textContent = label; }, 1500);
  }
  const copied = (what, chars) => status(`Copied ${what}: ${chars.toLocaleString()} chars (~${Math.round(chars / 4).toLocaleString()} tokens)`);
  ui.copy.addEventListener('click', async () => {
    if (!state.sel) return;
    try {
      const r = await api(`/api/context?root=${encodeURIComponent(state.data.root)}&path=${encodeURIComponent(state.sel)}`);
      await copyText(r.text, ui.copy);
      copied(`context for ${base(state.sel)}`, r.chars);
    } catch (e) { status(e.message, true); }
  });

  // ---------- Views: Map / Record (left rail) ----------
  function setView(name) {
    $$('[data-viewpane]').forEach((p) => { p.hidden = p.dataset.viewpane !== name; });
    $$('[data-viewbtn]').forEach((b) => { b.classList.toggle('on', b.dataset.viewbtn === name); b.setAttribute('aria-pressed', b.dataset.viewbtn === name); });
    try { localStorage.setItem('faravex.view', name); } catch { /* storage off */ }
    if (name === 'map') { // the canvas had no size while hidden
      if (state.fitPending) { state.fitPending = false; fitView(); } else { state.view.h = state.view.w * aspect(); applyView(); }
    } else if (name === 'record' && rec.trace) renderRuns(rec.trace);
    clearInterval(calls.timer);
    calls.timer = name === 'calls' ? (loadCalls(), setInterval(loadCalls, 2000)) : null;
  }
  $$('[data-viewbtn]').forEach((b) => b.addEventListener('click', () => setView(b.dataset.viewbtn)));

  // ---------- Calls view: every MCP tool call (any client, any server process), time and rough token cost ----------
  const calls = { timer: null, sig: '' };
  const num = (n) => Math.round(n).toLocaleString();
  const when = (sec) => new Date(sec * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  async function loadCalls(force) {
    let r;
    try { r = await api('/api/calls'); } catch (e) { status(e.message, true); return; }
    const sig = r.count + ':' + (r.calls[0]?.at || '');
    if (sig === calls.sig && force !== true) return; // nothing new: keep the scroll position
    calls.sig = sig;
    const sum = (k) => r.tools.reduce((s, t) => s + t[k], 0);
    $('[data-callinfo]').textContent = r.count
      ? `${num(r.count)} calls · ~${num(sum('out'))} tokens returned · ~${num(sum('in'))} sent (≈4 characters a token)` : '';
    const box = $('[data-calls]');
    if (!r.count) {
      box.innerHTML = `<div class="runs-empty muted"><p>No MCP calls yet.</p><p>Every faravex tool call from Claude Code, Copilot or Cline shows up here with its time and rough token cost (out = the result the model reads).</p><p><small>${esc(r.log)}</small></p></div>`;
      return;
    }
    const th = (cols) => `<tr>${cols.map(([c, n]) => `<th${n ? ' class="n"' : ''}>${c}</th>`).join('')}</tr>`;
    box.innerHTML = `<div class="rgroup"><p class="rgroup-head"><b>Per tool</b><small>every call in the log, costliest first</small></p>
      <table class="calls"><thead>${th([['tool'], ['calls', 1], ['~tokens out', 1], ['avg out', 1], ['~tokens in', 1], ['avg ms', 1]])}</thead><tbody>
      ${r.tools.map((t) => `<tr><td>${esc(t.tool)}</td><td class="n">${num(t.calls)}</td><td class="n tok">${num(t.out)}</td>
        <td class="n">${num(t.out / t.calls)}</td><td class="n">${num(t.in)}</td><td class="n">${num(t.ms / t.calls)}</td></tr>`).join('')}
      </tbody></table></div>
      <div class="rgroup"><p class="rgroup-head"><b>Recent calls</b><small>newest first${r.count > r.calls.length ? `, last ${r.calls.length}` : ''}</small></p>
      <table class="calls"><thead>${th([['when'], ['tool'], ['ms', 1], ['~out', 1], ['~in', 1], ['arguments']])}</thead><tbody>
      ${r.calls.map((c) => `<tr><td class="muted">${when(c.at)}</td><td>${esc(c.tool)}</td><td class="n">${num(c.ms)}</td>
        <td class="n tok" title="${num(c.chars)} characters">${num(c.out)}</td><td class="n">${num(c.in)}</td>
        <td class="args" title="${esc(c.error || c.args)}">${c.error ? `<span class="err">${esc(c.error)}</span> ` : ''}${esc(c.args)}</td></tr>`).join('')}
      </tbody></table></div>`;
  }
  $('[data-callsref]').addEventListener('click', () => loadCalls(true));

  // ---------- Record view (Python 3.12+ apps, web pages): one card per run (request, browser task), grouped by entry point, with timings ----------
  const rec = { timer: null, trace: null, sig: '', toggled: new Map(), full: new Set(), // toggled: run -> expanded
    idleText: ui.runsEmpty.firstElementChild.textContent, sessions: [], apps: {}, pinned: new Set(), cmpKey: '' };
  const portKey = () => 'faravex.port:' + state.data.root;
  const traceUrl = (q) => `/api/trace?root=${encodeURIComponent(state.data.root)}&${q}`;
  const tracePost = (action) => api('/api/trace', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: state.data.root, action, ...target() }) });
  // The box takes a Python app's port or a page URL (recorded in the browser).
  const target = () => { const v = ui.port.value.trim(); return /^(https?|file):/i.test(v) ? { url: v, port: null } : { url: null, port: +v || null }; };
  const msText = (v) => (v == null ? 'running' : v < 10 ? v.toFixed(2) + ' ms' : v < 10000 ? v.toFixed(1) + ' ms' : (v / 1000).toFixed(2) + ' s');
  const clock = (sec) => { const d = new Date(sec * 1000); return d.toLocaleTimeString([], { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0'); };
  const sessionText = (s) => s.replace(/^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)$/, '$1-$2-$3 $4:$5:$6');

  function setRecording(on) {
    clearInterval(rec.timer);
    rec.timer = on ? setInterval(pollTrace, 1000) : null;
    ui.trace.textContent = on ? 'Stop' : 'Record';
    ui.trace.classList.toggle('rec', on);
    $('[data-viewbtn="record"]').classList.toggle('rec', on); // visible from the map view too
    ui.port.disabled = on;
    ui.sessions.disabled = on || !ui.sessions.options.length || !ui.sessions.value;
    refreshCompare();
  }
  async function initRecord() { // a folder was mapped: its recordings, or the one still running
    setRecording(false);
    Object.assign(rec, { trace: null, sig: '' });
    rec.toggled.clear(); rec.full.clear();
    ui.trace.disabled = false; // tracing reads the running app, not the index
    try { ui.port.value = localStorage.getItem(portKey()) || ''; } catch { /* storage off */ }
    renderRuns(null);
    ui.recinfo.textContent = '';
    try {
      const s = await api(traceUrl('action=status'));
      fillSessions(s.sessions, s.apps, s.pinned);
      if (s.recording) { ui.port.value = s.recording.port || ui.port.value; setRecording(true); pollTrace(); } else if (s.sessions.length) loadTrace(s.sessions.at(-1));
    } catch { fillSessions([]); }
  }
  const sessionLabel = (s) => (rec.pinned.has(s) ? 'pinned · ' : '') + sessionText(s) + (rec.apps[s] ? ' · ' + rec.apps[s] : '');
  function fillSessions(list, apps = {}, pinned = []) {
    Object.assign(rec, { sessions: list, apps, pinned: new Set(pinned) });
    const keep = ui.sessions.value;
    ui.sessions.innerHTML = [...list].reverse().map((s) => `<option value="${esc(s)}">${esc(sessionLabel(s))}</option>`).join('')
      || '<option value="">no recordings yet</option>';
    if (list.includes(keep)) ui.sessions.value = keep;
    ui.sessions.disabled = !list.length || !!rec.timer;
    refreshCompare();
  }
  async function loadTrace(session) {
    try { showRuns(await api(traceUrl('action=show&session=' + encodeURIComponent(session)))); ui.sessions.value = session; } catch (e) { status(e.message, true); }
  }

  // ---------- Compare two recordings of this folder (both stay on this machine) ----------
  function refreshCompare() { // the shown recording is "this"; the picker lists the others
    const cur = rec.trace && !rec.trace.live && !rec.timer ? rec.trace.session : '';
    const keep = ui.compare.value;
    const others = rec.sessions.filter((s) => s !== cur).reverse();
    ui.compare.innerHTML = '<option value="">compare with…</option>'
      + others.map((s) => `<option value="${esc(s)}">${esc(sessionLabel(s))}</option>`).join('');
    ui.compare.value = others.includes(keep) ? keep : '';
    ui.compare.disabled = !cur || !others.length;
    ui.pin.disabled = !cur;
    ui.pin.textContent = cur && rec.pinned.has(cur) ? 'Unpin' : 'Pin';
    loadCompare();
  }
  async function loadCompare() {
    const cur = ui.compare.disabled ? '' : rec.trace.session, against = ui.compare.value, key = cur && against ? cur + '|' + against : '';
    if (key === rec.cmpKey) return; // already shown (refreshCompare runs on every poll)
    rec.cmpKey = key;
    if (!key) { ui.cmp.hidden = true; ui.cmp.innerHTML = ''; return; }
    try {
      const c = await api(traceUrl(`action=compare&session=${encodeURIComponent(cur)}&base=${encodeURIComponent(against)}`));
      if (rec.cmpKey === key) renderCompare(c);
    } catch (e) { status(e.message, true); ui.cmp.hidden = true; rec.cmpKey = ''; }
  }
  const signed = (v) => (v > 0 ? '+' : v < 0 ? '−' : '±') + msText(Math.abs(v));
  function renderCompare(c) {
    const rows = c.rows.map((r, i) => {
      const x = r.base, y = r.new, both = x && y;
      const moved = r.moved.length ? `<tr class="cmp-moved" data-for="${i}" hidden><td colspan="6">time moved (self time per run): `
        + r.moved.map((m) => `<span><b>${esc(m.name)}</b> <i>${esc(base(m.file))}</i> ${signed(m.delta)} <small>(${msText(m.base)} → ${msText(m.new)})</small></span>`).join('')
        + '</td></tr>' : '';
      return `<tr class="cmp-row v-${r.verdict.replace(' ', '-')}" data-i="${i}"${r.moved.length ? ' tabindex="0" title="Show where the time moved"' : ''}>`
        + `<td class="lab" title="${esc(r.label)}">${esc(r.label)}</td><td class="n">${x ? x.runs : '–'} / ${y ? y.runs : '–'}</td>`
        + `<td class="n">${both ? `${msText(x.median)} → ${msText(y.median)}` : msText((x || y).median)}</td>`
        + `<td class="n">${r.delta != null ? `${signed(r.delta)} <small>${r.pct > 0 ? '+' : ''}${r.pct}%</small>` : ''}</td>`
        + `<td class="n">${both ? `${msText(x.p95)} → ${msText(y.p95)}` : ''}</td><td class="verdict">${r.verdict}</td></tr>${moved}`;
    }).join('');
    ui.cmp.innerHTML = `<p class="rgroup-head"><b>Comparison</b><small>base ${esc(sessionText(c.base.session))} (${c.base.runs} runs) → this recording `
      + `${esc(sessionText(c.new.session))} (${c.new.runs} runs) · slower / faster: ≥ ${c.rule.min_runs} runs on each side and the median changed `
      + `≥ ${c.rule.min_pct}% and ≥ ${c.rule.min_ms} ms · click a row for where the time moved</small></p>`
      + c.warnings.map((w) => `<p class="cmp-warn">${esc(w)}</p>`).join('')
      + '<table class="calls cmp-table"><thead><tr><th>entry point</th><th class="n">runs</th><th class="n">median</th><th class="n">change</th>'
      + `<th class="n">p95</th><th>verdict</th></tr></thead><tbody>${rows}</tbody></table>`;
    ui.cmp.hidden = false;
  }
  function toggleMoved(tr) {
    const m = tr && ui.cmp.querySelector(`.cmp-moved[data-for="${tr.dataset.i}"]`);
    if (m) m.hidden = !m.hidden;
  }
  ui.cmp.addEventListener('click', (ev) => toggleMoved(ev.target.closest('.cmp-row')));
  ui.cmp.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggleMoved(ev.target.closest('.cmp-row')); } });
  ui.compare.addEventListener('change', loadCompare);
  ui.pin.addEventListener('click', async () => {
    const cur = rec.trace?.session;
    if (!cur) return;
    try {
      status((await api('/api/trace', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: state.data.root, action: rec.pinned.has(cur) ? 'unpin' : 'pin', session: cur }) })).message);
      const s = await api(traceUrl('action=status'));
      fillSessions(s.sessions, s.apps, s.pinned);
    } catch (e) { status(e.message, true); }
  });
  ui.sessions.addEventListener('change', () => { if (ui.sessions.value) { rec.toggled.clear(); rec.full.clear(); loadTrace(ui.sessions.value); } });
  function showRuns(t) {
    rec.trace = t;
    ui.reccopy.disabled = !t.runs.length;
    ui.recinfo.textContent = `${t.live ? 'recording · ' : sessionText(t.session) + ' · '}${t.app ? t.app + ' · ' : ''}${t.runs.length} run${t.runs.length === 1 ? '' : 's'} · `
      + `${t.entries.length} entry point${t.entries.length === 1 ? '' : 's'}`;
    if (!ui.recPane.hidden) renderRuns(t);
    if (!t.live) refreshCompare(); // a finished recording can be compared
  }
  const liveBrowser = (t) => t.live && /(^| · )browser /.test(t.app);
  async function pollTrace() {
    try {
      const t = await api(traceUrl('action=show'));
      if (rec.timer) ui.trace.textContent = `Stop · ${t.runs.length} run${t.runs.length === 1 ? '' : 's'}`;
      const sig = t.session + t.runs.map((r) => `${r.id}:${r.ms}:${r.calls.length}`).join();
      if (sig !== rec.sig) { rec.sig = sig; showRuns(t); }
    } catch { /* nothing recorded yet */ }
  }

  function renderRuns(t) {
    const keep = ui.runs.scrollTop;
    ui.runs.querySelectorAll('.rgroup').forEach((g) => g.remove());
    ui.runsEmpty.hidden = !!(t && t.runs.length);
    if (!t || !t.runs.length) {
      ui.runsEmpty.firstElementChild.textContent = !t ? rec.idleText : liveBrowser(t)
        ? 'Recording in the Chrome window faravex opened. Use the page there (keep it visible): each task shows up here within a second.'
        : t.live ? 'Recording. Use the app: each request shows up here as it finishes.' : 'No runs in this recording.';
      return;
    }
    const byLabel = new Map();
    for (const r of t.runs) (byLabel.get(r.label) || byLabel.set(r.label, []).get(r.label)).push(r);
    const frag = document.createDocumentFragment();
    for (const g of t.entries) {
      const runs = byLabel.get(g.label) || [];
      const own = new Map();
      let total = 0;
      for (const r of runs) for (const c of r.calls) if (c[5] != null) { own.set(c[0], (own.get(c[0]) || 0) + c[5]); total += c[5]; }
      const hot = [...own].sort((a, b) => b[1] - a[1]).slice(0, 3)
        .map(([fn, ms]) => `<span>${esc(t.functions[fn].name)}</span> ${Math.round(100 * ms / (total || 1))}%`).join(' · ');
      const sec = document.createElement('section');
      sec.className = 'rgroup';
      sec.innerHTML = `<p class="rgroup-head"><b>${esc(g.label)}</b><small>${g.runs} run${g.runs > 1 ? 's' : ''} · median ${msText(g.median)} · p95 ${msText(g.p95)} · max ${msText(g.max)}`
        + `${g.errors ? ` · <span class="err">${g.errors} failed</span>` : ''}</small></p>` + (hot ? `<p class="hot">time goes to ${hot} (self time)</p>` : '');
      const latest = runs.slice(-3).map((r) => r.id); // newest runs open by default
      for (const r of [...runs].reverse()) sec.append(card(t, r, rec.toggled.has(r.id) ? rec.toggled.get(r.id) : latest.includes(r.id)));
      frag.append(sec);
    }
    ui.runs.append(frag);
    ui.runs.scrollTop = keep;
  }
  function card(t, r, open) {
    const el = document.createElement('article');
    el.className = 'card' + (r.error ? ' bad' : '');
    el.dataset.run = r.id;
    const io = r.error ? `<span class="err">✗ ${esc(r.error)}</span>` : esc(`${r.inputs || '()'} → ${r.output ?? (r.ms == null ? '…' : 'None')}`);
    el.innerHTML = `<div class="card-head" data-toggle title="${open ? 'Hide' : 'Show'} the call timeline"><span class="ms">${msText(r.ms)}</span>`
      + `<span class="when">#${esc(r.id)} · ${clock(r.start)}</span><span class="io">${io}</span>`
      + `<button class="btn btn-sm" type="button" data-onmap title="Light this run's files on the map">Map</button>`
      + `<button class="btn btn-sm" type="button" data-runcopy title="Copy the source of the functions this run executed">Copy slice</button></div>`;
    if (open) el.append(waterfall(t, r));
    return el;
  }
  // Call timeline: rows in call order, indented under the caller; bar = when and how long, bright part = self time.
  function waterfall(t, r) {
    const box = document.createElement('div');
    box.className = 'wf';
    const total = r.ms ?? Math.max(1, ...r.calls.map((c) => c[2] + (c[3] ?? 0)));
    const kids = new Map();
    r.calls.forEach((c, i) => (kids.get(c[1]) || kids.set(c[1], []).get(c[1])).push(i)); // calls are stored in start order
    const order = [], stack = (kids.get(-1) || []).slice().reverse().map((i) => [i, 0]);
    while (stack.length) {
      const [i, d] = stack.pop();
      order.push([i, d]);
      for (const k of (kids.get(i) || []).slice().reverse()) stack.push([k, d + 1]);
    }
    const hottest = Math.max(0, ...r.calls.map((c) => c[5] ?? 0));
    // By default only calls that matter for latency: at least 0.5% of the run, raised, or still running, plus their callers.
    let shown = order;
    if (!rec.full.has(r.id)) {
      const keep = new Set(), floor = total * 0.005;
      r.calls.forEach((c, i) => {
        if (c[3] == null || c[3] >= floor || c[4] & 1) for (let k = i; k >= 0 && !keep.has(k); k = r.calls[k][1]) keep.add(k);
      });
      shown = order.filter(([i]) => keep.has(i)).slice(0, 60);
    }
    const rows = shown.map(([i, d]) => {
      const [fn, , t0, ms, flags, own] = r.calls[i], f = t.functions[fn];
      const width = 100 * (ms ?? total - t0) / total, solid = ms ? 100 * (own ?? 0) / ms : 0;
      const tip = ms == null ? 'still running' : `${msText(ms)} from call to return · ${msText(own)} in this function itself`;
      return `<div class="wf-row${flags & 1 ? ' raised' : ''}${hottest && own === hottest ? ' hotrow' : ''}" title="${esc(tip)}">`
        + `<span class="wf-name" style="padding-left:${d * 12}px"><button type="button" data-file="${esc(f.file)}" data-line="${f.line}" title="${esc(f.name)} · ${esc(f.file)}:${f.line} (click: open on the map)">${esc(f.name)}</button> <i>${esc(base(f.file))}</i></span>`
        + `<span class="wf-track"><span class="wf-bar" style="left:${100 * t0 / total}%;width:${width}%"><span class="wf-self" style="width:${solid}%"></span></span></span>`
        + `<span class="wf-num">${msText(ms)}</span></div>`;
    });
    box.innerHTML = `<p class="card-io">in: ${esc(r.inputs || '-')}<br>${r.error ? 'error' : 'out'}: ${esc(r.error || r.output || '-')}`
      + `${r.caller ? `<br>called from ${esc(r.caller)}, which was already running when recording began` : ''}</p>`
      + `<p class="wf-key"><b class="k-self"></b> time inside the function (incl. library and network waits) <b class="k-wait"></b> waiting on the calls under it · <span class="k-hot">yellow</span> = the call with the most time inside itself · `
      + `rows overlap (nested or parallel), so they don't add up to ${msText(total)}</p>`
      + `<div class="wf-row wf-cols"><span>call</span><span>timeline · ${msText(total)}</span><span class="wf-num">took</span></div>`
      + rows.join('') + (order.length > shown.length
        ? `<button class="wf-more" type="button" data-full>${order.length - shown.length} shorter calls hidden · show all ${order.length}</button>` : '')
      + (r.dropped ? `<p class="card-io">${r.dropped} calls past the 5,000-call limit were counted, not timed.</p>` : '');
    return box;
  }
  ui.runs.addEventListener('click', async (ev) => {
    const el = ev.target.closest('.card'), r = el && rec.trace?.runs.find((x) => x.id === el.dataset.run);
    if (!r) return;
    const b = ev.target.closest('button');
    if (b?.dataset.file) return openInMap(b.dataset.file, +b.dataset.line);
    if (b?.matches('[data-onmap]')) return runOnMap(rec.trace, r);
    if (b?.matches('[data-runcopy]')) return copySlice(rec.trace.session, r.id, b);
    if (b?.matches('[data-full]')) { rec.full.add(r.id); rec.toggled.set(r.id, true); } else if (ev.target.closest('[data-toggle]')) rec.toggled.set(r.id, !el.querySelector('.wf'));
    else return;
    el.replaceWith(card(rec.trace, r, rec.toggled.get(r.id)));
  });
  function openInMap(file, line) {
    setView('map');
    if (!state.byId.has(file)) return status(`${file} is not on the map (another folder, or not a project file)`, true);
    state.jump = { path: file, start: line, end: line };
    if (!state.nodeEls.has(file)) { showUnlinked(); draw(false); }
    select(file);
  }
  function runOnMap(t, r) { // the map's trace overlay, for this run only
    const n = new Map(), raised = new Map(), first = new Map();
    for (const c of r.calls) {
      n.set(c[0], (n.get(c[0]) || 0) + 1);
      if (c[4] & 1) raised.set(c[0], (raised.get(c[0]) || 0) + 1);
      if (!first.has(c[0])) first.set(c[0], c[2]);
    }
    const fns = [...n.keys()].sort((a, b) => first.get(a) - first.get(b)).map((fn) => ({ ...t.functions[fn], calls: n.get(fn), raised: raised.get(fn) || 0 }));
    setView('map');
    showTrace({ session: t.session, run: r.id, functions: fns }, `run #${r.id} · ${r.label} · ${msText(r.ms)}`);
  }
  function showTrace(t, msg) {
    const files = [...new Set(t.functions.map((f) => f.file))];
    const rank = new Map(), raised = new Set(), items = [];
    for (const file of files) {
      if (state.byId.has(file)) rank.set(file, rank.size + 1);
      const fns = t.functions.filter((f) => f.file === file);
      if (fns.some((f) => f.raised)) raised.add(file);
      items.push({ path: file, start: fns[0].line, end: fns[0].line, cls: 'chg',
        html: `<b>${rank.get(file) || '·'}</b>${esc(fit(file, 44))}<small>${fns.length} fn · ${fns.reduce((s, f) => s + f.calls, 0)} calls</small>` });
      for (const f of fns) items.push({ path: file, start: f.line, end: f.line, cls: 'sub',
        html: `<span class="tag">${f.calls}×</span>${esc(fit(f.name, 40))}:${f.line}${f.raised ? `<span class="tag err">raised ${f.raised}</span>` : ''}` });
    }
    showOverlay({ kind: 'trace', session: t.session, run: t.run, items, rank, raised, changed: new Set() },
      `${msg}: ${t.functions.length} functions in ${files.length} files` + (raised.size ? ` · exceptions in ${raised.size} files` : ''));
  }
  async function copySlice(session, run, btn) {
    try {
      const r = await api(traceUrl(`action=slice&session=${encodeURIComponent(session)}${run ? '&run=' + encodeURIComponent(run) : ''}`));
      await copyText(r.text, btn);
      copied(run ? `execution slice of run #${run}` : `execution slice of recording ${sessionText(session)}`, r.chars);
    } catch (e) { status(e.message, true); }
  }
  ui.reccopy.addEventListener('click', () => rec.trace && copySlice(rec.trace.session, null, ui.reccopy));
  ui.rcopy.addEventListener('click', () => copySlice(state.search.session, state.search.run, ui.rcopy));
  ui.trace.addEventListener('click', async () => {
    if (!state.data) return;
    try {
      if (rec.timer) {
        let r;
        try { r = await tracePost('stop'); } finally { setRecording(false); } // a failed stop has ended the recording too
        status(r.message);
        { const s = await api(traceUrl('action=status')); fillSessions(s.sessions, s.apps, s.pinned); }
        ui.sessions.value = r.trace.session;
        return showRuns(r.trace);
      }
      const to = target();
      if (!to.url && !to.port) { ui.port.focus(); return status('Type your Python app\'s port, or a page URL to record in the browser, then Record.', true); }
      try { localStorage.setItem(portKey(), ui.port.value.trim()); } catch { /* storage off */ }
      const s = await api(traceUrl('action=status'));
      if (to.port && s.venv && !s.enabled) { // no venv: the server records the port's page in the browser instead
        if (!confirm('Enable tracing for this folder?\n\nAdds a small faravex_trace.pth file to the app\'s virtual environment '
          + '(and to .gitignore) so the faravex probe loads when the app starts. It records nothing until you press Record.')) return;
        return status((await tracePost('enable')).message);
      }
      status((await tracePost('start')).message);
      rec.sig = '';
      rec.toggled.clear(); rec.full.clear();
      setRecording(true);
      pollTrace();
    } catch (e) { status(e.message, true); }
  });

  // ---------- Toolbar ----------
  $$('[data-show]').forEach((cb) => cb.addEventListener('change', () => { state.show[cb.dataset.show] = cb.checked; draw(true); }));
  $('[data-fit]').addEventListener('click', () => fitView());
  ui.relayout.addEventListener('click', () => { state.pins.clear(); savePins(); draw(true); });
  ui.form.addEventListener('submit', (ev) => { ev.preventDefault(); mapFolder(ui.path.value.trim()); });
  ui.indexBtn.addEventListener('click', async () => {
    if (!state.data) return;
    ui.indexBtn.disabled = true;
    status(state.data.index.built ? 'Updating index (changed files only)…' : 'Indexing with EmbeddingGemma2. The first run loads the model; large repos take minutes…');
    try {
      const r = await api('/api/index', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: state.data.root }) });
      status(`Indexed: ${r.files_embedded} files re-embedded in ${r.seconds} s`);
      await mapFolder(state.data.root);
    } catch (e) {
      status(e.message, true);
      ui.indexBtn.disabled = false;
    }
  });
  addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !ui.picker.open) { if (state.sel) clearSel(); else if (state.search) clearSearch(); }
    if (ev.key === '/' && document.activeElement.tagName !== 'INPUT') { ev.preventDefault(); ui.filter.focus(); }
  });

  // ---------- Folder picker (server-side listing: browsers cannot reveal absolute paths) ----------
  let here = null;
  async function browse(path) {
    try {
      here = await api('/api/browse?path=' + encodeURIComponent(path || ''));
      ui.crumb.textContent = here.path || 'Drives and home';
      ui.dirs.innerHTML = here.dirs.map((d) => `<li><button type="button" data-dir="${esc(d)}">${esc(d)}</button></li>`).join('')
        || '<li class="muted" style="padding:6px 14px">No sub-folders</li>';
      $('[data-up]').disabled = here.parent === null;
      $('[data-choose]').disabled = !here.path;
    } catch (e) { ui.crumb.textContent = e.message; }
  }
  const join = (a, b) => (/[\\/]$/.test(a) ? a + b : a + (a.includes('\\') ? '\\' : '/') + b);
  $('[data-browse]').addEventListener('click', () => {
    const v = ui.path.value.trim();
    ui.picker.showModal();
    browse(/^(https?|file):/i.test(v) ? '' : v);
  });
  ui.dirs.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-dir]');
    if (b) browse(here.path ? join(here.path, b.dataset.dir) : b.dataset.dir);
  });
  $('[data-up]').addEventListener('click', () => browse(here.parent || ''));
  $('[data-choose]').addEventListener('click', () => { ui.picker.close(); ui.path.value = here.path; mapFolder(here.path); });

  showRecent();
  if (recent()[0]) ui.path.value = recent()[0];
  // For guide.js: the few entry points the tour drives.
  window.faravex = { state, esc, mapFolder, select, runSearch, runChanges, clearSearch, status, center, setView };
  fitView();
  try { const v = localStorage.getItem('faravex.view'); if (v === 'record' || v === 'calls') setView(v); } catch { /* storage off */ }
})();
