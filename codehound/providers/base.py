"""Provider framework: auth backends and repo listing for remote code hosts.

To add a new provider:
1. Create a module in this package with an Account dataclass, a Provider
   subclass (key, label, load_accounts, dump_account, account_label,
   list_repos, git_auth) and any GitAuth backends it needs.
2. Register an instance in providers/__init__.py — config load/save and
   `codehound sync` pick it up automatically.
3. Optionally add an editor dialog to settings.ACCOUNT_EDITORS for the GUI.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

API_TIMEOUT = 30


class SyncError(Exception):
    """A provider/auth/clone failure with a user-readable message."""


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def toml_list(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def toml_bool(value: bool) -> str:
    return str(value).lower()


def run(cmd: list[str], timeout: int = 600) -> str:
    """Run a command, returning stdout; raise SyncError on failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise SyncError(f"command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise SyncError(f"timed out: {cmd[0]} {' '.join(cmd[1:2])}")
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()
        raise SyncError(tail[-1][:300] if tail else f"exit code {r.returncode}")
    return r.stdout


def http_json(url: str, headers: dict[str, str]) -> dict | list:
    """GET a JSON API endpoint; raise SyncError on failure."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise SyncError(f"HTTP {e.code} from {url}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise SyncError(f"{url}: {e}")


@dataclass
class RemoteRepo:
    """A repository offered by a provider account."""
    host_path: list[str]  # path segments under the clone dest, e.g. ["github.com", "owner", "repo"]
    clone_url: str        # clean HTTPS URL (no credentials)
    name: str = ""


class GitAuth(ABC):
    """Authenticates git clone/fetch/pull for one account."""

    @abstractmethod
    def clone(self, repo: RemoteRepo, dest: Path) -> None:
        """Clone repo into dest (parent dirs already created)."""

    def git_args(self) -> list[str]:
        """Extra `git` arguments (e.g. -c http.extraheader=…) for fetch/pull in existing clones."""
        return []


class NoAuth(GitAuth):
    """Plain clone — public repos or ambient git credentials."""

    def clone(self, repo: RemoteRepo, dest: Path) -> None:
        run(["git", "clone", repo.clone_url, str(dest)])


class TokenUrlAuth(GitAuth):
    """Credentials embedded in the clone URL (they persist in .git/config)."""

    def __init__(self, userinfo: str):
        self.userinfo = userinfo

    def clone(self, repo: RemoteRepo, dest: Path) -> None:
        url = repo.clone_url.replace("https://", f"https://{self.userinfo}@", 1)
        run(["git", "clone", url, str(dest)])


class BearerHeaderAuth(GitAuth):
    """Bearer token passed per command; the remote URL stays clean."""

    def __init__(self, token: str):
        self.token = token

    def git_args(self) -> list[str]:
        return ["-c", f"http.extraheader=AUTHORIZATION: Bearer {self.token}"]

    def clone(self, repo: RemoteRepo, dest: Path) -> None:
        run(["git", *self.git_args(), "clone", repo.clone_url, str(dest)])


@dataclass
class CleanupCandidate:
    """A remote branch the janitor may delete the local clone of."""
    repo: RemoteRepo
    branch: str
    reason: str          # e.g. "PR #42 merged", "abandoned", "archived"


class Provider(ABC):
    """A code hosting provider (GitHub, Azure DevOps, …)."""

    key: str = ""
    label: str = ""

    @abstractmethod
    def load_accounts(self, raw: list[dict]) -> list:
        """Parse [[accounts.<key>]] tables into Account objects."""

    @abstractmethod
    def dump_account(self, account) -> str:
        """Serialise one account as TOML key=value lines (no table header)."""

    @abstractmethod
    def account_label(self, account) -> str:
        """Short display label, e.g. 'zacpr (gh CLI)'."""

    @abstractmethod
    def list_repos(self, account, progress) -> list[RemoteRepo]:
        """List all repos the account should mirror. Raises SyncError."""

    @abstractmethod
    def git_auth(self, account) -> GitAuth:
        """Auth backend for cloning/updating this account's repos. Raises SyncError."""

    def cleanup_candidates(self, account, progress) -> list[CleanupCandidate]:
        """Branches/repos safe to remove locally. Providers override; default none."""
        return []
