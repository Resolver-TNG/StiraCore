// Test helper: globals that dashboard scripts expect (stiraFetch, t) inside vm contexts.
const fs = require('node:fs');
const path = require('node:path');

const catalog = JSON.parse(fs.readFileSync(
  path.join(__dirname, '..', 'dashboard', 'static', 'i18n', 'catalog.ja.json'), 'utf8'));

function shimSource() {
  return `var stiraFetch = (...args) => fetch(...args);
var __catalog = ${JSON.stringify(catalog)};
var t = (key, vars) => {
  let text = Object.prototype.hasOwnProperty.call(__catalog, key) ? __catalog[key] : key;
  if (vars) text = text.replace(/\\{(\\w+)\\}/g, (m, n) => (n in vars ? String(vars[n]) : m));
  return text;
};
`;
}

module.exports = { shimSource, catalog };
