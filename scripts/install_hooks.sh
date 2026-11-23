#!/usr/bin/env bash
# Turn on the repository's git hooks (.githooks/): a pre-push that runs the cheap checks of CI.
set -eu
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath .githooks
chmod +x .githooks/* scripts/prepush_check.sh
echo "hooks enabled: core.hooksPath=.githooks  (undo: git config --unset core.hooksPath)"
