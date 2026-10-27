// Real-browser check of static SVG on the Stage board (#453). Offline: the fake model
// (tests/fake_bedrock_endpoint.py, "SAY <text>" replies) and the real chat server on a fresh
// data directory; headless Chrome driven over --remote-debugging-pipe (no DevTools port).
//
//   node scripts/check_board_svg_browser.mjs [OUT_DIR]
//   node scripts/check_board_svg_browser.mjs --which-chrome   (prints the browser it would use)
//
// Also run by pytest (tests/test_board_svg_browser.py) whenever a Chrome / Chromium is found;
// otherwise that test is skipped and says why.
//
// Env: STIRA_PORT (default 5775), CHROME (default: found for this platform, see findChrome),
// PYTHON (default python3), SHOTS=0 to skip screenshots. Runs twice: with gateway ui.board_svg on and off. Checks:
//   - on: the three sample pictures are <img> of data:image/svg+xml, decoded, and their pixels
//     are the expected colours; the hostile block is drawn without its dangerous parts or stays
//     code; no request to the probe URLs the hostile SVG points at, no other request but the
//     page's own APIs, no console error, no dialog, the page's canary variable untouched;
//   - DOMParser (image/svg+xml) of the raw hostile SVG in the page runs nothing, loads nothing;
//   - the page's CSP header is the one in observability/security.py;
//   - off: no <img> on the board, the SVG blocks are code as before;
//   - on and off: the log (backlog) shows the same reply with no picture, the SVG blocks as code;
//   - control: the same hostile SVG inserted into the DOM as is (never done by the page) does
//     make probe requests, so "0 requests" above is a measurement, not a blind spot.
//   - axe-core (if installed in node_modules): the board with pictures has no axe violation
//     that the same board with the setting off does not have.
// Exit code 0 only when every check passed. Prints one JSON report.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { findChrome } from './find_chrome.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PORT = Number(process.env.STIRA_PORT || 5775);
if (process.argv[2] === '--which-chrome') {
  console.log(findChrome() || '');
  process.exit(0);
}
const CHROME = findChrome();
if (!CHROME) {
  console.error('No Chrome / Chromium found: set CHROME to its executable.');
  process.exit(2);
}
const PY = process.env.PYTHON || 'python3';
const OUT = process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), 'board-svg-'));
const SHOTS = process.env.SHOTS !== '0';
const BASE = `http://127.0.0.1:${PORT}`;
const EXPECTED_CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync(OUT, { recursive: true });

const NS = 'xmlns="http://www.w3.org/2000/svg"';
const COFFEE = `<svg ${NS} viewBox="0 0 200 200" width="200" height="200"><title>珈琲カップ</title><defs><linearGradient id="coffee" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#8b5a2b"/><stop offset="1" stop-color="#4a2c0f"/></linearGradient></defs><ellipse cx="100" cy="172" rx="72" ry="10" fill="#d9d4c7"/><path d="M50 80 L150 80 L140 160 Q100 176 60 160 Z" fill="#ffffff" stroke="#333333" stroke-width="4"/><ellipse cx="100" cy="86" rx="48" ry="9" fill="url(#coffee)"/><path d="M150 96 a22 22 0 0 1 0 44" fill="none" stroke="#333333" stroke-width="7" stroke-linecap="round"/><path d="M80 60 q10 -15 0 -30 M100 60 q10-15 0-30 M120 60 q10 -15 0 -30" stroke="#999999" stroke-width="3" fill="none" stroke-linecap="round"/></svg>`;
const BARS = `<svg ${NS} viewBox="0 0 320 200" role="img" aria-label="月別の件数"><rect width="320" height="200" fill="#ffffff"/><line x1="40" y1="170" x2="300" y2="170" stroke="#444444"/><line x1="40" y1="20" x2="40" y2="170" stroke="#444444"/><g fill="#3b82f6"><rect x="60" y="90" width="40" height="80"/><rect x="120" y="50" width="40" height="120"/><rect x="180" y="110" width="40" height="60"/><rect x="240" y="30" width="40" height="140" fill="#f97316"/></g><g font-family="sans-serif" font-size="12" text-anchor="middle" fill="#222222"><text x="80" y="188">7月</text><text x="140" y="188">8月</text><text x="200" y="188">9月</text><text x="260" y="188">10月</text></g></svg>`;
const DIAGRAM = `<svg ${NS} viewBox="0 0 360 140"><title>構成図</title><g font-family="sans-serif" font-size="13" text-anchor="middle" dominant-baseline="middle"><rect x="10" y="45" width="90" height="50" rx="8" fill="#e0f2fe" stroke="#0369a1" stroke-width="2"/><text x="55" y="70">ブラウザ</text><rect x="135" y="45" width="90" height="50" rx="8" fill="#dcfce7" stroke="#15803d" stroke-width="2"/><text x="180" y="70">サーバー</text><rect x="260" y="45" width="90" height="50" rx="8" fill="#fef3c7" stroke="#b45309" stroke-width="2"/><text x="305" y="70">モデル</text></g><g stroke="#333333" stroke-width="2" fill="#333333"><line x1="100" y1="70" x2="128" y2="70"/><polygon points="135,70 126,65 126,75"/><line x1="225" y1="70" x2="253" y2="70" stroke-dasharray="4 3"/><polygon points="260,70 251,65 251,75"/></g></svg>`;
// every way out the hostile picture tries points at a same-origin probe (allowed by the CSP,
// so only the sanitiser stands between it and a request) or at the canary
const P = `${BASE}/__probe`;
const HOSTILE = `<svg ${NS} xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 100 100" onload="window.__pwned='onload'"><script>window.__pwned='script'</script><style>@import url(${P}/css); @font-face{font-family:p;src:url(${P}/font)} rect{fill:url(${P}/fill.svg#x)}</style><image href="${P}/image.png" width="10" height="10"/><image xlink:href="${P}/xlink.png" width="10" height="10"/><use href="${P}/use.svg#a"/><foreignObject width="50" height="50"><img xmlns="http://www.w3.org/1999/xhtml" src="${P}/fo.png"/></foreignObject><a href="javascript:window.__pwned='link'"><text x="5" y="20">click</text></a><rect width="100" height="100" fill="#22c55e" style="fill:url(${P}/style.svg#x)"><set attributeName="fill" to="url(${P}/set.svg#x)"/><animate attributeName="fill" to="url(${P}/anim.svg#x)" dur="1s"/></rect><filter id="f"><feImage href="${P}/feimage.png"/></filter><text x="10" y="60" font-family="url(${P}/font2)">&lt;script&gt;</text></svg>`;
const REFUSED = `<!DOCTYPE svg [<!ENTITY x SYSTEM "${P}/xxe">]><svg ${NS}><text>&x;</text></svg>`;
const fence = (s) => `\`\`\`svg\n${s}\n\`\`\``;
const REPLY = ['# 図の確認', '珈琲カップ、棒グラフ、構成図です。', fence(COFFEE), fence(BARS), fence(DIAGRAM), fence(REFUSED), fence(HOSTILE), `インライン: <svg ${NS} onload="window.__pwned='inline'"><image href="${P}/inline.png"/></svg>`].join('\n\n');

// --- processes -------------------------------------------------------------------------------
const children = [];
function start(cmd, args, opts) {
  const p = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'], ...opts });
  children.push(p);
  return p;
}
function stopAll() {
  for (const p of children) if (p.exitCode === null) p.kill('SIGTERM');
}
process.on('exit', stopAll);

async function waitFor(fn, ms = 20000, step = 150) {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn().catch(() => null);
    if (v) return v;
    if (Date.now() > end) return null;
    await sleep(step);
  }
}

async function fakeModel() {
  const p = start(PY, [path.join(ROOT, 'tests/fake_bedrock_endpoint.py'), '0'], { cwd: ROOT });
  let out = '', err = '';
  p.stdout.on('data', (d) => (out += d));
  p.stderr.on('data', (d) => (err += d));
  const url = await waitFor(async () => (out.match(/on (http:\/\/127\.0\.0\.1:\d+)/) || [])[1]);
  // The reason, not just the fact (#662: a bind that waited for reverse DNS looked the same).
  if (!url) throw new Error(`fake model did not start (exit ${p.exitCode}): ${out.slice(-400)}${err.slice(-800)}`);
  return url;
}

async function server(dataDir, fakeUrl) {
  const env = { ...process.env };
  for (const k of Object.keys(env)) if (k.startsWith('AWS_') || k.startsWith('STIRA_PROVIDER') || k === 'STIRA_MODEL') delete env[k];
  Object.assign(env, {
    PYTHONPATH: `${path.join(ROOT, 'src')}${path.delimiter}${ROOT}`,
    STIRA_PORT: String(PORT),
    STIRA_STAGE: '1',
    STIRA_HEARTBEAT: '0',
    PYTHONUNBUFFERED: '1',
    AWS_ENDPOINT_URL: fakeUrl,
    AWS_ENDPOINT_URL_BEDROCK_RUNTIME: fakeUrl,
    AWS_ACCESS_KEY_ID: 'offline-test-key',
    AWS_SECRET_ACCESS_KEY: 'offline-fake-secret',
    AWS_REGION: 'us-east-1',
    AWS_DEFAULT_REGION: 'us-east-1',
    AWS_CONFIG_FILE: '/dev/null',
    AWS_SHARED_CREDENTIALS_FILE: '/dev/null',
  });
  const p = start(PY, [path.join(ROOT, 'tests/stira_headless_harness.py'), dataDir], { cwd: dataDir, env });
  const log = fs.createWriteStream(path.join(dataDir, 'server.log'));
  p.stdout.pipe(log);
  p.stderr.pipe(log);
  const ok = await waitFor(async () => (await fetch(`${BASE}/stage/`)).ok, 60000, 300);
  if (!ok) throw new Error(`server did not start: ${fs.readFileSync(path.join(dataDir, 'server.log'), 'utf8').slice(-2000)}`);
  return p;
}

// --- CDP over a pipe -------------------------------------------------------------------------
function chrome(profile) {
  const p = spawn(CHROME, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--remote-debugging-pipe', `--user-data-dir=${profile}`, '--window-size=1300,860', 'about:blank'], { stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'] });
  children.push(p);
  const out = p.stdio[3];
  const inp = p.stdio[4];
  let id = 0;
  const waiting = new Map();
  const listeners = [];
  let buf = '';
  inp.on('data', (d) => {
    buf += d.toString('utf8');
    let k;
    while ((k = buf.indexOf('\0')) >= 0) {
      const msg = JSON.parse(buf.slice(0, k));
      buf = buf.slice(k + 1);
      if (msg.id && waiting.has(msg.id)) {
        const { resolve, reject } = waiting.get(msg.id);
        waiting.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      } else for (const l of listeners) l(msg);
    }
  });
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    id += 1;
    waiting.set(id, { resolve, reject });
    out.write(`${JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) })}\0`);
  });
  return { send, on: (fn) => listeners.push(fn), proc: p };
}

async function openPage(cdp, events) {
  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
  const s = (m, p) => cdp.send(m, p, sessionId);
  cdp.on((msg) => {
    if (msg.sessionId !== sessionId) return;
    events.push(msg);
    if (msg.method === 'Page.javascriptDialogOpening') s('Page.handleJavaScriptDialog', { accept: false }).catch(() => {});
  });
  await s('Page.enable');
  await s('Network.enable');
  await s('Runtime.enable');
  await s('Log.enable');
  return s;
}

const evaluate = async (s, expression) => {
  const r = await s('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(`${expression.slice(0, 80)}: ${JSON.stringify(r.exceptionDetails).slice(0, 400)}`);
  return r.result.value;
};

async function run(label, boardSvg) {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), `board-svg-${label}-`));
  fs.writeFileSync(path.join(dataDir, 'gateway-config.json'), JSON.stringify({ ui: boardSvg ? { home: 'chat' } : { home: 'chat', board_svg: false } }));
  const fakeUrl = await fakeModel();
  const srv = await server(dataDir, fakeUrl);
  const cdp = chrome(path.join(dataDir, 'chrome'));
  const events = [];
  const s = await openPage(cdp, events);
  const report = { label, boardSvg, checks: {} };
  const check = (name, ok, detail) => {
    report.checks[name] = ok ? 'ok' : `FAIL ${detail === undefined ? '' : JSON.stringify(detail).slice(0, 600)}`;
  };
  try {
    await s('Emulation.setDeviceMetricsOverride', { width: 1300, height: 860, deviceScaleFactor: 1, mobile: false });
    await s('Page.navigate', { url: `${BASE}/stage/` });
    await waitFor(async () => (await evaluate(s, "document.readyState === 'complete' && !!document.getElementById('stage-input')")), 20000);
    // the page has read the chat history (its composer is ready) before we send
    await waitFor(async () => events.some((e) => e.method === 'Network.loadingFinished' && events.some((r) => r.method === 'Network.responseReceived' && r.params.requestId === e.params.requestId && r.params.response.url.includes('/api/chat/history'))), 20000);
    await sleep(1500);
    const doc = events.find((e) => e.method === 'Network.responseReceived' && e.params.type === 'Document');
    const headers = doc ? Object.fromEntries(Object.entries(doc.params.response.headers).map(([k, v]) => [k.toLowerCase(), v])) : {};
    check('csp header unchanged', headers['content-security-policy'] === EXPECTED_CSP, headers['content-security-policy']);
    check('page flag', (await evaluate(s, "document.getElementById('app').dataset.boardSvg || ''")) === (boardSvg ? 'on' : ''));
    const before = events.length;
    // send the scripted reply through the page's own composer
    await evaluate(s, `(() => { const i = document.getElementById('stage-input'); i.value = ${JSON.stringify(`SAY ${REPLY}`)}; i.dispatchEvent(new Event('input', { bubbles: true })); document.getElementById('stage-send').click(); return true; })()`);
    const shown = await waitFor(async () => (await evaluate(s, "(() => { const b = document.getElementById('board'); return b && !b.hidden && b.dataset.state === 'open' && document.querySelectorAll('#board-body .md-code').length >= 5; })()")), 30000, 300);
    check('board opened with the reply', Boolean(shown));
    await sleep(2500);
    const info = await evaluate(s, `(() => {
      const imgs = [...document.querySelectorAll('#board-body img')];
      return {
        imgs: imgs.map((i) => ({ src: i.getAttribute('src') || '', alt: i.alt, complete: i.complete, nw: i.naturalWidth, nh: i.naturalHeight, w: i.getBoundingClientRect().width, attrs: [...i.attributes].map((a) => a.name) })),
        notes: [...document.querySelectorAll('#board-body .md-svg-note')].map((n) => n.textContent),
        codes: [...document.querySelectorAll('#board-body .md-code:not(.md-svg) code')].map((c) => c.textContent.slice(0, 40)),
        svgEls: document.querySelectorAll('#board-body svg, #board-body script, #board-body foreignObject, #board-body iframe').length,
        bodyW: document.getElementById('board-body').getBoundingClientRect().width,
        pwned: window.__pwned === undefined ? null : String(window.__pwned),
        nodes: document.querySelectorAll('#board-body *').length,
      };
    })()`);
    report.board = { imgs: info.imgs.map(({ src, ...r }) => ({ ...r, src: src.slice(0, 40) })), notes: info.notes, codes: info.codes, nodes: info.nodes };
    check('no element of the SVG in the page DOM (svg/script/foreignObject/iframe)', info.svgEls === 0, info.svgEls);
    check('canary untouched', info.pwned === null, info.pwned);
    if (boardSvg) {
      check('four pictures (3 samples + the cleaned hostile one)', info.imgs.length === 4, info.imgs.length);
      check('every picture is our data: SVG, decoded', info.imgs.every((i) => i.src.startsWith('data:image/svg+xml;charset=utf-8,') && i.complete && i.nw > 0), info.imgs);
      check('pictures fit the board', info.imgs.every((i) => i.w <= info.bodyW + 0.5), [info.bodyW, info.imgs.map((i) => i.w)]);
      check('alt text', JSON.stringify(info.imgs.map((i) => i.alt).slice(0, 3)) === JSON.stringify(['珈琲カップ', '月別の件数', '構成図']), info.imgs.map((i) => i.alt));
      check('the hostile picture names what was left out', info.notes.some((n) => /foreignObject/.test(n) && /animate/.test(n)), info.notes);
      check('the DOCTYPE one stays code with its reason', info.notes.some((n) => /DOCTYPE/.test(n)) && info.codes.some((c) => c.startsWith('<!DOCTYPE')), info);
      // pixels: draw each picture into a canvas and read known points
      const px = await evaluate(s, `(async () => {
        const imgs = [...document.querySelectorAll('#board-body img')];
        const at = (img, pts) => { const c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight; const g = c.getContext('2d'); g.drawImage(img, 0, 0); return pts.map(([x, y]) => [...g.getImageData(x, y, 1, 1).data]); };
        return { coffee: at(imgs[0], [[100, 120], [100, 86], [100, 172], [5, 5]]), bars: at(imgs[1], [[80, 130], [260, 100], [200, 60]]), diagram: at(imgs[2], [[30, 60], [180, 60], [305, 60], [114, 70]]), hostile: at(imgs[3], [[50, 50]]) };
      })()`);
      const near = (p, rgb, tol = 24) => p[3] > 200 && rgb.every((v, k) => Math.abs(p[k] - v) <= tol);
      const clear = (p) => p[3] === 0;
      report.pixels = px;
      check('coffee cup pixels (white cup, brown coffee, grey saucer, clear corner)', near(px.coffee[0], [255, 255, 255]) && px.coffee[1][0] > px.coffee[1][2] + 30 && px.coffee[1][3] > 200 && near(px.coffee[2], [0xd9, 0xd4, 0xc7]) && clear(px.coffee[3]), px.coffee);
      check('bar chart pixels (blue bar, orange bar, white gap)', near(px.bars[0], [0x3b, 0x82, 0xf6]) && near(px.bars[1], [0xf9, 0x73, 0x16]) && near(px.bars[2], [255, 255, 255]), px.bars);
      check('diagram pixels (three boxes, dark arrow)', near(px.diagram[0], [0xe0, 0xf2, 0xfe]) && near(px.diagram[1], [0xdc, 0xfc, 0xe7]) && near(px.diagram[2], [0xfe, 0xf3, 0xc7]) && near(px.diagram[3], [0x33, 0x33, 0x33], 40), px.diagram);
      check('hostile picture: only the green rect (no style / set / animate fill)', near(px.hostile[0], [0x22, 0xc5, 0x5e]), px.hostile);
      // the browser reading the img's SVG again: only allowed elements / attributes
      const back = await evaluate(s, `(() => {
        const ok = new Set(${JSON.stringify(['svg', 'g', 'defs', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon', 'text', 'tspan', 'linearGradient', 'radialGradient', 'stop', 'title', 'desc'])});
        const bad = [];
        for (const img of document.querySelectorAll('#board-body img')) {
          const text = decodeURIComponent(img.getAttribute('src').split(',').slice(1).join(','));
          const d = new DOMParser().parseFromString(text, 'image/svg+xml');
          if (d.getElementsByTagName('parsererror').length) bad.push('parsererror');
          for (const el of d.getElementsByTagName('*')) {
            if (!ok.has(el.localName) || el.namespaceURI !== 'http://www.w3.org/2000/svg') bad.push(el.localName);
            for (const a of el.attributes) if (/^on|href|style|class/i.test(a.name) || a.namespaceURI && a.name !== 'xmlns' || /url\\((?!#)/.test(a.value)) bad.push(el.localName + '.' + a.name);
          }
        }
        return bad;
      })()`);
      check('re-reading every <img> SVG in the browser: nothing outside the allow-list', back.length === 0, back);
    } else {
      check('off: no picture, SVG blocks stay code', info.imgs.length === 0 && info.codes.filter((c) => c.startsWith('<svg') || c.startsWith('<!DOCTYPE')).length === 5, info);
    }
    // axe on the board (when axe-core is installed: npm install --no-save axe-core)
    const axePath = path.join(ROOT, 'node_modules/axe-core/axe.min.js');
    if (fs.existsSync(axePath)) {
      await evaluate(s, `${fs.readFileSync(axePath, 'utf8')}; true`);
      const ax = await evaluate(s, "axe.run(document.getElementById('board'), { resultTypes: ['violations'] }).then((r) => r.violations.map((v) => v.id + ':' + v.nodes.length))");
      report.axe = ax;
      // compared after both runs: the "on" board adds no violation to what "off" (= main) has
    }
    // DOMParser on the raw hostile SVG: a separate document, nothing runs or loads
    await evaluate(s, `(() => { const d = new DOMParser().parseFromString(${JSON.stringify(HOSTILE)}, 'image/svg+xml'); window.__parsedElements = d.getElementsByTagName('*').length; return true; })()`);
    await sleep(1500);
    check('DOMParser parsed the raw hostile SVG', (await evaluate(s, 'window.__parsedElements')) > 5);
    check('canary still untouched after DOMParser', (await evaluate(s, 'window.__pwned === undefined')) === true);
    if (SHOTS) {
      const shot = async (name) => fs.writeFileSync(path.join(OUT, name), Buffer.from((await s('Page.captureScreenshot', { format: 'png' })).data, 'base64'));
      await shot(`board-svg-${label}-1300.png`);
      await evaluate(s, "document.getElementById('board-body').scrollTop = 99999, true");
      await sleep(300);
      await shot(`board-svg-${label}-1300-end.png`);
      await s('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
      await sleep(1200);
      await evaluate(s, "document.getElementById('board-body').scrollTop = 0, true");
      await sleep(300);
      await shot(`board-svg-${label}-390.png`);
      if (boardSvg) {
        const w390 = await evaluate(s, "(() => { const b = document.getElementById('board-body').getBoundingClientRect().width; return [...document.querySelectorAll('#board-body img')].every((i) => i.getBoundingClientRect().width <= b + 0.5) && document.documentElement.scrollWidth <= 390; })()");
        check('390px: pictures fit, no sideways page scroll', w390 === true);
      }
    }
    // the log (backlog) draws the same reply without pictures, with the setting on or off:
    // talk.js renders it without opts.svg (#453 review: a DOM check, not a source match)
    await evaluate(s, "document.getElementById('btn-log').click(), true");
    const logged = await waitFor(async () => (await evaluate(s, "(() => { const l = document.getElementById('backlog-list'); return l && !document.getElementById('backlog').hidden && l.querySelectorAll('.md-code code').length >= 5 ? { imgs: l.querySelectorAll('img').length, figures: l.querySelectorAll('.md-svg').length, svgCodes: [...l.querySelectorAll('.md-code code')].filter((c) => c.textContent.startsWith('<svg')).length } : null; })()")), 10000, 200);
    report.log = logged;
    check('log: the reply is drawn without pictures (SVG blocks stay code)', Boolean(logged) && logged.imgs === 0 && logged.figures === 0 && logged.svgCodes === 4, logged);
    await evaluate(s, "document.getElementById('backlog-close').click(), true");
    // network and console while the reply was shown
    const after = events.slice(before);
    const reqs = after.filter((e) => e.method === 'Network.requestWillBeSent').map((e) => e.params.request.url);
    const probes = events.filter((e) => e.method === 'Network.requestWillBeSent' && e.params.request.url.includes('/__probe')).map((e) => e.params.request.url);
    const foreign = reqs.filter((u) => !u.startsWith(`${BASE}/api/`) && !u.startsWith('data:'));
    report.requests = { total: reqs.length, api: reqs.filter((u) => u.startsWith(`${BASE}/api/`)).length, data: reqs.filter((u) => u.startsWith('data:')).length, other: foreign };
    check('no request to any probe URL (whole session)', probes.length === 0, probes);
    check('no request other than the page APIs while the reply was shown', foreign.length === 0, foreign);
    const errors = after.filter((e) => e.method === 'Runtime.exceptionThrown' || (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error') || (e.method === 'Log.entryAdded' && e.params.entry.level === 'error'));
    report.consoleErrors = errors.map((e) => JSON.stringify(e.params).slice(0, 300));
    check('no console error / exception while the reply was shown', errors.length === 0, report.consoleErrors);
    check('no dialog opened', !events.some((e) => e.method === 'Page.javascriptDialogOpening'));
    // control (the monitor can see a leak): the same hostile SVG put into the page DOM as is
    // (what the board never does) makes probe requests at once
    const mark = events.length;
    await evaluate(s, `(() => { const box = document.createElement('div'); box.hidden = true; box.innerHTML = ${JSON.stringify(HOSTILE)}; document.body.append(box); return true; })()`);
    await sleep(1500);
    const leaked = events.slice(mark).filter((e) => e.method === 'Network.requestWillBeSent' && e.params.request.url.includes('/__probe')).map((e) => e.params.request.url.replace(BASE, ''));
    report.control = leaked;
    check('control: the raw SVG in the DOM does reach the probes (the monitor works)', leaked.length > 0, leaked);
  } finally {
    cdp.proc.kill('SIGTERM');
    srv.kill('SIGTERM');
    await sleep(800);
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
  return report;
}

const reports = [];
try {
  reports.push(await run('on', true));
  stopAll();
  await sleep(1000);
  reports.push(await run('off', false));
} finally {
  stopAll();
}
if (reports.length === 2 && reports[0].axe && reports[1].axe) {
  const base = new Set(reports[1].axe.map((v) => v.split(':')[0]));
  const added = reports[0].axe.filter((v) => !base.has(v.split(':')[0]));
  reports[0].checks['axe: no violation on the board beyond the setting-off board'] = added.length ? `FAIL ${JSON.stringify(added)}` : 'ok';
}
const failed = reports.flatMap((r) => Object.entries(r.checks).filter(([, v]) => v !== 'ok').map(([k, v]) => `${r.label}: ${k}: ${v}`));
console.log(JSON.stringify({ out: OUT, reports, failed }, null, 1));
process.exit(failed.length ? 1 : 0);
