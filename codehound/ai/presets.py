"""Built-in AI provider presets. OpenAI-compatible services are one-liners here."""
from __future__ import annotations

from .base import AIProvider

OLLAMA = AIProvider(
    key="ollama",
    label="Ollama / local",
    default_base_url="http://localhost:11434/v1",
    default_model="gemma4:12b",
)
OPENAI = AIProvider(
    key="openai",
    label="OpenAI (ChatGPT)",
    default_base_url="https://api.openai.com/v1",
    default_model="gpt-5.4-mini",
    env_key="OPENAI_API_KEY",
    needs_key=True,
)
GEMINI = AIProvider(
    key="gemini",
    label="Google Gemini",
    default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    default_model="gemini-3.5-flash",
    env_key="GEMINI_API_KEY",
    needs_key=True,
)
KIMI = AIProvider(
    key="kimi",
    label="Kimi (Moonshot)",
    default_base_url="https://api.moonshot.ai/v1",
    default_model="kimi-k3",
    env_key="MOONSHOT_API_KEY",
    needs_key=True,
)
CUSTOM = AIProvider(
    key="custom",
    label="Custom (OpenAI-compatible)",
    env_key="OPENAI_API_KEY",
)
