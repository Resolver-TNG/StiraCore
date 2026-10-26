// Settings hub heartbeat section (#472) via jsdom: the switch, the interval bounds in minutes
// (rounded into the limits on leaving a field, the other bound follows), field errors, the env
// pin through the shared settings-surface part (#469: input off + the variable's name, never its
// value), the running status, badges, the change list, the save body, axe.
// usage: node settings_heartbeat_dom.cjs <settings.html> <pinned.json> <standalone.json> <kept.json> <marker>
// Exit codes: 0 = ok, 1 = failure, 77 = deps missing (skip).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { shimSource } = require('../ui_shim.cjs');
const { polyfillDialog } = require('../dialog_polyfill.cjs');

let JSDOM;
let axeSource;
try {
  ({ JSDOM } = require('jsdom'));
  axeSource = require('axe-core').source;
} catch (err) {
  console.log(`SKIP: jsdom/axe-core not available (${err.message})`);
  process.exit(77);
}

const JS = path.join(__dirname, '..', '..', 'dashboard', 'static', 'js');
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 30; i += 1) await tick(); }

async function openPage(html, hub, { history = [], saveReply = null } = {}) {
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1/settings' });
  const { window } = dom;
  polyfillDialog(window);
  const posts = [];
  window.fetch = async (url, options = {}) => {
    const json = body => ({ ok: true, status: 200, json: async () => body });
    if (options.method === 'POST') {
      posts.push({ url, body: JSON.parse(options.body) });
      if (saveReply) return saveReply;
      return { ok: false, status: 409, json: async () => ({}) };
    }
    if (url.startsWith('/api/config/history')) return json({ entries: history });
    if (url.startsWith('/api/config/hub')) return json(hub);
    return json({});
  };
  window.setInterval = () => 0;
  window.eval(shimSource());
  window.eval(fs.readFileSync(path.join(JS, 'confirm_dialog.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'breaker_pairs.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'settings_surface.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'settings_hub.js'), 'utf8'));
  await settle();
  const doc = window.document;
  const $ = id => doc.getElementById(id);
  // Type into a minutes box and leave it (the page rounds on "change", like a real blur).
  const enter = async (id, value) => {
    $(id).value = value;
    $(id).dispatchEvent(new window.Event('change'));
    await settle();
  };
  const badge = (kind, group) => doc.querySelector(`.${kind}[data-group="${group}"]`).textContent;
  return { window, doc, $, enter, posts, badge };
}

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const pinned = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const standalone = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
  const kept = JSON.parse(fs.readFileSync(process.argv[5], 'utf8'));
  const marker = process.argv[6];
  assert.ok(marker && marker.length > 8, 'marker value');

  // --- No chat server, nothing pinned: edit, round, follow, save --------------------------
  {
    const { window, doc, $, enter, posts, badge } = await openPage(html, standalone);
    const t = window.t;
    assert.notEqual(t('hb.sec'), 'hb.sec', 'catalog loaded');
    assert.equal(badge('timing-badge', 'heartbeat'), t('hub.timing_next_start'));
    // Defaults: off, 5 and 180 minutes; the one-line "no model, no token cost" description.
    assert.equal($('hb-enabled').checked, false);
    assert.equal($('hb-enabled').disabled, false);
    assert.equal($('hb-floor').value, '5');
    assert.equal($('hb-ceil').value, '180');
    assert.equal($('hb-floor').min, '1');
    assert.equal($('hb-ceil').max, '1440');
    assert.equal($('hb-desc').textContent.trim(), t('hb.desc'));
    assert.equal($('hb-live-summary').textContent, t('hb.live_unknown'));
    assert.equal($('hub-save').disabled, true, 'nothing changed yet');

    // On: no confirmation dialog anywhere, just a draft change.
    $('hb-enabled').click();
    await settle();
    assert.equal($('hb-enabled').checked, true);
    assert.equal($('hub-save').disabled, false);

    // Rounded into the limits on leaving the field, with a note saying what happened.
    await enter('hb-ceil', '5000');
    assert.equal($('hb-ceil').value, '1440');
    assert.equal($('hb-interval-note').textContent,
      t('hub.note_rounded', { from: '5000', to: t('hb.unit_hours', { n: '24' }) }));
    await enter('hb-ceil', '2.4');
    assert.equal($('hb-ceil').value, '2', 'whole minutes');
    // The floor passes the ceiling: the ceiling follows it (no error left to puzzle over).
    await enter('hb-floor', '30');
    assert.equal($('hb-floor').value, '30');
    assert.equal($('hb-ceil').value, '30');
    assert.equal($('hb-interval-note').textContent,
      t('hb.note_ceil_raised', { to: t('hb.unit_minutes', { n: '30' }) }));
    // The ceiling below the floor: the floor follows it.
    await enter('hb-ceil', '10');
    assert.equal($('hb-floor').value, '10');
    assert.equal($('hb-interval-note').textContent,
      t('hb.note_floor_lowered', { to: t('hb.unit_minutes', { n: '10' }) }));
    assert.equal($('err-heartbeat-interval_floor_sec').hidden, true);
    await enter('hb-ceil', '120');
    // Not a number: the draft keeps the saved value and says why.
    await enter('hb-floor', 'abc');
    assert.equal($('hb-floor').value, '5');
    assert.equal($('hb-interval-note').textContent, t('hub.err_not_number'));
    await enter('hb-floor', '10');

    // The change list in words, minutes / hours (never raw seconds).
    const diff = [...doc.querySelectorAll('#hub-diff-list li')].map(li => li.textContent).join('\n');
    // The switch by the words on the page ("Use the heartbeat"), not the section title.
    assert.ok(diff.split('\n').some(line => line.startsWith(t('hb.enabled'))), diff);
    assert.ok(diff.includes(t('hb.unit_hours', { n: '3' })) && diff.includes(t('hb.unit_hours', { n: '2' })), diff);
    assert.ok(diff.includes(t('hb.unit_minutes', { n: '10' })), diff);
    assert.ok(!/10800|7200/.test(diff), diff);

    // Server errors in the page's words (seconds -> minutes / hours), on the input.
    const errors = [
      { field: 'heartbeat.interval_floor_sec', code: 'interval_out_of_range', params: { min: 60, max: 86400 } },
      { field: 'heartbeat.interval_ceil_sec', code: 'floor_above_ceil', params: { limit: 600 } },
    ];
    // Rendered through the shared error path: a 400 from the save.
    const reply = { ok: false, status: 400, json: async () => ({ error: 'Invalid settings.', errors }) };
    const page2 = await openPage(html, standalone, { saveReply: reply });
    page2.$('hb-enabled').click();
    await settle();
    page2.$('hub-save').click();
    await settle();
    assert.equal(page2.posts.length, 1);
    const floorErr = page2.$('err-heartbeat-interval_floor_sec');
    assert.equal(floorErr.hidden, false);
    assert.equal(floorErr.textContent, page2.window.t('hb.err_out_of_range', {
      min: page2.window.t('hb.unit_minutes', { n: '1' }), max: page2.window.t('hb.unit_hours', { n: '24' }),
    }));
    assert.equal(page2.$('hb-floor').getAttribute('aria-invalid'), 'true');
    assert.ok(page2.$('hb-floor').getAttribute('aria-describedby').split(' ').includes('err-heartbeat-interval_floor_sec'));
    assert.match(page2.$('err-heartbeat-interval_ceil_sec').textContent, /10/);
    page2.window.close();

    // Keyboard: Enter in a minutes box commits it like leaving the field.
    $('hb-floor').value = '0';
    $('hb-floor').dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter' }));
    await settle();
    assert.equal($('hb-floor').value, '1', 'Enter rounds into the limits too');
    await enter('hb-floor', '10');

    // axe over the section (jsdom: no color-contrast).
    window.eval(axeSource);
    const result = await window.axe.run($('sec-heartbeat'), { rules: { 'color-contrast': { enabled: false } } });
    if (result.violations.length) {
      for (const v of result.violations) console.log(`[${v.impact}] ${v.id}: ${v.help} (${v.nodes.length})`);
      process.exit(1);
    }

    // Save: only the heartbeat group, in seconds, with no confirmation.
    $('hub-save').click();
    await settle();
    assert.equal(posts.length, 1, 'posted once, no confirmation needed');
    const { changes, acknowledged_dangers: acknowledged } = posts[0].body;
    assert.deepEqual(Object.keys(changes), ['heartbeat']);
    assert.deepEqual(changes.heartbeat, { enabled: true, interval_floor_sec: 600, interval_ceil_sec: 7200 });
    assert.ok(!acknowledged || acknowledged.length === 0, 'nothing to confirm');

    // Reset to defaults brings the heartbeat back to off / 5 / 180 in the draft.
    $('hub-defaults').click();
    await settle();
    assert.equal($('hb-enabled').checked, false);
    assert.equal($('hb-floor').value, '5');
    assert.equal($('hb-ceil').value, '180');
    window.close();
  }

  // --- Running chat server, STIRA_HEARTBEAT set: pinned switch shows the running value ----
  {
    const { window, doc, $, badge } = await openPage(html, pinned);
    const t = window.t;
    assert.equal(pinned.values.heartbeat.enabled, true, 'saved on');
    assert.equal(pinned.apply.heartbeat.running.enabled, false, 'runs off (the variable)');
    const input = $('hb-enabled');
    assert.equal(input.getAttribute('data-surface-field'), 'gateway.hb_enabled');
    assert.equal(input.disabled, true);
    assert.equal(input.getAttribute('data-surface-pinned'), 'true');
    assert.equal(input.checked, false, 'the running value, not the saved one');
    const note = $('pin-gateway-hb_enabled');
    assert.equal(note.hidden, false);
    assert.equal(note.textContent, t('surface.pinned_by', { name: 'STIRA_HEARTBEAT' }));
    assert.ok(input.getAttribute('aria-describedby').split(' ').includes(note.id));
    assert.equal($('hb-enabled-live').textContent, t('cost.pinned_live'));
    assert.equal(doc.querySelectorAll('#sec-heartbeat .pin-note:not([hidden])').length, 1);
    // The intervals are not pinned and stay editable.
    assert.equal($('hb-floor').disabled, false);
    assert.equal($('hb-ceil').disabled, false);
    // Badges: after restart / applied (the env value runs).
    assert.equal(badge('timing-badge', 'heartbeat'), t('hub.timing_restart'));
    assert.equal(badge('apply-status', 'heartbeat'), t('hub.status_applied'));
    // The running status in the same card.
    assert.equal($('hb-live-summary').textContent, t('hb.live_running'));
    const facts = [...doc.querySelectorAll('#hb-live-facts dt')].map(dt => dt.textContent);
    assert.deepEqual(facts, [t('hb.live_last'), t('hb.live_next'), t('hb.live_interval'), t('hb.live_level'), t('hb.live_count')]);
    const values = [...doc.querySelectorAll('#hb-live-facts dd')].map(dd => dd.textContent);
    assert.equal(values[2], t('hb.unit_minutes', { n: '15' }));
    assert.equal(values[3], t('hb.level_normal'));
    // The location card lists the variable by name with "after restart"; never its value.
    const rows = [...doc.querySelectorAll('#pinned-list li')].map(li => li.textContent);
    const row = rows.find(r => r.startsWith('STIRA_HEARTBEAT '));
    assert.ok(row && row.includes(t('surface.grp_heartbeat')) && row.includes(t('hub.timing_restart')), rows.join(' | '));
    assert.equal(doc.documentElement.outerHTML.includes(marker), false, 'the env value is never on the page');
    window.eval(axeSource);
    const result = await window.axe.run($('sec-heartbeat'), { rules: { 'color-contrast': { enabled: false } } });
    if (result.violations.length) {
      for (const v of result.violations) console.log(`[${v.impact}] ${v.id}: ${v.help} (${v.nodes.length})`);
      process.exit(1);
    }
    window.close();
  }

  // --- A hand-edited 30 s floor (below the page's 1 minute): kept, no error, others save --
  {
    const { $, enter, posts } = await openPage(html, kept);
    assert.equal(kept.values.heartbeat.interval_floor_sec, 30);
    assert.equal($('hb-floor').value, '0.5');
    assert.equal($('err-heartbeat-interval_floor_sec').hidden, true);
    // Leaving the field untouched keeps the saved seconds (no rounding, no change).
    await enter('hb-floor', '0.5');
    assert.equal($('hub-save').disabled, true, 'nothing changed');
    await enter('hb-ceil', '60');
    assert.equal($('hub-save').disabled, false);
    $('hub-save').click();
    await settle();
    assert.equal(posts.length, 1);
    assert.equal(posts[0].body.changes.heartbeat.interval_floor_sec, 30, 'kept as saved');
    assert.equal(posts[0].body.changes.heartbeat.interval_ceil_sec, 3600);
  }
  console.log('ok: heartbeat section (switch, minutes, rounding, follow, errors, env pin, status, badges, save body, axe)');
}

main().catch((err) => { console.error(err); process.exit(1); });
