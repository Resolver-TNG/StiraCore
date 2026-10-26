# Procedural-memory seeds (#463)

Samples an operator may **propose** into the procedural layer. Nothing here is loaded on
install, start or update, and nothing is approved for you: a seed becomes a proposal under
`procedural/staging/`, and only a person turns it into a rule — in the memory browser
(`/memory`, "Procedural memory proposals") or with `procedural_cli approve`.

| Seed | State | Why |
|---|---|---|
| `safe_artifact_editing.md` | offered | Every step uses a tool the harness has today: `file_read` (a missing file reads as `path_not_found`), `editor` `view` / `create` (never overwrites) / `str_replace` (unique old text) / `insert`, `file_write`; a refused write (outside the write roots, a protected path, a declined Ask card) is reported, not worked around. Checked by `tests/test_procedural_seeds.py`. |
| `verify_before_done.md` | offered (sample; needs `run_checks` turned on) | "Test before reporting done" (ADR 0019 §7). Names only tools the harness has: `run_checks` (ADR 0019, registered only when gateway `run_checks` is `{"phase": 1}`; the seed says what to do when it is not offered), `lsp_diagnostics`, `page_check` (when a browser is found). Never approved on install: load it as a proposal like the one above (`--source-turn seed:verify_before_done`) and approve it on the review panel. Checked by `tests/test_procedural_seeds.py`. |
| `untrusted_ai_containment` | **held, not shipped** | It describes trust labels, taint, a tool broker and isolation modes. Those do not exist yet (#458 trust labels and taint, #460 isolation stages); a rule that tells the agent to rely on them would describe abilities it does not have. Revisit when they are implemented. |

## Load a seed as a proposal

From the repository root, with the server's data directory (the same `--base-dir` as the
other memory CLIs):

```sh
python -m stiracore.control.procedural_cli --base-dir <DATA_DIR>/memory \
    propose safe_artifact_editing --source-turn seed:safe_artifact_editing \
    --file examples/procedural-seeds/safe_artifact_editing.md
```

It appears in the review panel of `/memory` (proposed by the operator). Read it, edit the
file and propose again if your deployment differs, then approve or reject it there
(approving needs the breaker at NORMAL; rejecting works at any level). An
approved rule is in "Procedural Rules" from the next turn (the review screen refreshes the
static prompt between turns); only the newest five approved rules are read.
