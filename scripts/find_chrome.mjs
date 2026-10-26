// Where Chrome / Chromium is on this machine, for scripts/check_board_svg_browser.mjs (#453).
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// CHROME when set; otherwise the usual install places of Chrome / Chromium for this platform
// (macOS: the app bundles; Linux and others: the command names on PATH). null = none found.
export function findChrome(env = process.env, platform = process.platform, home = os.homedir()) {
  if (env.CHROME) return fs.existsSync(env.CHROME) ? env.CHROME : null;
  const app = (name, bin) => [`/Applications/${name}.app/Contents/MacOS/${bin}`, path.join(home, `Applications/${name}.app/Contents/MacOS/${bin}`)];
  const onPath = (names) => names.flatMap((n) => (env.PATH || '').split(path.delimiter).filter(Boolean).map((d) => path.join(d, n)));
  const candidates = platform === 'darwin'
    ? [...app('Google Chrome', 'Google Chrome'), ...app('Chromium', 'Chromium'), ...onPath(['google-chrome', 'chromium'])]
    : platform === 'win32'
      ? ['PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'].filter((k) => env[k]).map((k) => path.join(env[k], 'Google', 'Chrome', 'Application', 'chrome.exe'))
      : onPath(['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome']);
  return candidates.find((c) => { try { return fs.statSync(c).isFile(); } catch { return false; } }) || null;
}
