"""
GitHub Models API client (OpenAI-compatible REST).

Endpoint
--------
``https://models.inference.ai.azure.com/chat/completions``

Authentication
--------------
GitHub Personal Access Token (PAT) with the ``models: read`` permission
(or the legacy ``read:packages`` scope). Resolved in this order:

1. ``$GITHUB_TOKEN`` environment variable
2. ``$GITHUB_PAT`` environment variable
3. Plain-text file at ``%USERPROFILE%\\.canscope\\copilot_token``

Available models
----------------
``gpt-4o-mini`` (default, cheap+fast), ``gpt-4o``, ``Phi-3-medium-128k``,
``Mistral-large``, ``Llama-3.3-70B-Instruct``, ``Codestral-2501``,
``DeepSeek-V3``, ``Cohere-command-r-plus`` and many others. See the
GitHub Models catalogue for the latest list.

The client is intentionally vendor-neutral — to swap providers, point
``base_url`` at any other OpenAI-compatible endpoint.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://models.inference.ai.azure.com"

#: Maximum tokens for the response (per request)
DEFAULT_MAX_TOKENS = 2048
#: Sampling temperature — lower = more deterministic
DEFAULT_TEMPERATURE = 0.2
#: Network timeout in seconds (analysis can take a while on large prompts)
DEFAULT_TIMEOUT_S = 120


class LLMError(RuntimeError):
    """Raised on auth / network / API errors so the GUI can show a helpful
    message instead of a stack trace."""


class LLMClient(Protocol):
    """Minimal interface — used by the engine and chat panel."""

    def chat(self, messages: list[dict], model: str | None = None) -> str: ...
    def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
    ) -> Iterator[str]: ...


# ── GitHub Models implementation ────────────────────────────────────────

class GitHubModelsClient:
    """
    OpenAI-compatible client for the GitHub Models API.

    Parameters
    ----------
    token : str | None
        GitHub PAT.  If None, resolved from environment / token file.
    model : str
        Model id (default: ``gpt-4o-mini``).
    base_url : str
        API base URL.  Override to use Azure / OpenAI / Ollama / etc.
    """

    def __init__(
        self,
        token: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.token = token or _resolve_token()
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s

    # ── public API ───────────────────────────────────────────────────────

    def chat(self, messages: list[dict], model: str | None = None) -> str:
        """Non-streaming completion. Returns the full text reply."""
        chunks = list(self.chat_stream(messages, model=model))
        return "".join(chunks)

    def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
    ) -> Iterator[str]:
        """Streaming completion. Yields text chunks as they arrive."""
        try:
            import requests
        except ImportError as exc:
            raise LLMError(
                "The 'requests' package is required for the LLM client. "
                "Run: pip install requests"
            ) from exc

        url = f"{self.base_url}/chat/completions"
        body = {
            "model":       model or self.model,
            "messages":    messages,
            "temperature": self.temperature,
            "max_tokens":  self.max_tokens,
            "stream":      True,
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type":  "application/json",
            "Accept":        "text/event-stream",
        }

        try:
            with requests.post(
                url, headers=headers, json=body,
                stream=True, timeout=self.timeout_s,
            ) as resp:
                if resp.status_code == 401:
                    raise LLMError(
                        "GitHub Models API rejected the token (HTTP 401).\n\n"
                        "Fix: create a fine-grained PAT at\n"
                        "  https://github.com/settings/tokens?type=beta\n"
                        "and enable  Account permissions → GitHub Copilot → Read-only.\n"
                        "Save the token (github_pat_…) to:\n"
                        f"  {Path.home() / '.canscope' / 'copilot_token'}"
                    )
                if resp.status_code == 429:
                    raise LLMError(
                        "GitHub Models rate limit hit (HTTP 429). "
                        "Wait a minute and retry, or switch to a less busy model."
                    )
                if resp.status_code >= 400:
                    try:
                        detail = resp.json()
                    except Exception:
                        detail = resp.text[:300]
                    raise LLMError(
                        f"GitHub Models API error {resp.status_code}: {detail}"
                    )

                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith("data: "):
                        payload = line[6:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc


# ── helpers ──────────────────────────────────────────────────────────────

def _resolve_token() -> str:
    """Resolve a GitHub token from env vars or the user's .canscope folder."""
    for env_name in ("GITHUB_TOKEN", "GITHUB_PAT"):
        tok = os.environ.get(env_name)
        if tok:
            return tok.strip()

    home = Path(os.path.expanduser("~"))
    token_file = home / ".canscope" / "copilot_token"
    if token_file.exists():
        try:
            return token_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    raise LLMError(
        "No GitHub token found. Set the GITHUB_TOKEN environment variable "
        f"or place a PAT at {token_file}.\n"
        "Generate a token at https://github.com/settings/tokens with "
        "'models: read' permission."
    )


def list_models() -> list[str]:
    """Best-effort static list of commonly available GitHub Models."""
    return [
        "gpt-4o-mini",
        "gpt-4o",
        "Phi-3-medium-128k-instruct",
        "Mistral-large-2407",
        "Llama-3.3-70B-Instruct",
        "Codestral-2501",
        "DeepSeek-V3",
        "Cohere-command-r-plus",
    ]
