// Artifacts page (#454, ADR 0020): what file_write / editor wrote, by version.
// Every string from the server is inserted with textContent (never as HTML). An HTML
// version is shown in <iframe sandbox="allow-scripts"> from the registry's snapshot, which
// the server serves with its own sandbox CSP (an opaque origin: no access to this page,
// its storage or the CSRF token, no network).
(function () {
  const ID = /^a[0-9a-f]{20}$/;
  const TEXT_KINDS = new Set(['html', 'markdown', 'code', 'text']);
  const state = { selected: null, detail: null };
  // #601 E2-E4: one counter per shared box (detail, preview, diff); an answer is drawn only
  // if no later click started a read for the same box meanwhile (the click order wins).
  const gen = { select: 0, preview: 0, diff: 0 };
  const tr = (key, vars) => (typeof window.t === 'function' ? window.t(key, vars) : key);
  const $ = (id) => document.getElementById(id);

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function when(seconds) {
    if (typeof seconds !== 'number') return '';
    try { return new Date(seconds * 1000).toLocaleString(); } catch (_e) { return ''; }
  }

  function size(bytes) {
    if (typeof bytes !== 'number') return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  const labelText = (code) => tr(`artifacts.label_${code}`);
  const kindText = (code) => tr(`artifacts.kind_${code}`);
  const opText = (code) => tr(code === 'create' ? 'artifacts.op_create' : 'artifacts.op_update');

  async function getJson(url) {
    const res = await fetch(url, { headers: { Accept: 'application/json' } });
    if (!res.ok) return null;
    return res.json();
  }

  function listUrl() {
    const params = new URLSearchParams({ limit: '200' });
    const kind = $('filter-kind').value;
    const turn = $('filter-turn').value.trim();
    const q = $('filter-q').value.trim();
    if (kind) params.set('kind', kind);
    if (turn) params.set('turn', turn);
    if (q) params.set('q', q);
    return `/api/artifacts?${params}`;
  }

  function renderWarnings(body) {
    const box = $('list-warnings');
    const w = (body && body.warnings) || {};
    const parts = [];
    if (w.limit_artifacts) parts.push(tr('artifacts.warn_limit', { count: w.limit_artifacts, limit: body.limits.artifacts }));
    if (w.hash_only) parts.push(tr('artifacts.warn_hash_only', { count: w.hash_only }));
    if (w.too_large) parts.push(tr('artifacts.warn_too_large', { count: w.too_large }));
    box.textContent = parts.join(' ');
    box.hidden = parts.length === 0;
  }

  function renderList(body) {
    const list = $('artifact-list');
    const items = (body && body.artifacts) || [];
    list.replaceChildren();
    $('list-empty').hidden = items.length > 0;
    renderWarnings(body);
    for (const a of items) {
      if (!ID.test(a.id)) continue;
      const li = el('li');
      const button = el('button', 'artifact-item');
      button.type = 'button';
      button.dataset.id = a.id;
      if (a.id === state.selected) button.setAttribute('aria-current', 'true');
      button.append(el('span', 'artifact-path', a.path));
      const meta = el('span', 'artifact-item-meta');
      meta.append(el('span', 'badge', kindText(a.kind)), el('span', 'badge', `v${a.version}`));
      for (const label of a.labels || []) meta.append(el('span', `badge badge-label label-${label}`, labelText(label)));
      meta.append(el('span', 'muted', when(a.updated_at)));
      button.append(meta);
      button.addEventListener('click', () => select(a.id));
      li.append(button);
      list.append(li);
    }
  }

  async function refresh() {
    renderList(await getJson(listUrl()));
  }

  function renderChecks(checks) {
    const box = $('checks');
    box.replaceChildren();
    const last = (checks || [])[0];
    if (!last) { box.hidden = true; return; }
    const s = last.summary || {};
    const parts = [tr('artifacts.check_head', { version: last.version, status: s.status || '?' })];
    if (typeof s.page_errors === 'number') parts.push(tr('artifacts.check_page_errors', { count: s.page_errors }));
    if (typeof s.console_errors === 'number') parts.push(tr('artifacts.check_console_errors', { count: s.console_errors }));
    if (s.network && typeof s.network.blocked === 'number') parts.push(tr('artifacts.check_blocked', { count: s.network.blocked }));
    if (s.assertions && typeof s.assertions.passed === 'number') {
      parts.push(tr('artifacts.check_assertions', { passed: s.assertions.passed, failed: s.assertions.failed || 0 }));
    }
    box.append(el('strong', '', tr('artifacts.check_title')), el('span', '', ' ' + parts.join(' · ')));
    box.hidden = false;
  }

  function renderVersions(detail) {
    const body = $('version-rows');
    body.replaceChildren();
    const versions = detail.versions || [];
    versions.forEach((v, index) => {
      const row = el('tr');
      row.append(
        el('td', '', `v${v.version}`),
        el('td', 'mono', v.turn_id || '—'),
        el('td', '', opText(v.op)),
        el('td', '', size(v.size) + (v.stored ? '' : ` (${tr('artifacts.hash_only')})`)),
        el('td', '', when(v.at)),
      );
      const actions = el('td', 'artifact-actions');
      const show = el('button', 'btn btn-secondary', tr('artifacts.show'));
      show.type = 'button';
      show.setAttribute('aria-label', tr('artifacts.show_version', { version: v.version }));
      show.addEventListener('click', () => preview(v.version));
      actions.append(show);
      const older = versions[index + 1];
      if (older && TEXT_KINDS.has(detail.kind)) {
        const diff = el('button', 'btn btn-secondary', tr('artifacts.diff_prev'));
        diff.type = 'button';
        diff.setAttribute('aria-label', tr('artifacts.diff_version', { from: older.version, to: v.version }));
        diff.addEventListener('click', () => showDiff(older.version, v.version));
        actions.append(diff);
      }
      row.append(actions);
      body.append(row);
    });
  }

  function renderLabels(detail) {
    for (const box of document.querySelectorAll('input[name="label"]')) {
      box.checked = (detail.labels || []).includes(box.value);
    }
    const last = (detail.label_history || [])[0];
    $('label-status').textContent = last ? tr('artifacts.label_last', { time: when(last.at) }) : '';
  }

  async function select(id) {
    if (!ID.test(id)) return;
    const mine = ++gen.select;
    const detail = await getJson(`/api/artifacts/${id}`);
    if (mine !== gen.select || !detail) return;
    gen.diff += 1; // a diff of the previous file still on the way is not drawn
    state.selected = id;
    state.detail = detail;
    $('detail-title').textContent = detail.path;
    $('detail-meta').textContent = tr('artifacts.meta', {
      kind: kindText(detail.kind), version: detail.version, count: (detail.versions || []).length,
      time: when(detail.updated_at),
    });
    $('detail').hidden = false;
    renderLabels(detail);
    renderChecks(detail.checks);
    renderVersions(detail);
    $('diff').hidden = true;
    $('diff-title').hidden = true;
    for (const b of document.querySelectorAll('.artifact-item')) {
      if (b.dataset.id === id) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current');
    }
    await preview(detail.version);
  }

  async function preview(version) {
    const detail = state.detail;
    const box = $('preview');
    const mine = ++gen.preview;
    box.replaceChildren();
    if (!detail) return;
    const entry = (detail.versions || []).find((v) => v.version === version);
    $('preview-title').textContent = tr('artifacts.preview_version', { version });
    if (!entry) return;
    if (!entry.stored) { box.append(el('p', 'muted', tr('artifacts.not_stored'))); return; }
    const raw = `/api/artifacts/${detail.id}/versions/${version}/raw`;
    if (detail.kind === 'html') {
      box.append(el('p', 'muted', tr('artifacts.sandbox_note')));
      const frame = document.createElement('iframe');
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.setAttribute('referrerpolicy', 'no-referrer');
      frame.setAttribute('loading', 'lazy');
      frame.title = tr('artifacts.frame_title', { path: detail.path, version });
      frame.className = 'artifact-frame';
      frame.src = raw;
      box.append(frame);
      return;
    }
    if (detail.kind === 'image') {
      const img = document.createElement('img');
      img.className = 'artifact-image';
      img.alt = tr('artifacts.image_alt', { path: detail.path, version });
      img.src = raw;
      box.append(img);
      return;
    }
    if (detail.kind === 'other') { box.append(el('p', 'muted', tr('artifacts.no_preview'))); return; }
    const body = await getJson(`/api/artifacts/${detail.id}/versions/${version}`);
    if (mine !== gen.preview) return;
    if (!body || typeof body.text !== 'string') { box.append(el('p', 'muted', tr('artifacts.no_preview'))); return; }
    const pre = el('pre', 'artifact-text', body.text);
    pre.tabIndex = 0;
    box.append(pre);
  }

  async function showDiff(from, to) {
    const detail = state.detail;
    const box = $('diff');
    const mine = ++gen.diff;
    box.replaceChildren();
    $('diff-title').hidden = false;
    $('diff-title').textContent = tr('artifacts.diff_title', { from, to });
    box.hidden = false;
    const body = detail && await getJson(`/api/artifacts/${detail.id}/diff?from=${from}&to=${to}`);
    if (mine !== gen.diff || !body) return;
    if (!body.available) { box.append(el('p', 'muted', tr(`artifacts.diff_${body.reason}`))); return; }
    box.append(el('p', 'muted', tr('artifacts.diff_counts', { added: body.added, removed: body.removed })));
    const pre = el('pre', 'artifact-diff-lines');
    pre.tabIndex = 0;
    for (const line of body.lines) {
      let cls = 'diff-ctx';
      if (line.startsWith('+') && !line.startsWith('+++')) cls = 'diff-add';
      else if (line.startsWith('-') && !line.startsWith('---')) cls = 'diff-del';
      else if (line.startsWith('@@')) cls = 'diff-hunk';
      pre.append(el('span', cls, line + '\n'));
    }
    box.append(pre);
    if (body.truncated) box.append(el('p', 'muted', tr('artifacts.diff_truncated')));
  }

  async function saveLabels() {
    const detail = state.detail;
    if (!detail) return;
    const labels = [...document.querySelectorAll('input[name="label"]:checked')].map((b) => b.value);
    const status = $('label-status');
    const res = await window.stiraFetch(`/api/artifacts/${detail.id}/labels`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ labels }),
    });
    // #601 E3: another file chosen meanwhile keeps its own detail, labels and status line.
    const id = detail.id;
    const still = () => state.detail && state.detail.id === id;
    if (!res.ok) {
      let code = '';
      try { code = (await res.json()).error || ''; } catch (_e) { code = ''; }
      if (still()) status.textContent = tr(code === 'tool_call_running' ? 'artifacts.label_busy' : 'artifacts.label_failed');
      return;
    }
    if (still()) status.textContent = tr('artifacts.label_saved');
    await refresh();
    if (!still()) return;
    const fresh = await getJson(`/api/artifacts/${id}`);
    if (fresh && still()) { state.detail = fresh; renderLabels(fresh); status.textContent = tr('artifacts.label_saved'); }
  }

  let timer = null;
  const later = () => { clearTimeout(timer); timer = setTimeout(refresh, 250); };
  $('filter-kind').addEventListener('change', refresh);
  $('filter-turn').addEventListener('input', later);
  $('filter-q').addEventListener('input', later);
  $('save-labels').addEventListener('click', saveLabels);
  const params = new URLSearchParams(location.search);
  if (params.get('turn')) $('filter-turn').value = params.get('turn');
  refresh().then(() => {
    const wanted = params.get('id') || (location.hash || '').slice(1);
    if (ID.test(wanted)) select(wanted);
  });
})();
