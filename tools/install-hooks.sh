#!/bin/sh
# Install the shared pre-commit hook for the PUBLIC CANScope repo.
# Run once after cloning:  tools/install-hooks.sh
#
# Uses core.hooksPath so the hook stays tracked in-repo (tools/hooks/) instead
# of being copied into the untracked .git/hooks/ directory.

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

git config core.hooksPath tools/hooks
chmod +x tools/hooks/pre-commit 2>/dev/null || true

echo "Installed: core.hooksPath -> tools/hooks"
echo "The pre-commit hook now blocks diagnostics-path commits and runs the test suite."
