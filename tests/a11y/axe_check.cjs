// Run axe-core over every *.html in a directory via jsdom (#197).
// Exit codes: 0 = no violations, 1 = violations/error, 77 = deps missing (skip).
const fs = require('node:fs');
const path = require('node:path');

let JSDOM;
let axeSource;
try {
  ({ JSDOM } = require('jsdom'));
  axeSource = require('axe-core').source;
} catch (err) {
  console.log(`SKIP: jsdom/axe-core not available (${err.message})`);
  process.exit(77);
}

async function checkFile(file) {
  const html = fs.readFileSync(file, 'utf8');
  // "outside-only": page <script> tags are NOT executed; only allows us to eval axe.
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  dom.window.eval(axeSource);
  const result = await dom.window.axe.run(dom.window.document, {
    // color-contrast cannot be computed in jsdom; covered by test_ui_contrast.py.
    rules: { 'color-contrast': { enabled: false } }
  });
  dom.window.close();
  return result;
}

async function main() {
  const dir = process.argv[2];
  if (!dir || !fs.existsSync(dir)) {
    console.log(`usage: node axe_check.cjs <dir> (got ${dir})`);
    process.exit(1);
  }
  const files = fs.readdirSync(dir).filter(f => f.endsWith('.html')).sort();
  if (files.length === 0) {
    console.log(`no .html files in ${dir}`);
    process.exit(1);
  }
  let total = 0;
  for (const name of files) {
    const res = await checkFile(path.join(dir, name));
    total += res.violations.length;
    console.log(`${name}: ${res.passes.length} passes, ${res.violations.length} violations`);
    for (const v of res.violations) {
      console.log(`  [${v.impact}] ${v.id}: ${v.help}`);
      console.log(`    ${v.helpUrl}`);
      for (const node of v.nodes) console.log(`    - ${node.html}`);
    }
  }
  if (total > 0) {
    console.log(`FAIL: ${total} violation(s) in ${files.length} page(s)`);
    process.exit(1);
  }
  console.log(`OK: ${files.length} page(s), no violations`);
  process.exit(0);
}

main().catch(err => {
  console.log(`ERROR: ${err && err.stack ? err.stack : err}`);
  process.exit(1);
});
