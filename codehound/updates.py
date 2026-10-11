"""GitHub release watch — spot newer releases of catalogued projects.

Phase 1 (read-only): build a watch list from catalogue rows that have a GitHub
remote, ask the Releases API what the newest published release is, match its
assets against this machine's OS/arch, and record the result in the `updates`
table. `codehound updates` prints it; the tray app bubbles a notification.
"""
from __future__ import annotations

import json
import platform
import re
import shutil
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlparse

from . import db
from .providers.base import SyncError, http_json, run

API = "https://api.github.com"

# Remote URL forms we recognise: https://github.com/owner/repo(.git),
# git@github.com:owner/repo(.git), https://github.com/owner/repo/ (trailing slash).
_GITHUB_PATH = re.compile(r"^([^/]+)/([^/]+?)(?:\.git)?/?$")

# Asset-name fragments that mark a build as NOT for this machine.
_ARCH_TOKENS = {
    "x86_64": ("x86_64", "amd64", "x64", "64bit-intel"),
    "aarch64": ("aarch64", "arm64", "armv8"),
    "armv7l": ("armv7", "arm32"),
    "i686": ("i386", "i686", "386", "32bit"),
}
_OS_TOKENS = {
    "Linux": ("linux", "ubuntu", "debian", "fedora", "appimage"),
    "Darwin": ("macos", "mac", "darwin", "osx", "apple-darwin"),
    "Windows": ("windows", "win64", "win32", ".exe", "msi"),
}
# Foreign-OS markers used to reject an asset when we know what we want.
_FOREIGN_OS = {
    "Linux": ("darwin", "macos", "-mac", "osx", "windows", "win64", "win32", ".exe", ".msi", ".dmg"),
    "Darwin": ("linux", "windows", "win64", "win32", ".exe", ".msi", "appimage"),
    "Windows": ("linux", "darwin", "macos", "-mac", "osx", "appimage"),
}
# Prefer self-contained formats over distro packages / archives for now —
# phase 1 is detection only, but the ranking also picks what phase 2 will install.
_FORMAT_RANK = {".appimage": 0, ".rpm": 1, ".deb": 2, ".tar.xz": 3, ".tar.gz": 3,
                ".tgz": 3, ".zip": 4, ".tar.zst": 3}


@dataclass
class WatchTarget:
    repo: str          # owner/repo
    path: str          # local clone path
    name: str          # display name from the catalogue


def parse_github_repo(url: str) -> str | None:
    """owner/repo from any GitHub remote URL, or None if it isn't GitHub."""
    if not url:
        return None
    url = url.strip()
    if url.startswith("git@"):                       # git@github.com:owner/repo.git
        _, _, rest = url.partition(":")
        path = rest
    else:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https", "git", "ssh"):
            return None
        if parsed.hostname not in ("github.com", "www.github.com"):
            return None
        path = parsed.path.lstrip("/")
    m = _GITHUB_PATH.match(path.strip("/"))
    return f"{m.group(1)}/{m.group(2)}" if m else None


def watch_targets(conn: sqlite3.Connection) -> list[WatchTarget]:
    """Catalogued (live) projects whose remote lives on GitHub."""
    out: list[WatchTarget] = []
    seen: set[str] = set()
    for row in db.all_projects(conn):
        if row["missing"] or not row["remote_url"]:
            continue
        repo = parse_github_repo(row["remote_url"])
        if not repo or repo in seen:
            continue
        seen.add(repo)
        out.append(WatchTarget(repo=repo, path=row["path"], name=row["name"]))
    return out


def local_tag(path: str) -> str:
    """The tag the checkout sits on ('' when there are no tags / not a repo)."""
    try:
        out = run(["git", "-C", path, "describe", "--tags", "--always"], timeout=10)
    except SyncError:
        return ""
    tag = out.strip()
    # describe prints <tag>-<n>-g<sha> when HEAD is past the tag — that means
    # local is ahead of (or between) releases, not behind; keep the tag anyway.
    return tag.split("-")[0] if re.match(r"^.+-\d+-g[0-9a-f]+$", tag) else tag


def _norm(tag: str) -> tuple:
    """Version-sortable key for a tag: v1.10.0 > v1.9.2, non-numeric sorts low."""
    parts = re.split(r"[.\-+]", tag.lstrip("vV"))
    return tuple(int(p) if p.isdigit() else -1 for p in parts)


def newer(candidate: str, current: str) -> bool:
    """True if candidate release is newer than the local tag."""
    if not candidate:
        return False
    if not current:
        return True
    if candidate == current:
        return False
    return _norm(candidate) > _norm(current)


def host_os() -> str:
    return {"Linux": "Linux", "Darwin": "Darwin", "Windows": "Windows"}.get(
        platform.system(), platform.system())


def host_arch() -> str:
    m = platform.machine().lower()
    return {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64",
            "arm64": "aarch64", "armv7l": "armv7l", "i686": "i686",
            "x86": "i686"}.get(m, m)


def pick_asset(assets: list[dict], os_name: str | None = None,
               arch: str | None = None) -> tuple[dict | None, str]:
    """Best installable asset for this machine, plus a reason when there is none.

    Only looks at real release binaries: source archives (source.zip /
    source.tar.gz uploaded automatically by GitHub) are skipped.
    """
    os_name = os_name or host_os()
    arch = arch or host_arch()
    candidates = []
    for a in assets:
        name = (a.get("name") or "").lower()
        if name in ("source.zip", "source.tar.gz"):   # GitHub's auto source archives
            continue
        if any(tok in name for tok in _FOREIGN_OS.get(os_name, ())):
            continue
        arch_tokens = _ARCH_TOKENS.get(arch, (arch,))
        # If the name names an arch at all, it must name ours.
        named_arch = any(t in name for group in _ARCH_TOKENS.values() for t in group)
        if named_arch and not any(t in name for t in arch_tokens):
            continue
        ext = next((e for e in _FORMAT_RANK if name.endswith(e)), None)
        candidates.append((_FORMAT_RANK.get(ext, 5), name, a))
    if candidates:
        candidates.sort(key=lambda c: (c[0], c[1]))
        return candidates[0][2], ""
    if not assets:
        return None, "release has no assets"
    return None, f"no asset matches {os_name}/{arch}"


def _api_get(cfg, path: str) -> list | dict:
    """GET a GitHub API path with the same auth the provider uses:
    PAT if configured, else gh CLI login, else anonymous."""
    for account in cfg.accounts.get("github", []):
        if getattr(account, "token", ""):
            return http_json(f"{API}/{path}", {"Authorization": f"Bearer {account.token}"})
    if shutil.which("gh"):
        try:
            return json.loads(run(["gh", "api", path]))
        except (SyncError, json.JSONDecodeError):
            pass   # gh present but not logged in — try anonymous
    return http_json(f"{API}/{path}", {})


def _is_authed(cfg) -> bool:
    return any(getattr(a, "token", "") for a in cfg.accounts.get("github", []))


def _latest_release(cfg, repo: str) -> dict | None:
    """Newest non-draft, non-prerelease release, or None if the repo has none."""
    releases = _api_get(cfg, f"repos/{repo}/releases?per_page=10")
    published = [r for r in releases if not r.get("draft") and not r.get("prerelease")]
    return published[0] if published else None


def check_updates(cfg, conn: sqlite3.Connection, progress=print) -> list[sqlite3.Row]:
    """Refresh the updates table for every watched repo; returns available rows.

    Individual repo failures (404, rate limit) are reported and skipped — one
    unreachable repo must not sink the whole check.
    """
    authed = _is_authed(cfg)
    targets = watch_targets(conn)
    if not targets:
        progress("no catalogued projects with a GitHub remote — nothing to watch")
    for t in targets:
        try:
            release = _latest_release(cfg, t.repo)
        except SyncError as e:
            hint = "" if authed else " (private or rate-limited — add a GitHub token in Settings → Accounts)"
            progress(f"  {t.repo}: check failed — {e}{hint}")
            continue
        if release is None:
            db.upsert_update(conn, {
                "repo": t.repo, "path": t.path, "name": t.name,
                "current_tag": local_tag(t.path), "latest_tag": None,
                "published_at": None, "asset_name": None, "asset_url": None,
                "available": 0, "compatible": 0, "reason": "no published releases",
                "last_checked": db.now()})
            continue
        current = local_tag(t.path)
        tag = release.get("tag_name") or ""
        asset, reason = pick_asset(release.get("assets", []))
        is_new = newer(tag, current)
        db.upsert_update(conn, {
            "repo": t.repo, "path": t.path, "name": t.name,
            "current_tag": current, "latest_tag": tag,
            "published_at": release.get("published_at"),
            "asset_name": (asset or {}).get("name"),
            "asset_url": (asset or {}).get("browser_download_url"),
            "available": 1 if is_new else 0,
            "compatible": 1 if (is_new and asset) else 0,
            "reason": "" if (is_new and asset) else ("up to date" if not is_new else reason),
            "last_checked": db.now()})
    conn.commit()
    return db.available_updates(conn)
