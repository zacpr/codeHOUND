"""Mirror repos from configured provider accounts and keep local clones in sync."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import providers
from .config import Config
from .providers.base import RemoteRepo, SyncError, run


@dataclass
class SyncResult:
    remote: int = 0
    cloned: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    skipped_dirty: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # "repo: error"


def clone_dest(cfg: Config, repo: RemoteRepo) -> Path:
    return Path(cfg.clone_dest).expanduser().joinpath(*repo.host_path)


def _git(dest: Path, args: list[str]) -> str:
    return run(["git", "-C", str(dest), *args])


def _is_dirty(dest: Path) -> bool:
    return bool(_git(dest, ["status", "--porcelain"]).strip())


def _update(dest: Path, git_args: list[str]) -> str:
    """Fetch all remotes and fast-forward the current branch. Returns 'dirty'|'updated'|'unchanged'."""
    _git(dest, [*git_args, "fetch", "--all", "--prune"])
    if _is_dirty(dest):
        return "dirty"
    try:
        out = _git(dest, [*git_args, "pull", "--ff-only"])
    except SyncError:
        return "dirty"  # e.g. local branch has diverged — leave it alone
    return "unchanged" if "Already up to date" in out else "updated"


def sync_accounts(cfg: Config, progress=print) -> SyncResult:
    result = SyncResult()
    for key, accounts in cfg.accounts.items():
        provider = providers.get(key)
        if provider is None:
            result.failed.append(f"unknown provider: {key}")
            continue
        for account in accounts:
            label = f"{provider.label} {provider.account_label(account)}"
            try:
                repos = provider.list_repos(account, progress)
                auth = provider.git_auth(account)
            except SyncError as e:
                result.failed.append(f"{label}: {e}")
                progress(f"{label}: {e}")
                continue
            progress(f"{label}: {len(repos)} repos")
            result.remote += len(repos)
            for repo in repos:
                rel = "/".join(repo.host_path)
                dest = clone_dest(cfg, repo)
                try:
                    if (dest / ".git").exists():
                        status = _update(dest, auth.git_args())
                        if status == "dirty":
                            result.skipped_dirty.append(rel)
                        elif status == "updated":
                            result.updated.append(rel)
                            progress(f"  updated {rel}")
                        else:
                            result.unchanged += 1
                    elif dest.exists() and any(dest.iterdir()):
                        raise SyncError(f"{dest} exists and is not a git clone")
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        progress(f"  cloning {rel}…")
                        auth.clone(repo, dest)
                        result.cloned.append(rel)
                except SyncError as e:
                    result.failed.append(f"{rel}: {e}")
                    progress(f"  FAILED {rel}: {e}")
    return result
