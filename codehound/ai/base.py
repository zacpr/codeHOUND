"""AI chat backends.

Adding a provider: if it speaks the OpenAI-compatible /chat/completions API,
just add an AIProvider preset in ai/presets.py. For anything else, subclass
ChatBackend in its own module and register it in ai/__init__.py.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass

TIMEOUT = 120

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def strip_think(text: str) -> str:
    """Drop reasoning blocks (<think>…</think>) that some local models emit."""
    return _THINK_RE.sub("", text).strip()


class AIError(Exception):
    """An AI provider/config failure with a user-readable message."""


def validate_base_url(url: str, hint: str = "check Settings → AI") -> None:
    """Check a base URL is usable (http/https, host, numeric port). Raises AIError."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise AIError(f"invalid base URL {url!r} ({hint}): scheme must be http or https")
    if not parts.hostname:
        raise AIError(f"invalid base URL {url!r} ({hint}): missing host")
    try:
        parts.port  # nonnumeric or out-of-range ports raise ValueError here
    except ValueError as e:
        raise AIError(f"invalid base URL {url!r} ({hint}): {e}") from e


class ChatBackend(ABC):
    """One configured model connection."""

    @abstractmethod
    def ask(self, system: str, prompt: str) -> str:
        """Send a system prompt + user prompt, return the reply text. Raises AIError."""


@dataclass
class AIProvider:
    """An AI service preset. Per-user overrides (model/key/url) come from config."""

    key: str
    label: str
    default_base_url: str = ""
    default_model: str = ""
    env_key: str = ""          # env var consulted when the configured key is blank
    needs_key: bool = False

    def is_configured(self, cfg) -> bool:
        """Enough settings present to actually make a call."""
        if not (cfg.ai_base_url or self.default_base_url):
            return False
        if not (cfg.ai_model or self.default_model):
            return False
        if self.needs_key:
            key = cfg.ai_api_key or (os.environ.get(self.env_key, "") if self.env_key else "")
            if not key:
                return False
        return True

    def backend(self, cfg) -> ChatBackend:
        base_url = cfg.ai_base_url or self.default_base_url
        model = cfg.ai_model or self.default_model
        api_key = cfg.ai_api_key or (os.environ.get(self.env_key, "") if self.env_key else "")
        if not base_url:
            raise AIError(f"{self.label}: set a base URL in Settings → AI")
        if not model:
            raise AIError(f"{self.label}: set a model in Settings → AI")
        if self.needs_key and not api_key:
            raise AIError(
                f"{self.label} needs an API key — set it in Settings → AI"
                + (f" or via ${self.env_key}" if self.env_key else "")
            )
        return OpenAICompatible(base_url, model, api_key)


class OpenAICompatible(ChatBackend):
    """Any endpoint speaking OpenAI's /chat/completions (Ollama, OpenAI, Kimi, Gemini, …)."""

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        validate_base_url(base_url)
        base_url = base_url.rstrip("/")
        # Bare host[:port] with no path (typical for local servers like
        # LM Studio) means the OpenAI API root — which lives under /v1.
        if not urllib.parse.urlsplit(base_url).path:
            base_url += "/v1"
        self.base_url = base_url
        self.model = model
        self.api_key = api_key

    def ask(self, system: str, prompt: str) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise AIError(f"HTTP {e.code} from {self.base_url}: {detail}")
        except http.client.InvalidURL as e:
            raise AIError(f"invalid base URL {self.base_url!r} (check Settings → AI): {e}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise AIError(f"{self.base_url}: {e}")
        try:
            return strip_think(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            if isinstance(data, dict) and "error" in data:
                err = data["error"]
                msg = err.get("message", err) if isinstance(err, dict) else err
                raise AIError(f"{self.base_url}: {msg} — check the base URL in Settings → AI")
            raise AIError(f"unexpected response shape: {str(data)[:300]}")
