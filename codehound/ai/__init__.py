"""Registry of AI providers. New providers register here."""
from __future__ import annotations

from . import presets
from .base import AIError, AIProvider, ChatBackend
from .config_like import AiSettings

PROVIDERS: dict[str, AIProvider] = {
    p.key: p for p in (presets.OLLAMA, presets.OPENAI, presets.GEMINI, presets.KIMI, presets.CUSTOM)
}

__all__ = [
    "PROVIDERS", "AIError", "AIProvider", "ChatBackend", "AiSettings",
    "get_backend", "is_configured",
]


def get_backend(cfg: AiSettings) -> ChatBackend:
    """Build the configured chat backend. Raises AIError on bad config."""
    provider = PROVIDERS.get(cfg.ai_provider)
    if provider is None:
        raise AIError(f"unknown AI provider: {cfg.ai_provider!r} (check config.toml [ai])")
    return provider.backend(cfg)


def is_configured(cfg: AiSettings) -> bool:
    """Has the user configured an AI provider well enough to call it?"""
    provider = PROVIDERS.get(cfg.ai_provider)
    return provider.is_configured(cfg) if provider else False
