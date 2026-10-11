"""Janitor — remove local clones whose remote work is done (merged/abandoned/archived).

Safety first: dry-run by default; a clone is only removed when the tree is clean,
the branch is pushed (or the remote branch is gone), and the path is inside the
configured clone destination.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import providers
from .config import Config
from .providers.base import CleanupCandidate, RemoteRepo, SyncError
from .safepath import UnsafePathError, safe_rmtree


@dataclass
class JanitorResult:
    candidates: list[tuple[Path, str]] = field(default_factory=list)   # (path, reason)
    removed: list[Path] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)      # (path, why)
    errors: list[str] = field(default_factory=list)


def _git(path: Path, args: list[str]) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=15)
    return r.stdout.strip() if r.returncode == 0 else ""


def _is_clean(path: Path) -> bool:
    return not _git(path, ["status", "--porcelain"]).strip()


def _branch_pushed(path: Path, branch: str) -> bool:
    """True if local branch has an upstream and is not ahead of it (or remote ref is gone)."""
    if not branch:
        return True  # repo-level removal (archived/disabled) — rely on clean tree
    ahead = _git(path, ["rev-list", "--count", f"origin/{branch}..{branch}"])
    if ahead and ahead != "0":
        return False
    # remote branch deleted → nothing left to push to
    if not _git(path, ["ls-remote", "--heads", "origin", branch]):
        return True
    return ahead == "0"


def _current_branch(path: Path) -> str:
    return _git(path, ["branch", "--show-current"])


def _local_path(cfg: Config, repo: RemoteRepo) -> Path:
    return Path(cfg.clone_dest).expanduser().joinpath(*repo.host_path)


def cleanup_candidates(cfg: Config, progress=print) -> list[tuple[CleanupCandidate, Path]]:
    """All cleanup candidates across providers, mapped to local paths."""
    out: list[tuple[CleanupCandidate, Path]] = []
    for key, accounts in cfg.accounts.items():
        provider = providers.get(key)
        if provider is None:
            continue
        for account in accounts:
            label = f"{provider.label} {provider.account_label(account)}"
            try:
                cands = provider.cleanup_candidates(account, progress)
            except SyncError as e:
                progress(f"{label}: cleanup check failed: {e}")
                continue
            for cand in cands:
                path = _local_path(cfg, cand.repo)
                if (path / ".git").exists():
                    out.append((cand, path))
    return out


def run(cfg: Config, dry_run: bool = True, progress=print) -> JanitorResult:
    result = JanitorResult()
    excluded = set(cfg.janitor_exclude)
    for cand, path in cleanup_candidates(cfg, progress):
        rel = "/".join(cand.repo.host_path)
        if rel in excluded or str(path) in excluded or cand.repo.name in excluded:
            result.skipped.append((path, "excluded in settings"))
            continue
        reason = cand.reason
        branch = cand.branch or _current_branch(path)

        # safety gates
        if not _is_clean(path):
            result.skipped.append((path, "uncommitted changes"))
            continue
        if cand.branch and _current_branch(path) != cand.branch:
            result.skipped.append((path, f"on {branch}, candidate is {cand.branch}"))
            continue
        if not _branch_pushed(path, branch):
            result.skipped.append((path, f"branch {branch} has unpushed commits"))
            continue

        result.candidates.append((path, reason))
        if dry_run:
            progress(f"  would remove {rel}  ({reason})")
        else:
            try:
                safe_rmtree(path, (cfg.clone_dest,), f"janitor {rel}")
                result.removed.append(path)
                progress(f"  removed {rel}  ({reason})")
            except (OSError, UnsafePathError) as e:
                result.errors.append(f"{rel}: {e}")
                progress(f"  FAILED {rel}: {e}")
    return result
