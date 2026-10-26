# STIRA / Neural State Observer

A local-only Flask observation console using the real `NeuralState` and `StiraEngine`. No CDN, cloud calls, or new dependencies.

```sh
cd ~/projects/StiraCore
PYTHONPATH="$HOME/projects/StiraCore/src" /usr/local/bin/python3 dashboard/app.py
```

Open **http://localhost:5555**. The development server binds only to `127.0.0.1` (not intended for production exposure).

## Data and semantics

- Default source: `/tmp/stiracore_poc_state.json`; override with `STIRA_STATE_PATH` for isolated testing.
- GET `/api/state`: complete core state plus read-only derived breaker/threshold/observer metadata. Polling every two seconds does **not** mutate decay or the coherence window.
- GET `/api/history`: last ten recorded dashboard stimuli, oldest first; UI renders newest first.
- POST `/api/stimulus`: JSON `{"stimulus":"joy","intensity":0.8}`. Validates known core stimuli and finite numeric intensity 0–1. Runs actual pre/post hooks and atomically saves state. These are **manual operator events**, not Bedrock agent invocations. Recording a recovery stimulus remains possible at CRITICAL; this does not authorize an autonomous turn.
- Core breaker is evaluated read-only through `_check_breaker()` because the current core has no public non-mutating status accessor. There are no core modifications.
- The existing PoC persisted **no historical turns and an empty transcription**. Those sections intentionally start empty; neither is fabricated. Dashboard does not generate emotional narration.
- History is an optional `dashboard_history` JSON extension in the same atomic save; `NeuralState.load()` ignores this extension. Unknown existing fields are preserved by dashboard mutations. Another program calling core `save()` replaces the file and removes extensions, including this log. This is not a shared transactional multi-writer database; avoid simultaneous external writes and manual stimuli.
- External file changes are reloaded on the next request. Invalid/deleted state returns 503, and the UI removes the GO signal and marks displayed readings stale. Same-origin mutation checks and local Host validation are enabled.
- Identity anchors are fixed, read-only demo labels, not a parser or editor of SOUL.md. The display reports the engine's safety classification, not a guarantee about arbitrary external agent behavior.

## UI shell, navigation and language (#183 / #184)

Pages extend `templates/base.html` (sidebar, attention chips, language selector) and use
`static/ui/tokens.css` + `components.css`; page CSS uses tokens only (no colour literals,
no inline `style`/`<script>`; palette, contrast table and how to swap it:
[docs/ui-theme.md](../docs/ui-theme.md)). Each server declares its own sidebar with
`stiracore.observability.shell.install_shell()`, so a server never links to a page it
does not serve. Status chips poll the server's own endpoints (`data-status-url`); the
approvals chip is shown only where a HITL broker exists.

Page audit (`tests/test_ui_observer_pages.py` requests every sidebar link of both servers):

| URL | `dashboard/app.py` (observer) | `dashboard/stira_server.py` | Template |
|---|---|---|---|
| `/` | State (sidebar) | redirect → `/chat` | `index.html` |
| `/chat` | sidebar only with a chat agent; otherwise redirect → `/` | sidebar | `chat.html` |
| `/observability` | sidebar | sidebar | `observability.html` |
| `/settings` | sidebar | sidebar | `settings.html` (settings hub, #227: model, classifier, breaker, tool permissions in one draft with a save/discard/defaults footer; `/api/config/hub`) |
| `/tools` | redirect → `/settings#sec-tools` | redirect → `/settings#sec-tools` | — (one tool list only) |
| `/memory` | — (no memory store) | sidebar (read-only tree, `/api/memory/tree`) | `forest.html` |
| — | not routed | not routed | `memory.html`, `artifacts.html` (on the shell; their APIs `memory_api` / `artifact_api` are not registered by either server) |

Language: Japanese by default; the selector (or `?lang=en|ja`) is stored in
`localStorage` and sets `<html lang>`. UI text lives in `static/i18n/catalog.{ja,en}.json`
(`data-i18n` / `t("key", {vars})`); missing keys fall back to Japanese with a console
warning. `python scripts/i18n_extract.py --apply|--check` converts/validates bilingual
literals. **Neural State dimension names are translated** in the UI (信頼 / 好奇心 / 警戒 /
愛着 / 安定 / 満足, keys `dim.*`; stimuli `stimulus.*`); the API/JSON identifiers
(`trust`, `stability`, …) stay English and are never translated. Breaker levels shown as
signals (`WARNING` / `CRITICAL` lines in charts) keep their English names as fixed labels.

## Verify

```sh
PYTHONPATH="$HOME/projects/StiraCore/src" /usr/local/bin/python3 -m unittest discover -s dashboard -p 'test_dashboard.py' -v
node --check dashboard/static/app.js
```

Tests use temporary files and never mutate the live PoC state. Coverage: rendered HTML/static assets, observation-only reads, validation, origin/host checks, real stimuli, last-ten/restart persistence, compatibility with core loading, external refresh, exact breaker thresholds, and invalid-file failure.

## Running demo handoff

Launched as a detached child (not an installed service). Log: `/tmp/stira-dashboard.log`; PID file: `/tmp/stira-dashboard.pid`. The server is left running until its operator stops it or the host session terminates.

Live HTTP checks returned 200 for `/`, both static assets, `/api/state`, and `/api/history`. Browser screenshot was unavailable: OpenClaw browser policy blocks localhost navigation. No browser-policy workaround was attempted.
