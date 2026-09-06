"""GitHub provider — prefers the gh CLI login, falls back to a configured PAT."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .base import (
    CleanupCandidate,
    GitAuth,
    Provider,
    RemoteRepo,
    SyncError,
    TokenUrlAuth,
    _toml_str,
    http_json,
    run,
    toml_bool,
    toml_list,
)

API = "https://api.github.com"


class GhCliAuth(GitAuth):
    """Cloning via `gh repo clone` — uses gh's own credentials."""

    def clone(self, repo: RemoteRepo, dest: Path) -> None:
        run(["gh", "repo", "clone", "/".join(repo.host_path[-2:]), str(dest)])


@dataclass
class GitHubAccount:
    user: str = ""                       # display label; the authenticated user is used either way
    token: str = ""                      # PAT; blank = use the gh CLI login
    orgs: list[str] = field(default_factory=list)   # organisations to also mirror
    forks: bool = False
    archived: bool = False


class GitHubProvider(Provider):
    key = "github"
    label = "GitHub"

    # ----- config -----
    def load_accounts(self, raw: list[dict]) -> list[GitHubAccount]:
        return [
            GitHubAccount(
                user=a.get("user", ""),
                token=a.get("token", ""),
                orgs=list(a.get("orgs", [])),
                forks=bool(a.get("forks", False)),
                archived=bool(a.get("archived", False)),
            )
            for a in raw
        ]

    def dump_account(self, account: GitHubAccount) -> str:
        return "\n".join([
            f"user = {_toml_str(account.user)}",
            f"token = {_toml_str(account.token)}",
            f"orgs = {toml_list(account.orgs)}",
            f"forks = {toml_bool(account.forks)}",
            f"archived = {toml_bool(account.archived)}",
        ])

    def account_label(self, account: GitHubAccount) -> str:
        who = account.user or "authenticated user"
        return f"{who} ({'PAT' if account.token else 'gh CLI'})"

    # ----- auth -----
    def _use_gh(self, account: GitHubAccount) -> bool:
        return not account.token and shutil.which("gh") is not None

    def git_auth(self, account: GitHubAccount) -> GitAuth:
        if self._use_gh(account):
            return GhCliAuth()
        if account.token:
            return TokenUrlAuth(f"x-access-token:{quote(account.token)}")
        raise SyncError("GitHub account needs a token or the gh CLI")

    # ----- listing -----
    def _api_page(self, account: GitHubAccount, endpoint: str, page: int) -> list[dict]:
        url = f"{endpoint}{'&' if '?' in endpoint else '?'}per_page=100&page={page}"
        if self._use_gh(account):
            try:
                return json.loads(run(["gh", "api", url]))
            except json.JSONDecodeError as e:
                raise SyncError(f"gh api returned invalid JSON: {e}")
        if not account.token:
            raise SyncError("GitHub account needs a token or the gh CLI")
        return http_json(f"{API}/{url}", {"Authorization": f"Bearer {account.token}"})

    def _api_all(self, account: GitHubAccount, endpoint: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            batch = self._api_page(account, endpoint, page)
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def list_repos(self, account: GitHubAccount, progress) -> list[RemoteRepo]:
        raw = self._api_all(account, "user/repos?affiliation=owner")
        for org in account.orgs:
            raw += self._api_all(account, f"orgs/{quote(org)}/repos")
        repos: dict[str, RemoteRepo] = {}
        for r in raw:
            if r.get("fork") and not account.forks:
                continue
            if r.get("archived") and not account.archived:
                continue
            full = r["full_name"]
            owner, name = full.split("/", 1)
            repos[full] = RemoteRepo(["github.com", owner, name], f"https://github.com/{full}.git", name)
        return sorted(repos.values(), key=lambda r: r.host_path)

    # ----- janitor -----
    def cleanup_candidates(self, account: GitHubAccount, progress) -> list[CleanupCandidate]:
        """Archived repos are safe to remove locally (GitHub keeps them read-only)."""
        out: list[CleanupCandidate] = []
        for r in self._api_all(account, "user/repos?affiliation=owner"):
            if not r.get("archived"):
                continue
            full = r["full_name"]
            owner, name = full.split("/", 1)
            out.append(CleanupCandidate(
                RemoteRepo(["github.com", owner, name], f"https://github.com/{full}.git", name),
                branch="",
                reason="archived",
            ))
        return out
