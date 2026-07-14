"""Collect metadata from a git repository via subprocess."""
from __future__ import annotations

import subprocess
from pathlib import Path

GIT_TIMEOUT = 15  # seconds per command; repos on slow disks shouldn't stall the scan


def _git(path: str, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def collect(path: str) -> dict:
    """Return git metadata for the working tree at `path`."""
    info: dict = {}
    info["branch"] = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    head = _git(path, "log", "-1", "--format=%aI%x09%s")
    if head and "\t" in head:
        date, msg = head.split("\t", 1)
        info["last_commit_date"] = date
        info["last_commit_msg"] = msg[:200]
    info["remote_url"] = _git(path, "remote", "get-url", "origin")
    status = _git(path, "status", "--porcelain")
    info["dirty"] = 1 if status else 0
    first = _git(path, "rev-list", "--max-parents=0", "--format=%aI", "--all")
    if first:
        # output lines alternate "commit <sha>" / "<date>"; take the oldest date
        dates = [l for l in first.splitlines() if l and not l.startswith("commit ")]
        if dates:
            info["created"] = min(dates)
    return info


def is_bare_repo(path: Path) -> bool:
    """Heuristic for a bare repository directory (e.g. foo.git)."""
    return (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir()
