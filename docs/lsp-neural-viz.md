# Python analysis and Neural State visualization

## Requirements and design

- Optional Python diagnostics prefer Pyright type analysis and fall back to Ruff lint.
  Results explicitly identify the backend/capability; skipped is not equivalent to clean.
  Both tools use bounded Python input and subprocess timeouts, never shell invocation or fixes.
  They analyse the bytes read through the read floor, not the file name (#315): Ruff on
  stdin (`--stdin-filename`), Pyright through a one-shot `pyright-langserver --stdio`
  session (open document + pull diagnostics; the CLI alone is skipped). Install the
  `pyright` package (it ships both commands).
- Hover positions use 1-based Unicode character columns. Optional Jedi supplies inference;
  dependency-free AST fallback only lists matching local function/class definitions (not
  scope resolution or imported types). Source code is never executed.
- `lsp_diagnostics` and `lsp_hover` are deliberately WRITE-classified for breaker filtering.
- `/observability` adds dependency-free SVG time series and accessible heatmap tables.
  No CDN/network dependency or CSP relaxation is needed. Existing panels remain intact.
- `GET /api/state/history?limit=100` accepts 1–500 and returns oldest-first history,
  six dimensions, live thresholds, actual Emotion Matrix coefficients and a 24h projection.
  Projection uses copies and the real linear baseline-clamped core decay, not an exponential
  approximation. Reads never advance turns, decay, cooldown, or persistence.
- Observer stimulus snapshots persist up to 500 samples; legacy `/api/history` still returns
  ten. Pre-feature rows without six-dimensional snapshots are omitted, not reconstructed.
- Live gateway records at startup and after each invocation, even without browser polling.
  Its 500-sample buffer lasts for the process lifetime. Event dots indicate a changed
  `last_stimulus_time`; the classifier is not called twice and an unknown category is not
  invented. Both successful and unsuccessful completed invocations record resulting state.
- Correlations are observed Pearson coefficients, distinct from the causal stimulus matrix.
  Insufficient/constant data display an em dash. Failure leaves charts explicitly stale.

## Tasks and verification

- [x] Structured diagnostics, fallback and AST/Jedi hover; registry integration.
- [x] Shared bounded history API; observer and live gateway wiring.
- [x] SVG history, decay, stimulus dots and stability thresholds; both heatmaps.
- [x] Python regression tests and Node correlation tests.

Run `python -m pytest tests/ -x`, `ruff check .`, `ruff format --check .`,
and `node --test tests/neural_viz_test.cjs`. Optional real Ruff test skips only if
Ruff is unavailable. Pyright/Jedi behavior is covered offline using controlled inputs.
