"""Registry of code hosting providers. New providers register here."""
from __future__ import annotations

from .azure import AzureProvider
from .base import Provider, RemoteRepo, SyncError
from .github import GitHubProvider

PROVIDERS: dict[str, Provider] = {p.key: p for p in (GitHubProvider(), AzureProvider())}

__all__ = ["PROVIDERS", "Provider", "RemoteRepo", "SyncError", "get"]


def get(key: str) -> Provider | None:
    return PROVIDERS.get(key)
