"""
Install the pre-commit hook that runs the test suite before every commit.

Run once after cloning:
    python tests/install_hooks.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_SRC = Path(__file__).parent / "hooks" / "pre-commit"
HOOKS_DST = REPO_ROOT / ".git" / "hooks" / "pre-commit"


def main() -> None:
    if not (REPO_ROOT / ".git").is_dir():
        print("Error: not a git repository.", file=sys.stderr)
        sys.exit(1)

    HOOKS_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HOOKS_SRC, HOOKS_DST)
    HOOKS_DST.chmod(0o755)
    print(f"Installed pre-commit hook: {HOOKS_DST}")


if __name__ == "__main__":
    main()
