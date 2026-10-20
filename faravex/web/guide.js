/* Interactive guide: a spotlight tour of every feature, with "Try it" actions that drive the real UI.
   Opens by itself on the first visit (per browser); the "? Guide" button reopens it. */
(() => {
  'use strict';
  const cs = window.faravex;
  if (!cs) return;
  const $ = (s) => document.querySelector(s);
  const esc = cs.esc;
  const SEEN = 'faravex.guided';
  const mapped = () => !!cs.state.data;
  const indexed = () => mapped() && !!cs.state.data.index.built;
  // the most-imported code file (import links first, all links to break ties): a good file to demo on
  const imports = (n) => n.in.filter((e) => e.type === 'import').length + n.out.filter((e) => e.type === 'import').length;
  const busiest = () => [...cs.state.byId.values()].filter((n) => cs.state.nodeEls?.has(n.id))
    .sort((a, b) => (b.kind === 'code') - (a.kind === 'code') || imports(b) - imports(a) || b.in.length + b.out.length - a.in.length - a.out.length)[0];
  let info = null;
  fetch('/api/info').then((r) => r.json()).then((j) => { info = j; }).catch(() => {});

  // needs: what must be true for the action (and the spotlight) to make sense; `hint` says how to get there.
  const STEPS = [
    {
      title: 'Faravex, in two minutes',
      body: `<p>A map of how a project's files depend on each other, plus search by meaning or by exact name.
        Everything runs on this machine: the EmbeddingGemma model, the index and this page.</p>
        <p>The same engine serves AI agents over MCP (last step). Use <kbd>→</kbd>/<kbd>←</kbd> to move, <kbd>Esc</kbd> to close.</p>`,
    },
    {
      target: '.pathform', title: '1 · Pick a project',
      body: '<p>Type a folder path or use <b>Browse</b>, then <b>Map</b>. Recent folders are suggested as you type.</p>',
      action: () => (cs.state.data ? null : $('[data-path]').value && {
        label: `Map ${esc($('[data-path]').value.split(/[\\/]/).pop())}`,
        run: () => cs.mapFolder($('[data-path]').value.trim()),
      }),
    },
    {
      target: '[data-canvas]', needs: mapped, hint: 'Map a folder first (step 1).', title: '2 · Read the map',
      body: `<p>Each box is a file: <span class="g-code">code</span>, <span class="g-doc">doc / spec</span>, <span class="g-cfg">config</span>.
        Lines are links read from the files: <b>solid</b> = import, <b>dashed</b> = a doc names the file,
        <b>dotted</b> = a doc and code file mean the same thing (from the index).</p>
        <p>Scroll to zoom, drag empty space to pan, <b>Fit</b> to see everything.</p>`,
    },
    {
      target: '.tools .tog', title: '3 · Choose which links to show',
      body: '<p>Turn link types on and off. <b>Unlinked files</b> adds files with no links at all. Counts show how many of each were found.</p>',
    },
    {
      target: '[data-side]', needs: () => !!cs.state.sel, hint: 'Click any file on the map, or use Try it.',
      title: '4 · Click a file',
      body: `<p>Its links light up and the panel shows what it imports, what <b>uses</b> it, docs that name it,
        how many files it <b>affects</b> (directly or not) and a preview of the file.</p>`,
      action: () => mapped() && { label: 'Select the busiest file', run: () => { const n = busiest(); if (n) { cs.select(n.id); } } },
    },
    {
      target: '[data-relayout]', needs: mapped, hint: 'Map a folder first.', title: '5 · Drag files to untangle',
      body: `<p>Dense area? <b>Drag a file</b> to move it; its links follow and it stays selected so you see what it connects to.
        Positions are kept for this project in this browser. <b>Reset layout</b> forgets them.</p>`,
    },
    {
      target: '[data-filter]', title: '6 · Find a file by name',
      body: '<p>Type part of a name to highlight matching files, <kbd>Enter</kbd> jumps to the first. <kbd>/</kbd> focuses this box from anywhere.</p>',
    },
    {
      target: '[data-index]', needs: mapped, hint: 'Map a folder first.', title: '7 · Index for search',
      body: `<p><b>Index</b> reads the code into the local model once (minutes for a big repo); <b>Update index</b> later only
        re-reads changed files. Search, Changes and the dotted links need it.</p>`,
    },
    {
      target: '[data-search]', needs: indexed, hint: 'Index the folder first (step 7).', title: '8 · Search by meaning or by name',
      body: `<p>Ask a question (<i>where are API keys checked</i>) or type an exact name (<code>parseConfig</code>,
        <code>HTTP_401</code>, a quoted error message). Both kinds work in the same box; press <kbd>Enter</kbd>.</p>`,
      action: () => indexed() && { label: 'Try an example search', run: () => {
        const q = 'where errors are handled and reported';
        $('[data-search]').value = q; cs.runSearch(q);
      } },
    },
    {
      target: '[data-results]', needs: () => cs.state.search?.kind === 'search', hint: 'Run a search first (step 8).',
      title: '9 · Read the results',
      body: `<p>Matching files glow on the map with their <b>rank</b>. Files that connect the matches are dashed, and the links
        between them stay lit: one cluster = one subsystem, scattered = several.</p>
        <p><span class="g-doc">text</span> marks an exact-text match. Click a result to open the file at those lines. <b>Clear</b> or <kbd>Esc</kbd> to leave.</p>`,
    },
    {
      target: '[data-changes]', needs: indexed, hint: 'Index the folder first (step 7); the folder must be in a git repository.',
      title: '10 · Check your changes',
      body: `<p><b>Changes</b> reads your uncommitted git edits (Δ) and shows, for each, the function it is in, the files that use the
        changed names, similar code elsewhere and the <b>tests likely affected</b>. Useful before a commit.</p>`,
      action: () => indexed() && { label: 'Show my changes', run: () => cs.runChanges() },
    },
    {
      target: '[data-viewbtn="record"]', needs: mapped, hint: 'Map a folder first (step 1).', title: '11 · Record a running app',
      body: `<p>The <b>Record</b> view (this icon) shows what your app <b>actually runs</b>. Python 3.12+ app: type the port it listens on
        and press <b>Record</b>. The first time, faravex offers to enable tracing (a small file in the app's venv); restart the app once.
        Web page: type its URL instead; it opens in Chrome or Edge and every task that runs your JS/TS becomes a card (source maps
        lead back to your .ts/.tsx files).</p>
        <p>Every request becomes a card, grouped by endpoint, with its inputs, result and a timeline of every call: the solid part of a
        bar is time inside that function (library and network waits included), the dim part is waiting on the calls under it, so the
        slowest step stands out. Hover a row for both numbers. The dropdown names the app, port and command each recording came from. <b>Map</b> lights the run's files; <b>Copy slice</b>
        copies just the code it ran.</p>`,
      action: () => mapped() && { label: 'Open the Record view', run: () => cs.setView('record') },
    },
    {
      target: '[data-copy]', needs: () => !!cs.state.sel, hint: 'Select a file first (step 4), or use Try it.', title: '12 · Copy context for any chat',
      body: `<p>Copies one paste-ready bundle: the selected file, the signatures of what it imports, the lines that use it
        elsewhere and doc lines that mention it, sized to stay small. For chats that cannot read your files.</p>`,
      action: () => mapped() && {
        label: cs.state.sel ? 'Copy it now' : 'Select the busiest file and copy',
        run: () => { if (!cs.state.sel) { const n = busiest(); if (n) cs.select(n.id); } if (cs.state.sel) $('[data-copy]').click(); },
      },
    },
    {
      title: '13 · Give the tools to your AI agent', wide: true,
      body: () => {
        const py = info?.python || 'C:\\path\\to\\.venv\\Scripts\\python.exe', srv = info?.server || 'C:\\path\\to\\server.py';
        const j = (o) => esc(JSON.stringify(o, null, 2));
        const snip = (label, text) => `<p class="g-snip-h">${label}<button class="btn btn-sm" type="button" data-copytext="${esc(text)}">Copy</button></p><pre class="g-snip">${esc(text)}</pre>`;
        return `<p>Any MCP client gets these tools (offline, same index as this page):</p>
          <ul class="g-tools">
            <li><b>search_code</b>: by meaning or exact name; ranked line ranges with their function/class</li>
            <li><b>get_symbol_outline</b>: a file's classes and functions with line ranges, no full read</li>
            <li><b>get_dependencies</b>: imports, importers, docs</li>
            <li><b>get_blast_radius</b>: what a change can break, incl. tests and the lines using a symbol</li>
            <li><b>get_context_slice</b>: the Copy context bundle</li>
            <li><b>search_diff</b>: the Changes view</li>
            <li><b>trace</b> + <b>get_execution_slice</b>: Record / Stop and Copy slice</li>
          </ul>`
          + snip('Claude Code (terminal)', `claude mcp add faravex --scope user -- "${py}" "${srv}"`)
          + snip('VS Code / GitHub Copilot: .vscode/mcp.json', JSON.stringify({ servers: { faravex: {
            type: 'stdio', command: py, args: [srv], env: { FARAVEX_ROOT: '${workspaceFolder}' } } } }, null, 2))
          + snip('Cline: cline_mcp_settings.json', JSON.stringify({ mcpServers: { faravex: { command: py, args: [srv], disabled: false } } }, null, 2))
          + '<p class="g-note">Clients that start the server outside the project: the tools take a <code>path</code> argument, and absolute file paths work on their own.</p>';
      },
    },
    {
      title: 'That is everything',
      body: '<p>Reopen this guide with <b>? Guide</b> at the top. Keys: <kbd>/</kbd> filter files · <kbd>Esc</kbd> clear selection, then search.</p>',
    },
  ];

  let at = -1, spot = null, card = null;

  function build() {
    spot = Object.assign(document.createElement('div'), { className: 'guide-spot' });
    card = Object.assign(document.createElement('div'), { className: 'guide-card' });
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-label', 'Guide');
    document.body.append(spot, card);
    card.addEventListener('click', async (ev) => {
      const b = ev.target.closest('button');
      if (!b) return;
      if (b.dataset.nav === 'next') return go(at + 1);
      if (b.dataset.nav === 'back') return go(at - 1);
      if (b.dataset.nav === 'close') return close();
      if (b.dataset.copytext !== undefined) {
        try { await navigator.clipboard.writeText(b.dataset.copytext); b.textContent = 'Copied'; } catch { b.textContent = 'Select and copy'; }
        return;
      }
      if (b.dataset.act !== undefined) {
        const step = STEPS[at], act = step.action && step.action();
        if (!act) return;
        b.disabled = true; b.textContent = 'Working…';
        try { await act.run(); } finally { setTimeout(() => go(at), 350); } // re-render: the step may be ready now
      }
    });
  }

  function place(step) {
    const ready = !step.needs || step.needs();
    const el = ready && step.target ? $(step.target) : null;
    const r = el && !el.hidden && el.getClientRects().length ? el.getBoundingClientRect() : null;
    if (r) {
      const p = 6;
      Object.assign(spot.style, { display: 'block', left: `${r.left - p}px`, top: `${r.top - p}px`, width: `${r.width + 2 * p}px`, height: `${r.height + 2 * p}px` });
    } else {
      Object.assign(spot.style, { display: 'block', left: '50%', top: '50%', width: '0px', height: '0px' });
    }
    // card: below the target if there is room, else above, else beside; always inside the viewport
    const W = innerWidth, H = innerHeight, cw = card.offsetWidth, ch = card.offsetHeight, m = 16;
    let x = (W - cw) / 2, y = (H - ch) / 2;
    if (r) {
      x = Math.min(Math.max(m, r.left), W - cw - m);
      if (r.bottom + 14 + ch < H - m) y = r.bottom + 14;
      else if (r.top - 14 - ch > m) y = r.top - 14 - ch;
      else { y = Math.min(Math.max(m, r.top), H - ch - m); x = r.right + 14 + cw < W - m ? r.right + 14 : Math.max(m, r.left - 14 - cw); }
    }
    Object.assign(card.style, { left: `${x}px`, top: `${y}px` });
  }

  function go(i) {
    if (i < 0 || i >= STEPS.length) return close();
    at = i;
    const step = STEPS[i], ready = !step.needs || step.needs();
    const act = step.action && step.action();
    const body = typeof step.body === 'function' ? step.body() : step.body;
    card.classList.toggle('wide', !!step.wide);
    card.innerHTML = `<p class="g-head"><span>${esc(step.title)}</span><span class="g-count">${i + 1} / ${STEPS.length}</span></p>
      <div class="g-body">${body}${ready ? '' : `<p class="g-hint">${esc(step.hint || '')}</p>`}</div>
      <p class="g-nav">
        <button class="btn btn-sm" type="button" data-nav="close">Close</button>
        <span class="grow"></span>
        ${act ? `<button class="btn btn-sm" type="button" data-act>${act.label}</button>` : ''}
        ${i ? '<button class="btn btn-sm" type="button" data-nav="back">Back</button>' : ''}
        <button class="btn btn-sm btn-primary" type="button" data-nav="next">${i === STEPS.length - 1 ? 'Done' : 'Next'}</button>
      </p>`;
    card.hidden = spot.hidden = false;
    place(step);
    card.querySelector('[data-nav="next"]').focus({ preventScroll: true });
  }

  function open(i = 0) { if (!card) build(); go(i); }
  function close() {
    if (card) card.hidden = spot.hidden = true;
    at = -1;
    try { localStorage.setItem(SEEN, '1'); } catch { /* storage off */ }
  }

  $('[data-guide]').addEventListener('click', () => open(0));
  addEventListener('keydown', (ev) => {
    if (at < 0 || ev.target.closest?.('input, textarea')) return;
    if (ev.key === 'ArrowRight') { ev.preventDefault(); go(at + 1); }
    else if (ev.key === 'ArrowLeft') { ev.preventDefault(); go(at - 1); }
    else if (ev.key === 'Escape') { ev.stopImmediatePropagation(); close(); }
  }, true);
  addEventListener('resize', () => { if (at >= 0) place(STEPS[at]); });

  let seen = false;
  try { seen = !!localStorage.getItem(SEEN); } catch { seen = true; } // no storage: do not nag on every load
  if (!seen) setTimeout(() => open(0), 400);
})();
