"""
GitHub-token storage + resolution helper.

Single place for reading/writing the CANScope token file and for reporting
*where* the active token comes from, so the GUI can show accurate status
without duplicating :func:`client._resolve_token` logic.

Resolution precedence (matches the client):

1. ``GITHUB_TOKEN`` environment variable
2. ``GITHUB_PAT`` environment variable
3. Plain-text file at ``%USERPROFILE%\\.canscope\\copilot_token``

Security note: this module never logs or prints token values. Use
:func:`mask_token` for any human-visible output.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Environment variables checked (in order) before the token file.
ENV_VARS: tuple[str, ...] = ("GITHUB_TOKEN", "GITHUB_PAT")

#: Known GitHub PAT prefixes, used for masking + the soft sanity check.
KNOWN_PREFIXES: tuple[str, ...] = (
    "github_pat_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_",
)


def token_file_path() -> Path:
    """Absolute path to the CANScope token file (may not exist)."""
    return Path(os.path.expanduser("~")) / ".canscope" / "copilot_token"


def load_token() -> str | None:
    """Return the stripped token from the file, or ``None`` if absent/empty."""
    path = token_file_path()
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def save_token(token: str) -> Path:
    """Write *token* to the token file (creating ``.canscope`` if needed).

    The value is stripped and written UTF-8 with **no** trailing newline.
    Returns the path written.
    """
    token = token.strip()
    path = token_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    return path


def remove_token() -> bool:
    """Delete the token file. Returns ``True`` if a file was removed."""
    path = token_file_path()
    if path.exists():
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return False


def resolve_token_source() -> tuple[str | None, str]:
    """Resolve the active token and where it came from.

    Returns ``(token, source)`` where *source* is one of
    ``"GITHUB_TOKEN"``, ``"GITHUB_PAT"``, ``"file"`` or ``"none"``.
    """
    for env_name in ENV_VARS:
        value = os.environ.get(env_name)
        if value and value.strip():
            return value.strip(), env_name

    tok = load_token()
    if tok:
        return tok, "file"

    return None, "none"


def mask_token(token: str | None) -> str:
    """Return a safe, human-readable masked form, e.g. ``github_pat_…abcd``.

    Never returns the raw token. Shows a recognised prefix (or the first few
    characters) plus the last 4 characters.
    """
    if not token:
        return ""
    token = token.strip()
    last4 = token[-4:] if len(token) >= 4 else token
    for prefix in KNOWN_PREFIXES:
        if token.startswith(prefix):
            return f"{prefix}…{last4}"
    head = token[:4] if len(token) > 8 else ""
    return f"{head}…{last4}" if head else f"…{last4}"


def looks_like_pat(token: str) -> bool:
    """Soft check: does *token* start with a known GitHub PAT prefix?"""
    token = token.strip()
    return any(token.startswith(p) for p in KNOWN_PREFIXES)
