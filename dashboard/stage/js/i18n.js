// UI strings for Stage (#341): the dashboard's translation catalog (ja / en), keys "stage.*".
// Served by the chat server, the page embeds the catalog as JSON (#i18n-catalog, like every
// dashboard page); the standalone demo fetches the same catalog files. Text is always set
// with textContent, never parsed as HTML.

export const LANGS = ['ja', 'en'];
const STORE = 'stira.lang'; // shared with the dashboard pages
const STEP_IDS = ['calm', 'thinking', 'tool', 'memory', 'approval', 'achievement', 'curious', 'worried', 'critical'];

export function pickLang(search, navLang, stored) {
  const q = new URLSearchParams(search).get('lang');
  if (LANGS.includes(q)) return q;
  if (LANGS.includes(stored)) return stored;
  return typeof navLang === 'string' && navLang.toLowerCase().startsWith('ja') ? 'ja' : 'en';
}

export function storedLang() {
  try {
    return localStorage.getItem(STORE);
  } catch {
    return null;
  }
}

export function storeLang(lang) {
  try {
    localStorage.setItem(STORE, lang);
  } catch {
    /* storage unavailable */
  }
}

// {name} placeholders, like the dashboard's t().
export function format(text, vars) {
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (m, k) => (Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : m));
}

// translator(catalog, lang): key -> text in `lang`, falling back to ja, then the key itself.
export function translator(catalog, lang) {
  const primary = (catalog && catalog[lang]) || {};
  const fallback = (catalog && catalog.ja) || {};
  return (key, vars) => {
    const own = Object.prototype.hasOwnProperty;
    const text = own.call(primary, key) ? primary[key] : own.call(fallback, key) ? fallback[key] : key;
    return format(text, vars);
  };
}

// The object the UI reads (S.name, S.steps[id], ...), built from the flat "stage.*" keys.
export function buildStrings(catalog, lang) {
  const t = translator(catalog, lang);
  const S = { t };
  const keys = Object.keys((catalog && catalog.ja) || {}).filter((k) => k.startsWith('stage.'));
  for (const k of keys) S[k.slice(6)] = t(k);
  S.steps = Object.fromEntries(STEP_IDS.map((id) => [id, t(`stage.step_${id}`)]));
  return S;
}

// Embedded catalog (served page), else the dashboard's catalog files (standalone demo,
// served from dashboard/), else empty (keys are shown).
export async function loadCatalog(doc, fetchFn) {
  const node = doc.getElementById('i18n-catalog');
  if (node) {
    try {
      const data = JSON.parse(node.textContent);
      if (data && data.ja) return data;
    } catch {
      /* fall through */
    }
  }
  const out = { ja: {}, en: {} };
  for (const lang of LANGS) {
    try {
      const r = await fetchFn(`../static/i18n/catalog.${lang}.json`);
      if (r.ok) out[lang] = await r.json();
    } catch {
      /* missing catalog: keys are shown */
    }
  }
  return out;
}
