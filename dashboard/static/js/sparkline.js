/* Neural State Sparklines — inline SVG mini charts */

function renderSparkline(container, values, options = {}) {
  if (!container) return;
  const width = options.width || 120;
  const height = options.height || 32;
  // Colours come from CSS (currentColor + options.className, e.g. "dim-trust").
  const thresholds = options.thresholds || [];
  const pad = 2;
  const w = width - pad * 2;
  const h = height - pad * 2;

  const pts = (values && values.length) ? values : [0];
  const n = pts.length;
  const stepX = n > 1 ? w / (n - 1) : 0;

  const toXY = (v, i) => {
    const clamped = Math.max(0, Math.min(1, v));
    const x = pad + i * stepX;
    const y = pad + (1 - clamped) * h;
    return [x.toFixed(2), y.toFixed(2)];
  };

  const polyPoints = pts.map((v, i) => toXY(v, i).join(',')).join(' ');

  let thresholdLines = '';
  thresholds.forEach((t) => {
    const y = (pad + (1 - t.value) * h).toFixed(2);
    thresholdLines += `<line x1="${pad}" y1="${y}" x2="${width - pad}" y2="${y}" ` +
      `class="${t.className}" stroke="currentColor" stroke-width="1" stroke-dasharray="3,2" opacity="0.7"/>`;
  });

  const [lx, ly] = toXY(pts[n - 1], n - 1);

  container.innerHTML = `
    <svg width="100%" height="24" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" class="sparkline-svg ${options.className || ''}">
      ${thresholdLines}
      <polyline points="${polyPoints}" fill="none" stroke="currentColor"
        stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="${lx}" cy="${ly}" r="2.2" fill="currentColor"/>
    </svg>`;
}

const STABILITY_THRESHOLDS = [
  { value: 0.5, className: 'spark-warn' },
  { value: 0.3, className: 'spark-crit' }
];

window.renderSparkline = renderSparkline;
window.STABILITY_THRESHOLDS = STABILITY_THRESHOLDS;
