"""
Assert that APP_VERSION, the topmost CHANGELOG.md entry, and (optionally) the
release tag all agree.

Run this before the dependency install and PyInstaller build in build.yml so
a version mismatch fails the release in seconds, instead of after Windows
build minutes have been spent.

Usage:
    python tools/check_version_consistency.py                # internal check only
    python tools/check_version_consistency.py v00.00.54       # also check against a tag

Version format: vXX.YY.ZZ (e.g. v00.00.54).

Exit codes:
    0 — all sources agree
    1 — sources disagree, or the tag argument doesn't match the version format
    2 — a source file is missing or its version could not be parsed
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"^v\d{2}\.\d{2}\.\d{2}$")

APP_PY = Path("app.py")
CHANGELOG_MD = Path("CHANGELOG.md")

APP_VERSION_RE = re.compile(r'^APP_VERSION\s*=\s*"(v\d{2}\.\d{2}\.\d{2})"', re.MULTILINE)
CHANGELOG_HEADING_RE = re.compile(r'^##\s*\[(v\d{2}\.\d{2}\.\d{2})\]', re.MULTILINE)


def read_app_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = APP_VERSION_RE.search(text)
    if not match:
        print(f"error: could not find an APP_VERSION = \"vXX.YY.ZZ\" line in {path}", file=sys.stderr)
        raise SystemExit(2)
    return match.group(1)


def read_changelog_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = CHANGELOG_HEADING_RE.search(text)
    if not match:
        print(f"error: could not find a '## [vXX.YY.ZZ]' heading in {path}", file=sys.stderr)
        raise SystemExit(2)
    return match.group(1)


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(__doc__, file=sys.stderr)
        return 2

    tag = argv[1] if len(argv) == 2 else None
    if tag is not None and not VERSION_RE.match(tag):
        print(f"error: tag argument '{tag}' does not match the vXX.YY.ZZ format", file=sys.stderr)
        return 1

    if not APP_PY.exists():
        print(f"error: {APP_PY} not found", file=sys.stderr)
        return 2
    if not CHANGELOG_MD.exists():
        print(f"error: {CHANGELOG_MD} not found", file=sys.stderr)
        return 2

    app_version = read_app_version(APP_PY)
    changelog_version = read_changelog_version(CHANGELOG_MD)

    sources = [
        (f"APP_VERSION in {APP_PY}", app_version),
        (f"topmost heading in {CHANGELOG_MD}", changelog_version),
    ]
    if tag is not None:
        sources.append(("tag argument", tag))

    if len({value for _, value in sources}) == 1:
        print(f"OK — all sources agree: {app_version}")
        return 0

    print("Version mismatch:\n", file=sys.stderr)
    for label, value in sources:
        print(f"  {label}: {value}", file=sys.stderr)
    print(file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
