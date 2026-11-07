#!/usr/bin/env bash
# Give every tracked file the mode git records (644, or 755 where the index says 100755).
# A NAS file system with inherited ACLs makes everything executable, and ruff's EXE002 then fails
# ("executable but no shebang"); the same check also guards against real mode drift. Run in the checkout.
# NUL-separated so unusual file names (spaces, non-ASCII) are handled; a file that cannot be changed is skipped.
set -u
git ls-files -s -z | while IFS= read -r -d '' rec; do
  meta="${rec%%$'\t'*}"; path="${rec#*$'\t'}"
  case "$meta" in 100755*) chmod 755 -- "$path" 2>/dev/null ;; 1006*) chmod 644 -- "$path" 2>/dev/null ;; esac
done
exit 0
