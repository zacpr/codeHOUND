"""Structural type for anything carrying the [ai] settings (config.Config satisfies it)."""
from __future__ import annotations

from typing import Protocol


class AiSettings(Protocol):
    ai_provider: str
    ai_model: str
    ai_api_key: str
    ai_base_url: str
    ai_catalogue_summary: bool
