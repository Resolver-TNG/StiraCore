/* Dependency-free SVG charts: works offline and with script-src 'self'. */
(function (root) {
  'use strict';
  // Series colours live in observability.css (.dim-<dimension> → --dim-* tokens).
  const series = dim => `dim-${dim}`;
  function correlation(rows, dims) {
    return dims.map(a => dims.map(b => {
      if (rows.length < 2) return null;
      const mean = d => rows.reduce((sum, r) => sum + r.state[d], 0) / rows.length;
      const ma = mean(a), mb = mean(b);
      let cov = 0, va = 0, vb = 0;
      rows.forEach(r => {
        const da = r.state[a] - ma, db = r.state[b] - mb;
        cov += da * db; va += da * da; vb += db * db;
      });
      return va > 1e-20 && vb > 1e-20 ? Math.max(-1, Math.min(1, cov / Math.sqrt(va * vb))) : null;
    }));
  }
  function timestamp(value) {
    return typeof value === 'number' ? value * 1000 : Date.parse(value);
  }
  // #238: read-only heartbeat summary (no control: the loop is opt-in via config only).
  function heartbeatText(hb, t, clock) {
    if (!hb || typeof hb !== 'object') return null;
    if (!hb.enabled) return t('neural.heartbeat_off');
    if (!hb.running) return t('neural.heartbeat_stopped', {error: hb.last_error || '—'});
    const at = v => (typeof v === 'number' && Number.isFinite(v) ? clock(v) : '—');
    const level = typeof hb.last_level === 'string' ? hb.last_level : null;
    return t(hb.degraded ? 'neural.heartbeat_degraded' : 'neural.heartbeat_on', {
      last: at(hb.last_tick_at), next: at(hb.next_tick_at),
      breaker: level ? t(`shell.${level}`) : '—', failures: hb.consecutive_failures || 0,
    });
  }
  if (typeof module !== 'undefined') module.exports = {correlation, timestamp, heartbeatText};
  if (!root.document) return;
  const doc = root.document;
  const container = doc.getElementById('neural-chart');
  if (!container) return;
  const t = root.t || (key => key);
  const label = (prefix, raw) => (root.i18n ? root.i18n.label(prefix + raw, raw) : raw);
  const dimLabel = dim => label('dim.', dim);
  const stimulusLabel = name => label('stimulus.', name);
  function element(name, text) {
    const el = doc.createElement(name);
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function svgNode(name, attrs, text) {
    const el = doc.createElementNS('http://www.w3.org/2000/svg', name);
    Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function heatmap(id, rows, columns, values, scale, rowLabel) {
    const table = element('table'), head = element('thead'), header = element('tr');
    header.append(element('td', ''));
    columns.forEach(c => { const th = element('th', dimLabel(c)); th.scope = 'col'; header.append(th); });
    head.append(header); table.append(head);
    const body = element('tbody');
    rows.forEach((label, i) => {
      const tr = element('tr'), th = element('th', rowLabel(label)); th.scope = 'row'; tr.append(th);
      values[i].forEach(v => {
        const td = element('td', v === null ? '—' : v.toFixed(3));
        if (v !== null && v !== 0) td.className = `heat-${Math.abs(v) >= scale ? 'strong-' : ''}${v > 0 ? 'positive' : 'negative'}`;
        tr.append(td);
      });
      body.append(tr);
    });
    table.append(body); doc.getElementById(id).replaceChildren(table);
  }
  function render(data) {
    const dims = data.dimensions;
    const history = data.history.filter(r => Number.isFinite(timestamp(r.timestamp)));
    const observed = history.concat([data.current]).sort((a, b) => timestamp(a.timestamp) - timestamp(b.timestamp));
    const predicted = data.prediction;
    const first = timestamp(observed[0].timestamp), now = timestamp(data.current.timestamp);
    const last = timestamp(predicted[predicted.length - 1].timestamp);
    const x = time => 55 + (timestamp(time) - first) / Math.max(1, last - first) * 870;
    const y = v => 285 - v * 250;
    const svg = svgNode('svg', {viewBox: '0 0 1020 335', role: 'img', 'aria-label': t('neural.svg_label')});
    svg.append(svgNode('title', {}, t('neural.svg_title')));
    for (let i = 0; i <= 5; i++) {
      const value = i / 5;
      svg.append(svgNode('line', {x1: 55, x2: 925, y1: y(value), y2: y(value), class: 'viz-grid'}));
      svg.append(svgNode('text', {x: 15, y: y(value) + 4, 'font-size': 12}, value.toFixed(1)));
    }
    [['WARNING', data.thresholds.warning, 'viz-warn'], ['CRITICAL', data.thresholds.critical, 'viz-crit']].forEach(([label, value, kind]) => {
      svg.append(svgNode('line', {x1: 55, x2: 925, y1: y(value), y2: y(value), class: `viz-threshold ${kind}`, 'stroke-dasharray': '8 4'}));
      svg.append(svgNode('text', {x: 930, y: y(value), class: `viz-threshold-label ${kind}`, 'font-size': 11}, label));
    });
    const path = (rows, dim) => rows.map((r, i) => `${i ? 'L' : 'M'}${x(r.timestamp)},${y(r.state[dim])}`).join(' ');
    dims.forEach((dim, i) => {
      svg.append(svgNode('path', {d: path(observed, dim), fill: 'none', class: `viz-line ${series(dim)}`, 'stroke-width': 2}));
      svg.append(svgNode('path', {d: path(predicted, dim), fill: 'none', class: `viz-line ${series(dim)}`, 'stroke-width': 2, 'stroke-dasharray': '3 5'}));
      history.filter(r => r.stimulus).forEach(r => {
        const dot = svgNode('circle', {cx: x(r.timestamp), cy: y(r.state[dim]), r: 4, class: `viz-dot ${series(dim)}`});
        dot.append(svgNode('title', {}, `${new Date(timestamp(r.timestamp)).toLocaleString()} · ${r.stimulus} · ${dimLabel(dim)}: ${r.state[dim]}`));
        svg.append(dot);
      });
    });
    svg.append(svgNode('line', {x1: x(now / 1000), x2: x(now / 1000), y1: 25, y2: 285, class: 'viz-now', 'stroke-dasharray': '2 2'}));
    [[first, 'start'], [last, 'end']].forEach(([t, anchor]) => svg.append(svgNode('text', {x: x(t / 1000), y: 320, 'text-anchor': anchor, 'font-size': 12}, new Date(t).toLocaleString())));
    container.replaceChildren(svg);
    const legend = doc.getElementById('neural-legend'); legend.replaceChildren();
    dims.forEach((dim, i) => {
      const item = element('span', `${dimLabel(dim)}: ${data.current.state[dim].toFixed(3)}`);
      const icon = svgNode('svg', {width: 18, height: 12, 'aria-hidden': 'true'});
      icon.append(svgNode('line', {x1: 0, x2: 15, y1: 6, y2: 6, class: `viz-line ${series(dim)}`, 'stroke-width': 3}));
      item.prepend(icon); legend.append(item);
    });
    heatmap('neural-correlation', dims, dims, correlation(history, dims), 0.5, dimLabel);
    heatmap('neural-emotion', data.emotion_matrix.stimuli, dims, data.emotion_matrix.values, 0.04, stimulusLabel);
    doc.getElementById('neural-status').textContent = t('neural.samples', {count: history.length});
    const hbEl = doc.getElementById('neural-heartbeat');
    const hbText = hbEl && heartbeatText(data.heartbeat, t, v => new Date(v * 1000).toLocaleTimeString());
    if (hbEl) { hbEl.hidden = !hbText; hbEl.textContent = hbText || ''; }
  }
  // Resolves to whether the history differs from the last read, null when it could not be read.
  let lastKey = null;
  async function refresh() {
    try {
      const response = await fetch('/api/state/history?limit=500', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      render(data);
      // The forecast and the "now" stamp are computed per request: not a change.
      const key = JSON.stringify([data.history, data.current && data.current.state, data.heartbeat, data.emotion_matrix]);
      const changed = key !== lastKey;
      lastKey = key;
      return changed;
    } catch (error) {
      doc.getElementById('neural-status').textContent = t('neural.unavailable', {error: error.message});
      return null;
    }
  }
  // #369: not while the tab is hidden, slower while nothing changes, backed off on failure.
  if (root.StiraPoll) root.StiraPoll.keep(refresh, 5000, 20000, undefined, doc);
  else {
    const again = () => refresh().finally(() => root.setTimeout(again, 5000));
    again();
  }
})(typeof window !== 'undefined' ? window : globalThis);
