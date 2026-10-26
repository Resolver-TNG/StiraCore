# Stira Neural State Observer

## Requirements
Local-only Flask product demo on port 5555. Show six live dimensions, stability zones, actual engine breaker, coherence threshold, five Japanese protected identity anchors, exact emotion transcription, and last ten observed dashboard stimuli. No packages or core changes. Read the existing PoC state; never fabricate historical turns or emotional narration.

## Design
`app.py`: real NeuralState/StiraEngine; lock-protected state, external file refresh, atomic persistence of state plus optional dashboard_history extension. GET is observation-only (no decay/window mutation). POST validates known stimuli and finite intensity 0–1, applies actual pre/post hooks on a copy, then persists. Loopback binding and same-origin write protection. `templates/index.html`, `static/style.css`, `static/app.js`: no CDN; two-second polling with stale/error handling, responsive tactical console. Manual stimuli are explicitly simulation/state mutations, not model invocations or an authorization to act.

## Tasks
- [x] Backend/API + validation/persistence
- [x] Responsive UI, safety and empty/error states
- [x] Isolated endpoint tests; HTTP HTML/static delivery verification (browser policy blocks localhost render/screenshot)
- [x] Background server on 5555, handoff URL and evidence

## Notes
PoC saves only a final NeuralState, not its turn log; old turns cannot be reconstructed. Its current emotion_transcription is empty. Show honest empty states.

## Verify結果
- Five unittest cases passed; JS syntax check passed.
- Live HTTP 200: /, /static/style.css, /static/app.js, /api/state, /api/history. All five Japanese anchors present in HTML.
- Actual live state: stability 0.90, bc_score 1.00, normal / GO. PoC file not mutated during verification.
- Detached PID 53077 listens on 127.0.0.1:5555. Log /tmp/stira-dashboard.log; PID file /tmp/stira-dashboard.pid.
- Browser tool refused localhost navigation by policy. No screenshot or rendered-layout assertion; report live HTTP output instead, as permitted by the assignment.
- No package installs, core edits, repository commits, or pushes. Existing examples/strands_poc.py left untouched.
