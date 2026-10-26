# ADR 0004: `editor` = SDK `file_editor` behind Stira's write sandbox

**Path:** `docs/adr/0004-editor-file-editor-sandbox.md`
**Status:** Accepted (#245)
**Decision:** Keep the tool name `editor`. Back it with `strands.vended_tools.file_editor`, bound to `StiraWriteSandbox`, which enforces `WritePathPolicy` on every write.

## Context

The upstream `strands_tools.editor` (strands-agents-tools 0.8.9) is deprecated and will be removed in 0.9.0. We measured three defects in it (reproduced in `tests/test_stira_editor.py`):

- It edits a per-process cached copy of the file. A file changed externally is overwritten by the next `str_replace`.
- A relative `create` fails, because it calls `os.makedirs('')`.
- `{path}.bak` is created with `shutil.copy2`, which also copies the `uchg` flag. A retry keeps failing after the original file is unlocked.

The SDK's replacement is `file_editor`, which routes all I/O through a `Sandbox`. The `file_editor` and `sandbox` sources are byte-identical in strands-agents 1.57.1 (`.venv`) and 1.59.0 (`.venv-jev`).

## Options

1. **Register `file_editor` under its own name and drop `editor`.**
   - Every name-based policy would silently stop applying to it: `blocked_tool_names`, the IdentityGuard heuristic, settings toggles, eval stubs, saved configs that disable `editor`. That loosens the boundary unless every list gets an alias.
2. **Keep the upstream `editor` and add stopgaps.** Possible stopgaps: an mtime check before using the cache, `EDITOR_DISABLE_BACKUP`, and making relative paths absolute.
   - This depends on a module that is going away.
   - It patches the cache from outside.
   - The backup environment variable is process-wide.
3. **Keep the name `editor` and swap in `file_editor` with Stira's sandbox (chosen).**

## Decision (option 3)

- `tool_registry` registers `make_file_editor(sandbox=StiraWriteSandbox(), name="editor")` at the same position as before. Tool order is part of the cached prompt prefix (#254).
  - The tool spec changes once: commands are `view`/`create`/`str_replace`/`insert`.
  - `pattern_replace`, `find_line` and `undo_edit` are gone.
  - `create` never overwrites, and `str_replace` requires a unique match.
- `IdentityProtectedTool` stays the policy boundary: it runs the gate mark check (#242), runs the `WritePathPolicy` check (#243), and anchors relative paths at the workspace.
- `StiraWriteSandbox` adds defence in depth:
  - It writes only inside an active grant for `editor`, and only to the exact path the wrapper checked.
  - Just before writing, it re-checks every wrapper's policy, outermost first.
  - It opens the file with `O_NOFOLLOW`.
  - It refuses command and code execution, and removal.
- `editor` no longer has a terminal prompt. It drops out of `CONSENT_TOOL_MODULES` (the consent bridge) but stays in `GATED_TOOLS` (gate mark required).
- On Windows, `file_editor` accepts only POSIX-absolute paths. `C:\x` is passed as `/C:/x` and mapped back inside the sandbox.

## SDK version alignment

We ran the full suite with strands-agents 1.57.1 (`.venv`) and with 1.59.0 (a throwaway venv using the same freeze, only strands-agents changed). Both runs gave the same result: 1717 passed, 38 skipped, 4 xfailed.

The code needs nothing beyond `>=1.57.1`, so `pyproject.toml` is unchanged. Aligning `.venv` to 1.59.0 is an environment step. It should not happen while other workers share `.venv`.

## Consequences

- Reads (`view`) are not restricted, the same as the upstream editor and `file_read`. This is not an isolation boundary for reads.
- TOCTOU is narrowed (write-time re-check, no final-symlink follow) but not eliminated. A swapped parent directory is still out of scope (#167).
