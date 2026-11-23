#!/usr/bin/env bash
# Checks that must be green before a push (the cheap half of CI; the full Linux gate is ci/linux/gate.sh).
#   scripts/prepush_check.sh [--dry-run]       changed files = diff of HEAD against origin/main
# PREPUSH_FILES="a\nb" overrides the changed-file list (tests). STIRA_PREPUSH_STRICT=1 makes a missing tool a failure.
# Bypass once with `git push --no-verify`.
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
if [ -n "${PREPUSH_FILES+x}" ]; then files="$PREPUSH_FILES"
else base="$(git merge-base HEAD origin/main 2>/dev/null || git rev-parse HEAD~1)"; files="$(git diff --name-only "$base" HEAD)"; fi
has() { printf '%s\n' "$files" | grep -Eq "$1"; }
ruff_bin=""; common="$(cd "$(git rev-parse --git-common-dir)/.." 2>/dev/null && pwd)"   # the main checkout, when this is a worktree
for c in "$(command -v ruff || true)" .venv/bin/ruff "$common/.venv/bin/ruff"; do [ -n "$c" ] && [ -x "$c" ] && { ruff_bin="$c"; break; }; done
plan=()
if has '\.py$|^pyproject\.toml$'; then
  if [ -n "$ruff_bin" ]; then plan+=("$ruff_bin check ." "$ruff_bin format --check ."); else
    echo "prepush: ruff not found (install '.[dev]' or use .venv)" >&2; [ "${STIRA_PREPUSH_STRICT:-0}" = 1 ] && exit 1; fi
fi
if has '^dashboard/stage/'; then
  plan+=("node --test tests/stage_gadgets_test.cjs tests/stage_art_lint_test.cjs tests/stage_model_test.cjs")
fi
if has '^dashboard/stage/tools/(build_gadgets|stack_[a-z_]+|sprite_stack)\.py$'; then
  plan+=("python3 dashboard/stage/tools/build_gadgets.py && git diff --exit-code -- dashboard/stage/assets/gadgets")
fi
changed_cjs="$(printf '%s\n' "$files" | grep -E '^tests/.*\.cjs$' | while read -r f; do [ -f "$f" ] && printf '%s ' "$f"; done)"
[ -n "$changed_cjs" ] && plan+=("node --test $changed_cjs")
if [ ${#plan[@]} -eq 0 ]; then echo "prepush: nothing to check for these files"; exit 0; fi
rc=0
for cmd in "${plan[@]}"; do
  echo "prepush: $cmd"
  [ "$DRY" = 1 ] && continue
  bash -c "$cmd" || { echo "prepush: FAILED: $cmd" >&2; rc=1; }
done
exit $rc
