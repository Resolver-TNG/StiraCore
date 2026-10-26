# ADR 0015: A verification-only browser tool — open and exercise the agent's own pages, inside the workspace, with no network

**Path:** `docs/adr/0015-verification-browser-tool.md`
**Status:** Accepted (operator, 2026-10-19: the recommendations of Q1–Q7, with Q2 narrowed —
in phase 1 `page_check` is a **card in every YOLO window** until the launch guard is measured
at every start). **Phase 1 implemented** (#449): discovery and the doctor note (an `info`
note: the tool is optional), the refusing proxy, the CDP pipe client, open + error counts +
blocked network and hosts + one screenshot. **Phase 2 implemented** (#449, 2026-10-20): the
input actions of §5, `steps` with `text_changed`, the text excerpt, up to 4 screenshots, page
text framed as data — see [Addendum rev 2](#addendum-rev-2-2026-10-20-phase-2-as-built-and-the-open-questions-of-phases-34);
its questions (Q8–Q12) were answered by the operator on 2026-10-21 — see
[Addendum rev 3](#addendum-rev-3-2026-10-21-the-operators-decisions-on-rev-2s-questions-and-type--wait_for--assertions-as-built):
**`type`, `wait_for` and five assertion forms implemented** (#449); **phase 3 (one named
`localhost` origin) implemented after its canary measurement passed** — see
[Addendum rev 4](#addendum-rev-4-2026-10-21-phase-3--the-canary-measurement-then-one-named-localhost-origin-as-built);
phase 4 not implemented. Review round
(PR #488): every browser session runs only after Chrome **confirmed** its guard, else the
run ends (`browser_guard_unverified`, §3); logs keep the target as `@workspace/<relative
path>` only (§5); **Always is not offered in phase 1** (§6, Q3 deferred).
**Issue:** #449. Related: #89 (web research; `browser` left as "later", closed), #167
(exec tools, sandbox), #145 (tool-path contract tests), #368 (tool-definition tokens), #280
(close acceptance on "proven"), #365 / ADR 0012 (YOLO tiers, `yolo/tiers.py`), #190 /
ADR 0010 (Always / Ask / Never, gate stack), #277 / ADR 0013 (audit contract), #244 /
ADR 0007 (`stira doctor`), #312 (read floor), #243 (write roots), #450 (tool presets, on
hold).
**Decision (proposed):** add **one** standard tool, `page_check`, that opens a page **the
agent made** — an HTML file inside the workspace (phase 1) or one named
`http://localhost:<port>` origin (phase 3) — in a **disposable headless Chrome/Chromium**
found on the host, drives it with a few input actions, and returns counts (page errors,
console errors, blocked network attempts with their host names only), the title, a short
text excerpt and screenshots saved under the workspace for `view_image`. Stira talks to the
browser over **CDP on a pipe** (`--remote-debugging-pipe`, Python standard library only, no
new dependency, no debugging port). **Every request of the browser goes to a refusing proxy
that runs inside Stira** (loopback included), so the network is closed by where the
browser's traffic goes, not by watching it; CDP request interception only adds the
`file://` scope check and the report. The rule starts as **Ask**; in a YOLO window the tool
is **internal** only while the launch guard is verified (otherwise a card). No browser on
the host → the tool is not registered and `stira doctor` says why.

## Context

Measured on 2026-10-19 (`main` = `608153a`, issue #449): asked for an air-hockey game, Stira
wrote three single-file HTML games (18 KB → 22 KB → 32 KB). Driven from outside with
headless Chrome, all three had **0 JavaScript errors, 0 network requests**, and the score
changed when played. Stira itself could not see that: its tools are file tools only, so
"testing" was the model re-reading its own source — the last version was followed by more
than 12 minutes of read-and-fix. The person also had to find the file and open it.

What this ADR is **not**: a web-research browser (#89's `browser` stage — logged-in pages,
arbitrary sites), visual quality judgement, or a way to open the person's own browser
(§8: a person's action, not a tool).

### How this repository already uses a browser (checked)

* **Research and UI verification** (outside the product): headless **system Chrome** driven
  by **puppeteer-core** (Node) or by the Chrome CLI (`--headless --screenshot
  --virtual-time-budget`) — e.g. `docs/research/ux-audit-2026-10-15.md`,
  `docs/research/stage-2026-10-17/report.md`, `docs/research/ui-analysis/settings-hub-walkthrough.md`
  (CDP directly). Nothing is committed that launches a browser.
* **Tests and CI**: `jsdom` + `axe-core` installed on demand (`npm install --no-save`,
  `STIRA_REQUIRE_AXE=1`), no real browser. Node is **not** a runtime dependency of Stira.
* **Runtime**: Python only. The upstream `strands_tools.browser` (`LocalChromiumBrowser`)
  exists behind the `local-chromium-browser` extra (Playwright); it is a general browser:
  arbitrary `navigate`, `evaluate`, `execute_cdp`, `get_cookies` / `set_cookies`, tabs, an
  optional persistent profile.
* **A precedent for a standard tool that starts a helper process**: `lsp_diagnostics`
  (finds `pyright` / `ruff` with `shutil.which`, one-shot process, timeout, process-group
  end, `tools/lsp_tools.py`). **A precedent for "optional component missing → doctor
  finding"**: `_classifier_check` in `diagnostics/doctor.py`.

### Spike (2026-10-19, throwaway, not committed)

macOS 26.5 / M1 Max, Google Chrome 155.0.8059.80 `--headless=new`, Python 3.11 standard
library only: CDP over `--remote-debugging-pipe` (fd 3/4, NUL-framed JSON), a fresh
`--user-data-dir` per run, ephemeral ports only. The page was a `file://` HTML in a temporary
workspace that tries, towards a **canary HTTP + UDP listener on this host's LAN address**
(non-loopback, so a "leak" is observable without leaving the machine): `fetch`, `<img>`,
`<iframe>`, `<link rel=prefetch>`, `sendBeacon`, a `WebSocket`, a WebRTC STUN request; plus
`fetch('file:///etc/hosts')`, a `localhost` fetch, one `console.error`, one thrown error.

| Configuration | Reached the LAN canary | UDP | Notes |
|---|---|---|---|
| none (baseline) | fetch, img, iframe, prefetch, beacon, **ws** | 5 packets | proves the canary sees every channel |
| CDP `Fetch` interception only (fail everything not allowed) | **ws** | **5 packets** | HTTP-ish requests blocked and reported; **WebSocket and WebRTC leak past it** |
| dead proxy (`--proxy-server`) + `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`, interception only *reports* | nothing | 0 | `localhost` still worked (Chrome bypasses the proxy for loopback by default) |
| the same + `--proxy-bypass-list=<-loopback>` | nothing (also no loopback http **or ws**) | 0 | loopback goes through the proxy too |

Also measured: the error counts were right in every configuration (1 page error, 1 console
error, from `Runtime.exceptionThrown` / `Runtime.consoleAPICalled`); `Network.webSocketCreated`
reports the WebSocket's host in every configuration; `Fetch.requestPaused` sees the
`file:///etc/hosts` read (so a `file://` scope check is possible there; the proxy does not
see `file://`); `window.open` creates a **separate target** the page's interception does not
cover; `Browser.setDownloadBehavior {deny}` refuses a download and emits
`downloadWillBegin`; Chrome's own component extensions start even with
`--disable-extensions` (their traffic goes to the proxy as well). Launch to first result ≈
0.5–0.7 s on top of the page's own wait; a default-viewport screenshot of that simple page ≈
6 KB PNG. **Not measured:** Linux, Windows, DNS prefetch with a fixed proxy, WebRTC over TCP,
Chromium/Edge builds, service workers on `localhost`.

Playwright for comparison (not installed, downloaded only): the macOS arm64 wheel is 43 MB
(132 MB unpacked, it bundles its Node driver); the browser is a separate
`playwright install` download unless `channel="chrome"` uses the system Chrome.

## Decisions (each: options, cost, recommendation)

Columns: **Safety** (what the design guarantees), **Convenience** (for the person / the
agent), **Cost** (distribution, maintenance), **Platforms**.

### 1. The base — **recommended: (b), as a standard tool registered only when a browser is found**

| Option | Safety | Convenience | Cost | Platforms |
|---|---|---|---|---|
| (a) Playwright (Python) | good primitives (routing, contexts) — but routing alone has the same WS/WebRTC gaps as (b)'s interception; still needs the proxy | rich API, waits built in | +43 MB wheel / 132 MB unpacked, Node driver inside, a separate browser download (size not measured) or `channel="chrome"`; a fast-moving dependency in a Python package that has none of that size | all three, Playwright's own matrix |
| (a′) upstream `strands_tools.browser` | its surface is a general browser (`evaluate`, `execute_cdp`, cookies, arbitrary navigation, persistent profile option); restricting it means wrapping and removing most actions | ready-made | Playwright cost + tracking an upstream tool spec (#254 cache, #368 tokens) | as (a) |
| **(b) system Chrome/Chromium/Edge over CDP on a pipe, Python stdlib** | no debugging port (nothing else on the host can attach); all policy in Stira's code; the proxy guarantee measured above | needs a Chromium-family browser installed (most desktops have one) | one module, estimated 400–600 lines (discovery, pipe client, refusing proxy, tool); CDP is stable for the few domains used (`Page`, `Runtime`, `Input`, `Fetch`, `Network`, `Target`, `Browser`) | macOS/Linux: fd 3/4 pipe (`pass_fds`). Windows: Python cannot pass fd 3/4; Chrome's `--remote-debugging-io-pipes` with inherited handles is possible but untested → §7 |
| (c) MCP (e.g. a browser MCP server, separate process) | network policy lives outside Stira; MCP tools are "unclassified = external" and never answered by YOLO (ADR 0012 §4) | one config line if the person already uses one | needs Node/npx at runtime; one tool per action (many definitions, against #368) | depends on the server |
| (d) not standard: an extra tool enabled by a profile | — (orthogonal: about who turns it on) | the person must find and enable it | same code as (b) | — |
| (e) Chrome CLI only (`--headless --screenshot`, `--dump-dom`) | no way to block or report requests | screenshot only, no errors, no input | none | all |

**Recommendation: (b)**, matching how this repository already verifies UI (system Chrome,
CDP), with **no new dependency**. Registration answers (d): `page_check` is a standard tool,
**registered only when a supported browser is found and the platform is supported** (else
not registered, §7). Its rule follows the existing defaults, so nobody's tools change on
update: a **new install** gets it as **Ask** (it is registered in `WRITE_TOOLS`, which
`ToolConfig.first_run_defaults` puts in `ask_tools`); an **existing** `custom` config does not
list it, so it is **Never** until the person turns it on in the settings page or picks the
#450 development preset (§9). A `trust_all` config gets it as **Ask** (phase 1: no Always,
§6), unlike the other registered tools.

### 2. Closing the network — **recommended: a refusing proxy inside Stira, for all traffic including loopback; CDP interception for `file://` and the report**

| Option | Safety | Convenience | Cost | Platforms |
|---|---|---|---|---|
| CDP `Fetch` interception with an allow-list | **leaks** WebSocket and WebRTC (measured); misses targets it is not attached to (popups) | precise per-request report | small | all |
| OS sandbox (macOS `sandbox-exec` profile, Linux network namespace / `unshare -n`, Windows AppContainer) | strongest (kernel level) | Chrome inside a custom sandbox breaks in version-specific ways; `sandbox-exec` is deprecated; namespaces need privileges or user namespaces | three different mechanisms, each needing its own test matrix | uneven |
| **Proxy: `--proxy-server=http://127.0.0.1:<Stira's port>` + `--proxy-bypass-list=<-loopback>` + `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`** | every HTTP(S)/WS(S) request — loopback too — reaches Stira's proxy, which **refuses** (phase 1) or forwards only to the one named localhost origin (phase 3); WebRTC UDP off (measured: nothing reached the canary) | invisible to the page; same report for every channel | estimated ~80 lines (a loopback listener on an ephemeral port that reads the request line / `CONNECT host:port`, records the host, answers 403 and closes; never reads a body) | the same Chrome switches on all three |

**Recommendation:** the proxy is the guarantee; interception is only the `file://` scope
(§3) and the per-request report. "Attempts are recorded, host only": the proxy records
`host[:port]` from `CONNECT` / the absolute-form request line and discards the rest (path,
query, headers, body are never stored or returned); WebSocket hosts also come from
`Network.webSocketCreated`. The result lists at most 10 distinct hosts and the total count
(§5). Why our own listener and not a "dead" port: a port nobody listens on today can be
taken by another local program tomorrow, which would then *be* the proxy.

Launch flags kept on in every run: Chrome's own renderer sandbox (`--no-sandbox` is never
passed; running as root → not registered, §7), `--disable-background-networking`,
`--disable-component-update`, `--disable-sync`, `--disable-extensions`,
`--no-first-run`, `--no-default-browser-check`, `--headless=new`.

**Limit (written in the docs):** this closes the network for the browser Stira starts. It
does not hold a program of the same OS user (out of scope, as everywhere in Stira), nor a
Chrome exploit by the page (the page is the agent's own code; a browser 0-day is treated
like an OS compromise). DNS prefetch under a fixed proxy and WebRTC-TCP were not measured —
phase 1's contract test (§10) adds them.

### 3. What may be opened — **recommended: the workspace's `file://` (phase 1), one named `localhost` origin (phase 3); everything else refused or blocked and counted**

| Case | Handling |
|---|---|
| target argument | a workspace-relative path (or absolute inside the workspace), resolved with symlinks followed, inside the workspace **and** outside the read floor (#312); `.html` / `.htm` / `.svg` only. Phase 3: `http://localhost:<port>/…` or `http://127.0.0.1:<port>/…`. Refused before launch: `javascript:`, `data:`, `blob:`, `chrome:`, `about:`, `view-source:`, `file://` outside, any other host, the **dashboard's own port** (code `own_dashboard`; the in-process guard of ADR 0012 does not reach Chrome, a separate process) |
| sub-resources | `file://` inside the workspace: allowed (checked in `Fetch.requestPaused`, realpath + read floor); `file://` outside: failed and counted (`outside_workspace`, no path returned); `data:` / `blob:`: allowed (no network); everything else: the proxy |
| redirects | `file://` has none; a phase-3 localhost redirect elsewhere goes through the proxy → refused, counted |
| main-frame navigation away (`location = …`, form submit) | only to an allowed target; otherwise failed and counted (`navigations_blocked`) |
| iframes, workers | auto-attached (`Target.setAutoAttach`, `waitForDebuggerOnStart`, `flatten`) so the same interception applies before they run. **Implemented (review M1):** a child is resumed only after Chrome answered its `Fetch.enable` and `Target.setAutoAttach` without an error; an error or no answer within 5 s ends the run and the browser (`browser_guard_unverified`, fail closed). Measured: a dedicated worker has no `Fetch` domain (`'Fetch.enable' wasn't found`) and its requests pause in the owning frame, so a worker needs the auto-attach only |
| `window.open` / `target=_blank` | the new target is closed at once, counted (`popups_blocked`) — measured: it is a separate target the page's interception does not cover |
| downloads | `Browser.setDownloadBehavior {deny}`, counted (`downloads_blocked`) — measured |
| file upload | `Page.setInterceptFileChooserDialog`; the chooser is never answered, counted (`file_choosers`); the tool has no upload action |
| permissions (camera, mic, geolocation, notifications, clipboard) | denied (`Browser.setPermission` / no grant); the disposable profile has none |

Residual: the check of a `file://` path and Chrome's read of it are two steps; a symlink
swapped in between by a same-user program is out of scope (as the write path, ADR 0004).

### 4. Disposable profile and limits — **recommended: a fresh profile per call; small, fixed limits with reason codes**

A new `--user-data-dir` in a temporary directory per call, removed afterwards (no cookies,
logins, extensions or history carried over; nothing of the person's browser profile is
read). One browser process per call; the process group is ended on every exit path (like
`lsp_tools`). Proposed limits (constants first, a setting only if someone needs one):

| Limit | Default | Over the limit |
|---|---|---|
| wall time per call | 30 s (max 60) | `time_limit` — results so far are returned |
| actions per call | 20 | `action_limit` |
| one `wait` | 10 s | clamped, noted |
| page loads (navigations) per call | 3 | `page_limit` |
| screenshots per call | 4 (each ≤ 1280×800, PNG) | `screenshot_limit` |
| text excerpt | 4 KB (after whitespace folding) | `output_truncated` |
| error / console summaries | count + first 3, each ≤ 200 chars | `output_truncated` |
| hosts listed | 10 distinct | count stays exact |

The result always says `status` (`ok` / `truncated` / `refused` / `error`) and one
`reason` code, so "it stopped because of a limit" is never confused with "the page broke".

### 5. Tool surface — **recommended: one tool, small action set, no `evaluate`**

One tool (#368: one definition, compact description; not registered when unusable, so it
costs nothing then). Sketch:

```
page_check(
  target: str,                 # "games/hockey.html" (workspace-relative) — phase 3: "http://localhost:8080/"
  actions: list = [],          # in order; each one of:
     {"click": "<css selector>"} | {"click": [x, y]}
     {"key": "ArrowUp", "hold_ms": 300} | {"key_down": "w"} | {"key_up": "w"}
     {"move": [x, y]} | {"wait_ms": 1500}
     {"screenshot": true} | {"text": "<css selector>"}
  viewport: [width, height] = [1280, 800],
)
→ {
  "status": "ok", "reason": null, "elapsed_ms": 4120,
  "loaded": true, "title": "Hockey",
  "text": "Score 0 – 0 …",                       # untrusted page content, ≤ 4 KB
  "page_errors": {"count": 1, "first": ["TypeError: … (game.js:42)"]},
  "console_errors": {"count": 0, "first": []},
  "network": {"blocked": 2, "hosts": ["cdn.example.com"]},   # hosts only
  "blocked": {"outside_workspace": 0, "popups": 0, "downloads": 0, "file_choosers": 0, "navigations": 0},
  "steps": [{"i": 0, "ok": true, "text_changed": true}, …],
  "screenshots": [".stira-verify/20261019-2104-ab12/1.png"],   # workspace-relative
  "open_yourself": {"path": "/…/workspace/games/hockey.html", "url": "file:///…/games/hockey.html"}
}
```

* **No `evaluate`, no CDP passthrough, no cookies, no tabs.** The agent already writes the
  page's JavaScript; `evaluate` would add surface (and audit text) for little. `text` reads
  one element's `innerText`, which covers "did the score change".
* **Screenshots** are written under `<workspace>/.stira-verify/<run>/N.png` through the same
  `WritePathPolicy` check as `file_write` (a refused write → the shot is skipped and
  `reason` says so). They are **not** inlined: the model reads one with `view_image` only
  when it wants to look (zero image tokens otherwise), and `view_image` already accepts
  workspace paths (YOLO internal scope, `scope_refusal`). Only the last 5 runs are kept.
  The person can open them as files too.
* **Page content is untrusted** (like `web_fetch`): text and error messages are data, not
  instructions; the description says so.
* **Audit**: the existing tool-call path records the call as usual; the tool's own row (when
  ADR 0013 lands) holds only the target (`@workspace:` + keyed hash per ADR 0013 §1 — see
  Open (Q4)), the counts and the reason code — **never** page text, titles, error messages,
  host names or screenshots. **Implemented in phase 1 (review M2)** for the observation
  log that exists today (`payload_for_log`): the input keeps `target` as
  `@workspace/<workspace-relative path>` (or `refused:<code>`) and `wait_ms` only, the
  output the same path of the opened page with the counts and codes; never an absolute
  path, a home directory or a user name. The keyed-hash form of Q4 is for the ADR 0013
  row, which is not implemented yet.

### 6. Rule and YOLO tier — **recommended: Ask by default; Always allowed; YOLO internal while the launch guard is verified, else a card**

**Rule.** New installs: Ask (§1). Unlike exec tools (`always_allowed` false, ADR 0010 §5),
`page_check` may be set to **Always** by the person: its scope is bounded (workspace,
no network, disposable profile, time limits), the same reason `file_write` may be Always.
At WARNING and above it is hidden with the other `WRITE_TOOLS` (it writes screenshots).

**Phase 1 (review M3): Always is not offered — Q3 is deferred.** Reason: Stira has turns no
person watches (scheduled jobs, webhooks, bots); with Always the agent's own JavaScript —
possibly written under prompt injection — would run there with no card. `page_check` is
in `tool_policy.NO_ALWAYS_TOOLS` (`always_allowed` false, like exec): the settings page
offers Ask / Never, the rule API answers `always_not_allowed`, every save path writes it into
`ask_tools` when it is on, and a saved Always (or `trust_all`) is read as Ask with one
warning at start-up. Revisit when a phase can tell a person's turn from the others in the
rule itself. Cost: one card per check outside YOLO (safety: no unattended run of page code;
convenience: no one-click "always" for the build → check loop).

**YOLO.** The question is whether a call may be answered automatically in an
**internal** window. What runs: JavaScript the agent wrote — arbitrary code, but inside
Chrome's renderer sandbox (no file system, no processes), with network closed by §2 and
reads by §3. So the tier depends only on how strong the network guarantee is.

| Option | Safety | Convenience |
|---|---|---|
| (A) card in every window until the contract test covers DNS prefetch, WebRTC-TCP and Linux | strictest | the "build → check → fix" loop stops on a card every time, which is the case YOLO exists for |
| **(B) internal, when every launch is verified**: the proxy listener is up, the launch flags include the proxy, `<-loopback>` and the WebRTC policy, no `--no-sandbox`, not root, the target passed §3; any failed check → this call is a card (`browser_guard_unverified`) | the guarantee is checked per call, structurally; residual = the unmeasured channels of §2, written down | the loop runs unattended on a working setup |
| (C) internal, no per-call check | weakest | same as (B) |

**Recommendation: (B)** — in `yolo/tiers.py`: `page_check` in `INTERNAL_TOOLS`,
`_WORKSPACE_PATHS["page_check"] = "target"` for a file target (so `scope_refusal` keeps the
workspace rule), a phase-3 localhost target needs the port to be one the person allowed
(Open (Q5)), and the per-call guard result as one more refusal reason.

### 7. Platforms and discovery — **recommended: macOS and Linux in phase 1; Windows hidden with a doctor reason**

Order: `STIRA_BROWSER_PATH` (proposed; an explicit executable) → known locations →
`PATH`. Accepted: Google Chrome, Chromium, Microsoft Edge (Chromium), Chrome for Testing /
`chrome-headless-shell`; version read with `--version`, minimum checked.

| Platform | Locations | Notes |
|---|---|---|
| macOS (measured) | `/Applications/{Google Chrome,Chromium,Microsoft Edge}.app/Contents/MacOS/…`, the same under `~/Applications` | fd 3/4 pipe works |
| Linux (not measured) | `google-chrome`, `google-chrome-stable`, `chromium`, `chromium-browser`, `microsoft-edge` on `PATH` | snap Chromium has its own `/tmp` and confinement — the profile dir must be under `$HOME` or the snap is not supported (doctor says so); running as root → not registered (Chrome would need `--no-sandbox`); headless servers (EC2) usually have no browser |
| Windows (not measured) | `%ProgramFiles%`, `%ProgramFiles(x86)%`, `%LocalAppData%` for Chrome; Edge is always present | `pass_fds` does not exist; options: `--remote-debugging-io-pipes` with inherited handles (ctypes, untested) or a debugging port (any local user could attach while it runs). Phase 1: not registered, doctor reason `platform_unsupported` (as YOLO v1 is macOS-only) |

`stira doctor` adds one finding (pattern of `_classifier_check`): `page_check_ready` (info:
browser path, version), `page_check_no_browser` (warning: "install Chrome, Chromium or Edge,
or set STIRA_BROWSER_PATH"), `page_check_platform` (Windows, phase 1), `page_check_root`.
Display only; nothing changes. The settings page shows the tool row as "unavailable: see
`stira doctor`" instead of hiding it silently.

### 8. For the person — **recommended: the result carries where to open it; opening the person's browser is a person's click, not an agent tool**

The result's `open_yourself` gives the absolute path and the `file://` URL, and the
description asks the agent to end its report with them ("open `…/hockey.html` in your
browser"). The chat UI can render that as a **copy** button. Opening the person's browser
is a separate operation **started by the person**: a button on the tool card that calls a
dashboard endpoint (same-origin / CSRF checks of every dashboard POST, no agent route to
it) which runs the OS opener (`open` / `xdg-open` / `start`) for a path inside the
workspace. There is no agent tool for it. **Never** serve workspace files from the dashboard
origin for a preview: the agent's JavaScript would run on the dashboard's origin (with its
session) — that is the one thing this ADR rules out.

**Updated (#454, operator approved 2026-10-21).** ADR 0020 §6 refines this rule. The
artifacts page may preview a **registered snapshot** of an agent-written file, served with
its own `Content-Security-Policy: sandbox allow-scripts; default-src 'none'; …` and embedded
in `<iframe sandbox="allow-scripts">` (no `allow-same-origin`). The document gets a unique
opaque origin, not the dashboard's: no access to the parent's DOM, storage, session or
CSRF token, no network. Serving live workspace files on the dashboard origin stays ruled
out.

### 9. #450 presets

When #450 resumes: the **development** preset sets `page_check` to **Ask** (the person can
make it Always), the "minimal" preset leaves it Never; a preset row for an unavailable tool
says "browser not found — see `stira doctor`" and is skipped, not an error. Nothing else
of #450 depends on this ADR.

## Phases and connection points

| Phase | Scope | Where |
|---|---|---|
| 1 (minimum) | discovery; CDP pipe client; refusing proxy; `page_check` for a workspace file: load, wait, page / console error counts, blocked network count + hosts, title, text, one final screenshot; limits; §3 refusals; doctor finding; registration only when available | new `src/stiracore/tools/page_check.py`; `tools/tool_registry.py` (register in `WRITE_TOOLS` when `find_browser()` and the platform allow); `tools/compact_specs.py`; `yolo/tiers.py` (`INTERNAL_TOOLS`, `_WORKSPACE_PATHS`, guard reason); `diagnostics/doctor.py`; `docs/tool-execution-paths.md` (rows), `docs/configuration.md` / `_ja` (`STIRA_BROWSER_PATH`), `docs/tool-definitions.md`; tests below |
| 2 | actions (click / key / key_down / key_up / move / wait / screenshot / text), `steps` with `text_changed` | same module |
| 3 | one named `localhost` origin, forwarded by the proxy; dashboard port refused | same module + the proxy |
| 4 | person-side: copy / open button on the tool card; #450 preset row; Linux measured; Windows decided | `dashboard/` templates + one POST route; #450 |

**Tests (phase 1):** `tests/test_page_check.py` with fixture pages — errors / none,
network / none (counts and hosts right; a LAN-address canary as in the spike, skipped when
the host has no non-loopback address), every §3 refusal (`file://` outside, external
`http://`, `javascript:`, dashboard port), each limit's reason code, the profile removed
and the process gone afterwards, the audit row without page content; skipped when no
browser is found unless `STIRA_REQUIRE_BROWSER=1` (as `STIRA_REQUIRE_AXE`).
`tests/test_tool_path_contract.py` (#145) gets the tool; `test_every_registered_write_tool_is_classified`
and the YOLO classification test cover it; the default-rule tests check Ask on a new install
and Never on an existing `custom` file. CI (no browser required) runs the pure parts:
target validation, flag list, proxy refusal, result shaping, limits.

## Costs (safety side / convenience side)

* **Proxy instead of interception only:** + an estimated ~80 lines and one loopback listener per call;
  − nothing for the person. Interception alone was measured to leak WebSocket and WebRTC.
* **No `evaluate`:** − the agent cannot probe internal state directly; it reads text or a
  screenshot instead, or writes a visible debug line into its own page.
* **Screenshots as files, not inline:** + a write into the workspace (`.stira-verify/`,
  last 5 runs); − one extra `view_image` call when the model wants to see.
* **Windows later:** − Windows users do not get the tool in phase 1; + no untested transport.
* **Ask by default:** − one card per check outside YOLO until the person picks Always; the
  development preset (§9) is the one-click way.
* Not added on purpose (lighter alternatives were enough): an OS sandbox (§2), a second
  confirmation for Always, an allow-list of pages, a separate "verification user".

## Consequences

* Stira can close its own "write → open → see errors → fix" loop for single-page
  artifacts, and tell the person where to open the result.
* A browser becomes an optional host dependency, reported by `stira doctor`, never
  required.
* Residual risks: same-user programs and browser exploits are out of scope; the
  unmeasured channels of §2 until phase 1's contract test; the `file://` check-to-read gap
  (§3).

## Questions for the operator

1. **(Q1) Base:** (a) Playwright / (b) **system Chrome over a CDP pipe, stdlib only
   (recommended)** / (c) MCP. Recommended because it adds no dependency and matches how the
   repository already verifies UI.
2. **(Q2) YOLO tier:** (A) card until more channels are measured / **(B) internal with a
   per-call launch-guard check (recommended)** / (C) internal unconditionally.
3. **(Q3) May the person set `page_check` to Always?** **Yes (recommended)** — bounded
   scope, like `file_write` / No — Ask or Never only, like exec. **Deferred in phase 1:**
   Ask or Never only (§6), because of the turns no person watches.
4. **(Q4) Audit target:** (a) the workspace-relative path in clear (the issue's wording,
   readable) / **(b) `@workspace:` + keyed hash as ADR 0013 §1 (recommended for one audit
   contract; the readable path is in the chat's tool card)**.
5. **(Q5) Localhost (phase 3):** (a) any port except the dashboard's / **(b) only ports the
   person listed in the settings (recommended; default empty = file targets only)** — other
   local services (model servers, Jupyter) otherwise become reachable by the page.
6. **(Q6) Windows:** **hidden with a doctor reason in phase 1 (recommended)** / try the
   handle-inheriting pipe in phase 1 / use a debugging port.
7. **(Q7) Name:** `page_check` (recommended; does not collide with upstream `browser`) or
   another.

## Addendum rev 2 (2026-10-20): phase 2 as built, and the open questions of phases 3–4

**Scope taken.** The accepted decisions authorise phase 2 exactly as §5 sketches it (the eight
actions, `steps` with `text_changed`, the text excerpt of the phase-1 row, screenshots up to the
§4 limit) with no rule or tier change (Q2 narrowed: a card in every YOLO window; Q3: Ask or
Never). Phase 3 is accepted in principle (Q5 (b): only ports the person listed, default empty),
but *where* that list lives, *who* may edit it and how the proxy forwards are not decided, so it
is not built (Q11 below). Anything beyond the §5 sketch — typing a string, waiting for an
element, assertions — is proposed here, not built (Q8–Q10).

**Phase 2 as built** (`tools/page_check.py`, `tests/test_page_check_actions.py`):

* `actions` (optional, ≤ 20 run, ≤ 200 accepted for the check): each object has exactly one of
  `click` (CSS selector ≤ 300 chars, or `[x, y]` inside 1280×800), `key` (+ `hold_ms`),
  `key_down`, `key_up`, `move` (`[x, y]`), `wait_ms`, `screenshot` (`true`), `text` (CSS
  selector). All are checked **before** a browser starts; anything else → `refused` /
  `invalid_action` with the action's index. More than 20 → the first 20 run, `action_limit`.
* **No code from the agent.** Selectors are *arguments* of three fixed page functions of Stira
  (`Runtime.callFunctionOn` on `document`, `silent`); a selector built to break out of a string
  (`x'), document.title = 'pwned', ('`) is just a selector that matches nothing (tested, and a
  mutation that concatenates it into code fails the test).
* **Keys.** One printable ASCII character or a named key (Enter, Tab, Escape, Backspace,
  Delete, Space, arrows, Home, End, PageUp, PageDown, Shift); no Control / Alt / Meta and no
  `modifiers`, so no combination can reach the browser's own shortcuts. *Measured:* with the
  native key code set as well (`nativeVirtualKeyCode = 38`, macOS's code for J), ArrowUp made
  Chrome 155 open a `chrome://help/` page target — the popup guard closed and counted it; the
  tool now sends the Windows key code only (as puppeteer does) and a test sends every named key
  and asserts no popup.
* **Navigation away** is now failed with `Aborted` instead of `BlockedByClient`: the page stays
  where it was (no error page in its place), so later steps still act on the agent's page. It is
  still counted (`navigations`) and the host recorded.
* **Steps.** One entry per action: `ok`, `code` (`not_found`, `not_visible`, `bad_selector`,
  `screenshot_limit`, `screenshot_*`, or `clamped` for a `wait_ms` / `hold_ms` over 10 s),
  `text_changed` (length + FNV-1a hash of the body text before / after, computed in the page,
  so the text does not cross the pipe), `new_errors` (page + console errors during the step).
* **Text.** `text` (the body after the actions) and the `text` steps share 4,096 characters per
  call (`output_truncated`).
* **Screenshots.** ≤ 4 per call, `screenshot` steps first, then the final one when there is room
  (`<run>/1.png`, `2.png`, …); over the limit → `screenshot_limit`.
* **Page content framed as data** (as `<recalled-memory>` and `<delegate-output>`): the model
  gets two content blocks — Stira's JSON (status, codes, counts, steps, screenshot paths,
  `open_yourself`) and, when the page said anything, a fixed note plus one `<page-content>`
  block with every page-controlled string (title, text, step texts, first messages, missing file
  names, hosts), each defanged (`neutralize_child_markers`) and secret-masked.
* **Logs.** Input: `target`, `wait_ms`, actions as a count per kind (never a selector, key or
  point). Output: the phase-1 counts plus `screenshots` (a number) and `steps` (`count`,
  `failed`, `text_changed`); the `<page-content>` block is not kept.
* **Unchanged:** the rule (Ask / Never), the YOLO tier (card), the network guarantee, discovery,
  Windows / root hiding, the doctor codes (the `page_check_ready` note now says what the tool
  does).

**Open questions (each with a recommendation; nothing below is built):**

8. **(Q8) `type`: insert a string as one action** (`{"type": "hello"}`, ≤ 200 printable
   characters, `Input.insertText` into the focused element). Today a form needs one `key` per
   character (20 actions). **Recommended: yes** — same input surface as `key`, no code; the
   string is logged as a length only.
9. **(Q9) `wait_for`: wait until a selector exists / is visible, or until the body text contains
   a string**, polled by a fixed function, ≤ 10 s, `code: timeout` when it does not happen.
   Replaces guessed `wait_ms` values in the build → check loop. **Recommended: yes.**
10. **(Q10) Structured assertions** (`{"expect": {"selector": "#score", "text_contains": "1"}}`,
    also `exists`, `visible`, `text_equals`, `count`): `pass` / `fail` in the step and a count of
    failed expectations in the result, decided by Stira from fixed functions (no JavaScript from
    the model, no model-judged pass). The agent can already compare `text` itself; assertions
    make "did it work" a field, not a reading. **Recommended: yes, these five forms only.**
11. **(Q11) Phase 3, one named `http://localhost:<port>` origin.** Proposed shape: (a) the list
    lives in `tool-config.json` (`page_check.localhost_ports`, default empty), shown on the
    settings page under the tool, editable only by a person through the existing save flow
    (adding a port is a loosening → confirmation; the agent's own config API answers
    `forbidden`, as other loosening changes); the dashboard's own port and ports < 1024 refused;
    (b) per call the target's origin is the *one* forwarded origin: the proxy connects only to
    `127.0.0.1:<that port>` (never resolves a name, never IPv6 / LAN), forwards HTTP and
    WebSocket for that origin, refuses every other host and port (loopback included) and every
    redirect off the origin; (c) contract test before it is offered, as the spike: a LAN canary,
    a canary on another loopback port and an allowed port that is *not* the target's are never
    reached, the named origin is; service workers and HMR WebSockets measured. Also open: Stira
    cannot start a server itself unless exec tools are on (`STIRA_ENABLE_EXEC_TOOLS`), so in the
    default setup the person starts it. **Recommended: (a) + (b) + (c), built only after this is
    accepted.**
12. **(Q12) Phase 4.** The person-side open button (§8) and the #450 preset row stay as written;
    Linux needs one measured run (process-group end of the zygote / sandbox helpers, snap
    Chromium). Q3 (Always) and the YOLO tier (Q2 narrowed) stay as they are until a phase can
    tell a person's turn from unattended ones. **Recommended: keep deferred; measure Linux
    first.**

## Addendum rev 3 (2026-10-21): the operator's decisions on rev 2's questions, and `type` / `wait_for` / assertions as built

**Decisions (operator, 2026-10-21).** The operator answered rev 2's questions as recommended;
the operator's numbering is one higher than rev 2's (operator Q9 = rev 2 Q8, … Q13 = Q12):

* **`type` (rev 2 Q8): yes** — up to 200 characters inserted as **one** action into the focused
  element or a selector.
* **`wait_for` (rev 2 Q9): yes** — a selector or a text, up to 10 s; a longer wait is cut.
* **Assertions (rev 2 Q10): yes, exactly five forms** — `exists`, `visible`, `text_contains`,
  `text_equals`, `count` — evaluated by fixed Stira functions; **pass / fail is decided by
  Stira**; no JavaScript from the model.
* **Phase 3 (rev 2 Q11): accepted as proposed**, built **only after the canary measurement
  passes**: one named `http://localhost:<port>` origin; the allowed ports live in
  `tool-config.json` (default empty); only a person adds a port, on the settings page, with a
  confirmation (the agent's tools and APIs cannot); the dashboard's own port and ports below
  1024 refused; the proxy forwards only to `127.0.0.1:<that port>` and refuses every other host,
  port and any redirect leaving the origin. Not part of this revision (a separate change with its
  own measurement).
* **Phase 4 (rev 2 Q12): as recommended** — Always stays unavailable (Q3), the YOLO card stays
  (Q2 narrowed); Linux is measured first.

**Built** (`tools/page_check.py`, `tests/test_page_check_assertions.py`):

| Action | Shape | Result in its step |
|---|---|---|
| `type` | `{"type": "<1–200 chars>"}`, optional `"into": "<css selector>"`. No control, format, private-use or unassigned character (no newline, tab, bidi override, zero-width) — Enter and Tab stay `key` actions. With `into`: the element is scrolled into view and focused by a fixed function; without: the focused element. Only an editable element is typed into (an `input` of type text / search / url / tel / email / password / number, a `textarea`, a `contenteditable`; not read-only, not disabled). The string goes as the parameter of `Input.insertText` (one `input` event, no key events) | `ok`, or `code`: `not_found`, `not_editable`, `not_focused` (e.g. a hidden field), `bad_selector` — nothing is inserted then |
| `wait_for` | `{"wait_for": {"selector": "<css>"}}` (exists) or `{"selector": …, "visible": true}`, or `{"wait_for": {"text": "<1–200 chars>"}}` (the body's text contains it, whitespace folded); optional `"timeout_ms"` (default 5,000) | polled every 100 ms by a fixed function; `waited_ms`; `ok`, or `code: timeout`; over 10 s → cut to 10 s, `clamped: true` (and `code: clamped` when it was met); a selector that never parses → `bad_selector`; the call's 30 s limit still ends everything (`time_limit`) |
| `expect` | `{"expect": {"selector": "<css>", <one form>}}` with `exists: bool`, `visible: bool`, `text_contains: "<1–200>"`, `text_equals: "<0–200>"`, `count: 0–10000` | `ok` = passed, `form`, `code` when failed: `mismatch`, `not_found` (a text form and no element), `bad_selector` (also when the page did not answer: never a pass); `count` (the number found) for the count form |

* **Who decides.** One fixed page function (`_FN_PROBE`, `Runtime.callFunctionOn` with the
  selector as an *argument*, `silent`) returns facts of the first match — how many elements
  match, whether the first is visible, and (text forms only) up to 100,000 characters of its
  text. Stira compares in Python (`expect_outcome`, `wait_for_met`): texts after whitespace
  folding, case-sensitive; `text_contains` looks in those first 100,000 characters,
  `text_equals` compares all of it; malformed facts never pass. A breakout selector that is
  valid CSS (`[data-x="'), document.title = 'pwned', ('"]`) and a typed string of the same kind
  stay data (tested).
* **Visible** means: the element has a non-empty box (`display: none` on it or an ancestor
  gives none) that is not wholly left of or above the page, computed
  `visibility: visible`, no ancestor (itself included) with opacity 0, and Chrome's
  `checkVisibility` (`content-visibility: hidden`) where it exists (Stira's own rules are
  tested with it removed as well). Below the fold is visible
  (the page can scroll there). **Not checked:** covered by another element, clipped by an
  `overflow` ancestor. `visible: false` passes when nothing matches.
* **Limits.** Assertions and the new actions are entries of `actions`: they **count toward the
  20-action limit** (one list, one order, one cap; no separate assertion cap — a call that
  needs more checks than that is two calls). Over 20 → the first 20 run, `action_limit`, and
  `assertions.not_run` counts the assertions that were dropped (or not reached at the time
  limit). The waits keep the 10 s cut, the call 30 s, screenshots 4, page text 4,096
  characters — no new page text: the facts' text stays in Stira and is never returned.
  Everything is checked **before** a browser starts (`refused` / `invalid_action` with the
  index).
* **Result.** `assertions: {passed, failed, not_run, results: [{i, form, pass}]}` in Stira's
  block when the call had assertions. A failed assertion does not change `status` or
  `reason` (the page answered; it is the check's result, not a limit). The page's strings stay
  in the `<page-content>` block only.
* **Logs / audit.** Input: the actions as a count per kind (`type`, `wait_for`, `expect` are
  kinds) — never the typed text, a selector, an expected text, a count or a timeout. Output:
  `assertions` as `passed` / `failed` / `not_run` counts only (no per-assertion row, no form).
* **Tool definition** (#368): 1,457 → 1,855 characters of JSON spec (description 1,042 →
  1,394).
* **Unchanged:** rule (Ask / Never), YOLO tier (a card), the network guarantee, discovery,
  doctor codes (the `page_check_ready` note now says "runs a few input actions and checks").

**Linux (phase 4):** not measured. The reachable Linux host (Ubuntu 24.04, aarch64) has no
Chromium-family browser (Firefox only; Google Chrome has no Linux arm64 build, Chromium would be
a snap or a distribution package to install) — so nothing about Linux changed.

## Addendum rev 4 (2026-10-21): phase 3 — the canary measurement, then one named localhost origin as built

**Measured first** (throwaway script, not committed; macOS 26.5, Chrome 155.0.8059.85
headless, the proxy prototype that is now `RefusingProxy(forward_port=…)`). The page was
served from `http://localhost:P/` by a test server on `127.0.0.1:P`. Its port P and a
second port R were in the allowed list; only P was the target. The page tried:

* `fetch`, `<img>`, `<iframe>`, `<link rel=prefetch>`, `sendBeacon` and `EventSource`
  towards three canaries: an **HTTP canary on this host's LAN address**, a **canary on
  another loopback port Q**, and the **listed-but-not-target port R** (also by
  `127.0.0.1`);
* a WebRTC STUN request to a **UDP canary on the LAN address**;
* `WebSocket` to the origin, the LAN canary, Q and R;
* `fetch` to `127.0.0.1:P`, `foo.localhost:P`, `[::1]:P` and `https://localhost:P`;
* same-origin `fetch`;
* redirects from P to the LAN canary, Q, R and `127.0.0.1:P` (followed by `fetch`, by an
  iframe and as the main frame);
* a JavaScript navigation to the LAN canary, a form POST to it, `window.open`;
* a service worker whose install fetches all three canaries.

| Run | LAN TCP | LAN UDP | loopback Q | listed R | origin P | result |
|---|---|---|---|---|---|---|
| guarded, 4 runs | **0** | **0** | **0** | **0** | 16 requests, 1 WebSocket upgrade (13 forwarded, 49–50 refused by the proxy) | same-origin `fetch` → `same-ok`; same-origin WebSocket → message received; service worker registered; every other attempt `err` |
| proxy flags removed (baseline) | 13 | 4 | 10 | 9 | 20 | the canaries see the page: the measurement can detect a leak |

Main-frame redirects off the origin (to the LAN canary, to Q, to `127.0.0.1:P`): aborted and
counted in `navigations` (`loaded: false`); the canaries saw nothing. Target refusals
measured with the same build: `localhost:Q` (not listed) `port_not_allowed`; `127.0.0.1:P`,
`foo.localhost:P`, `[::1]:P`, `u@localhost:P` `host_refused`; `https://localhost:P`
`scheme_refused`; `localhost:80` `port_refused`. **The criteria held, so phase 3 was built.**
Chrome sends a WebSocket through the proxy as `CONNECT localhost:P` (measured); the proxy
tunnels that one `CONNECT` and no other.

**Built** (`tools/page_check.py`, `tools/tool_config.py`, `observability/page_check_ports_api.py`,
the settings page; `tests/test_page_check_phase3.py`):

* **Target.** `http://localhost:<port>/…` only: the host name `localhost`, an explicit port
  1024–65535 in one spelling, a port listed by a person, and not the dashboard's own port.
  The dashboard's port is taken from the armed own-dashboard guard, `STIRA_PORT` and, on the
  settings route, the port the request came in on. Refusal codes: `scheme_refused` (`https`,
  `ws`), `host_refused` (`127.0.0.1`, `[::1]`, `*.localhost`, user info, any other host),
  `port_refused`, `own_dashboard`, `port_not_allowed`. The path, query and fragment are
  passed to Chrome.
* **Proxy.** `forward_target`:
  * an absolute-form `http://localhost:<port>/…` request becomes an origin-form request to
    the literal address `127.0.0.1:<port>`. No name is ever resolved, and there is no IPv6.
    `Host` is set to `localhost:<port>`, `Connection: close` (or `Upgrade`), and the
    `Proxy-*`, `Keep-Alive` and `Host` headers of the page are dropped. One request per
    connection;
  * `CONNECT localhost:<port>` is tunnelled to the same address;
  * everything else is refused (403) and counted as before.

  The page's own `Fetch` guard passes requests for the origin, so the page's own document
  loads count toward the 3-page limit. A request for another host or port still goes to the
  proxy and is refused; a document request for one is aborted (`navigations`). A redirect
  that leaves the origin is the browser's next request, so it is refused the same way. A
  dead origin port answers 502 (`forward_failed`), never a fallback. The upstream connect
  happens inside the server process, so the own-dashboard connect guard (#495) also refuses
  the dashboard's port there.
* **The list** is `tool-config.json` `page_check_ports` (default absent, meaning none; at most
  8). It is part of the checked rules: written to the file, the head digest and snapshot, and
  the anchor. A file without the key is byte-for-byte unchanged, and so is its digest. It is
  *not* in `ToolConfig.as_dict()` (what the tool APIs, the hub and the history show). Every
  other save path keeps the ports the head recorded. A recovery save (over locked rules)
  drops them. A hand edit that adds a port locks the rules (`edited`), as does an interrupted
  next generation that adds one (`unconfirmed_generation`). An added port counts as
  loosening (`rules_loosening`); a removed one does not.
* **Who can add a port.** Only `POST /api/tools/page-check-ports`, behind the dashboard's
  CSRF / Origin / Host checks. Adding needs:
  * `confirm: true` and the added port(s) typed (`confirm_ports`). The page asks with the
    shared dialog: the port is typed, Cancel is the default;
  * the breaker at NORMAL (403 `breaker_active`);
  * no agent tool call running (409 `tool_call_running`, with YOLO);
  * with the YOLO allowance, the host confirmation (428).

  Each change writes one settings-history row (`source: api:page_check_ports`, group
  `page_check`), which a rollback cannot restore. Removing needs none of this. Nothing else
  can add a port, and each is tested:
  * `page_check` has no argument for it: extra `local_ports` / `ports` / `page_check_ports`
    / `allowed_ports` inputs are ignored, and the server reads the list from the rules at
    every call;
  * `/api/tools/config` and `/api/config/tools` refuse the field;
  * a hub save carrying it changes nothing;
  * file tools cannot write the data directory (the floor, #271);
  * the server's own tools cannot reach the dashboard (#495).
* **Audit.** The target is logged as `@localhost:<port>/<path>`: the path in clear, as the
  workspace path is, but never the query, the fragment or user info. `network.forwarded`
  (a count) is the only new output field.
* **Docs** say that without exec tools (`STIRA_ENABLE_EXEC_TOOLS`) the person starts the
  server, and that it should listen on `127.0.0.1` (the proxy never connects to `::1`).
* Unchanged: the rule (Ask / Never), the YOLO tier (a card), workspace file targets.

**Not measured / not covered:** Linux (no Chromium-family browser on the reachable Linux
host), HMR WebSockets of real dev servers (Vite / webpack — the same `CONNECT` path as the
measured WebSocket), a dev server that listens only on `::1` (it gets 502: start it on
`127.0.0.1`), HTTP/2 (Chrome speaks HTTP/1.1 to a plain-HTTP proxy), and a service worker
of the origin that outlives one call (impossible: every call has a fresh profile).
