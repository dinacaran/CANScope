"""Cover the protected-path guard's matching rules.

The guard is the only enforcement point for the PR policy — CODEOWNERS just
requests review, and "Require review from Code Owners" is off because a lone
owner cannot self-approve. So the pattern lists are worth testing directly.

`tools/` is not a package, so the module is loaded from its path.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_GUARD_PATH = Path(__file__).resolve().parents[1] / "tools" / "check_protected_paths.py"
_spec = importlib.util.spec_from_file_location("check_protected_paths", _GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _protecting_group(path: str) -> str | None:
    """Return the title of the group that protects `path`, or None."""
    for title, patterns, _note in guard.GROUPS:
        if guard.matches(path, patterns):
            return title
    return None


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/build.yml",
        ".github/workflows/pr.yml",
        ".github/CODEOWNERS",
        ".github/pull_request_template.md",
        "tools/check_protected_paths.py",
        "tools/check_version_consistency.py",
        "tools/hooks/pre-commit",
    ],
)
def test_ci_and_policy_paths_are_protected(path):
    """build.yml runs with `contents: write` on a release tag, so a merged PR
    that edited it would run under the owner's next release. And the guard
    cannot be trusted to police edits to itself.
    """
    assert _protecting_group(path) == "CI, release, and policy enforcement"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("core/load_worker.py", "Validated loading/decoding pipeline"),
        ("core/readers/blf_can_reader.py", "Validated loading/decoding pipeline"),
        ("requirements.txt", "Validated loading/decoding pipeline"),
        ("core/signal_store.py", "Protected core files"),
        ("CANScope.spec", "Protected core files"),
        ("CHANGELOG.md", "Owner-only release metadata"),
        ("app.py", "Owner-only release metadata"),
    ],
)
def test_previously_protected_paths_still_match(path, expected):
    assert _protecting_group(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "gui/main_window.py",
        "core/debug_inspector.py",
        "tests/test_debug_mode.py",
        "docs/PR_Policy.md",
        "CONTRIBUTING.md",
        "README.md",
    ],
)
def test_ordinary_contributor_paths_are_not_protected(path):
    assert _protecting_group(path) is None


def test_directory_patterns_do_not_match_a_similarly_named_sibling():
    """Prefix matching means a `tools/` rule must not swallow `toolsmith.py`."""
    assert _protecting_group("toolsmith.py") is None
    assert _protecting_group("tools_notes.md") is None
    assert _protecting_group("core/readers_notes.md") is None
