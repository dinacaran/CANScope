"""
Fail if a change touches files the project owner has declared protected.

The lists below are transcribed from CLAUDE.md. They exist so the rule is
enforced by CI rather than depending on a contributor (human or AI agent)
having read the doc.

Usage:
    python tools/check_protected_paths.py                  # vs origin/main
    python tools/check_protected_paths.py <base> <head>    # explicit revs (CI)

Exit codes:
    0 — no protected file touched
    1 — protected file touched
    2 — could not determine the diff (git failure, bad revs)

Override: the owner applies the `approved-pipeline-change` label to the PR,
which makes the CI guard job skip this check entirely.
"""
from __future__ import annotations

import subprocess
import sys

# Validated loading/decoding pipeline — "Do not modify, refactor, replace,
# optimize, or otherwise change this pipeline without explicit permission
# from the project owner."
PROTECTED_PIPELINE: tuple[str, ...] = (
    "core/load_worker.py",
    "core/channel_config.py",
    "core/dbc_decoder.py",
    "core/vectorized_decoder.py",
    "core/raw_frame_store.py",
    "core/blf_reader.py",
    "core/readers/",           # whole package: base, factory, all readers
    "requirements.txt",        # loading/decoding dependency versions
)

# "What NOT to Modify Without Instruction" — plus owner-only release metadata.
PROTECTED_OTHER: tuple[str, ...] = (
    "core/signal_store.py",    # data source for the whole app
    "CANScope.spec",           # PyInstaller build spec
)

# Owner writes these at release time; contributors must never touch them.
# Keeps concurrent PRs from colliding on the same two lines.
OWNER_ONLY: tuple[str, ...] = (
    "CHANGELOG.md",
    "app.py",                  # guards APP_NAME / APP_VERSION
)

GROUPS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "Validated loading/decoding pipeline",
        PROTECTED_PIPELINE,
        "Changing this pipeline requires explicit permission from the project "
        "owner, even for cleanups, performance work, or bug fixes.",
    ),
    (
        "Protected core files",
        PROTECTED_OTHER,
        "These are listed under 'What NOT to Modify Without Instruction' in CLAUDE.md.",
    ),
    (
        "Owner-only release metadata",
        OWNER_ONLY,
        "The owner bumps APP_VERSION and writes CHANGELOG.md at release time. "
        "Please drop these changes from your PR.",
    ),
)


def changed_files(base: str, head: str) -> list[str]:
    """Return paths changed between base and head, using the merge base."""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", f"{base}...{head}"],
            text=True,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        print("error: git not found on PATH", file=sys.stderr)
        raise SystemExit(2)
    except subprocess.CalledProcessError as exc:
        print(f"error: git diff {base}...{head} failed:\n{exc.stderr}", file=sys.stderr)
        raise SystemExit(2)
    return [line.strip() for line in out.splitlines() if line.strip()]


def matches(path: str, patterns: tuple[str, ...]) -> bool:
    # Directory entries end in "/" and match by prefix; the rest match exactly.
    return any(
        path.startswith(p) if p.endswith("/") else path == p
        for p in patterns
    )


def main(argv: list[str]) -> int:
    if len(argv) >= 3:
        base, head = argv[1], argv[2]
    elif len(argv) == 1:
        base, head = "origin/main", "HEAD"
    else:
        print(__doc__, file=sys.stderr)
        return 2

    files = changed_files(base, head)
    if not files:
        print(f"No files changed between {base} and {head}.")
        return 0

    violations = [
        (title, note, hits)
        for title, patterns, note in GROUPS
        if (hits := sorted(f for f in files if matches(f, patterns)))
    ]

    if not violations:
        print(f"OK — {len(files)} changed file(s), none protected.")
        return 0

    print("Protected files were modified:\n", file=sys.stderr)
    for title, note, hits in violations:
        print(f"  {title}", file=sys.stderr)
        for f in hits:
            print(f"    - {f}", file=sys.stderr)
        print(f"    {note}\n", file=sys.stderr)

    print(
        "See CLAUDE.md for the full policy. If this change is intentional and\n"
        "approved, the project owner can apply the `approved-pipeline-change`\n"
        "label to this PR to skip this check.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
