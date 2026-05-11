"""LLM integration layer (GitHub Models API)."""
from core.diagnostics.llm.client import LLMClient, LLMError, GitHubModelsClient
from core.diagnostics.llm.prompts import (
    SYSTEM_PROMPT_BASE,
    build_analysis_prompt,
    build_chat_followup_prompt,
)

__all__ = [
    "LLMClient", "LLMError", "GitHubModelsClient",
    "SYSTEM_PROMPT_BASE", "build_analysis_prompt", "build_chat_followup_prompt",
]
