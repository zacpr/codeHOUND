"""GitHub release watch — spot newer releases of catalogued projects.

Phase 1: build a watch list from catalogue rows that have a GitHub remote, ask
the Releases API what the newest published release is, match its assets against
this machine's OS/arch, and record the result in the `updates` table.
Phase 2: `install_update` downloads the matched asset, verifies its SHA-256
(from a SHA256SUMS asset or a hash in the release body when published), and
installs it — replacing an existing managed/app binary in place when practical,
otherwise into a managed dir (`installs/<repo>/<version>/`) with a symlink in
`~/.local/bin`. The previous version is kept for `rollback_update`.
Phase 3: auto-update policy. `apply_updates` runs after a check: with the
default `[updates] mode = "stage"`, compatible updates are downloaded,
verified and unpacked to a staging dir (`stage_update`) and the user confirms
(`activate_staged` — via `codehound update REPO`, `updates --activate`, or
clicking the tray message). `mode = "silent"` or a per-repo `auto=1` installs
straight away; `auto=0` skips the repo entirely.
Phase 4: system package installs and self-update. `system_package_install`
(`codehound update REPO --system`) hands a verified RPM/DEB to the distro
package manager through pkexec, so the app lands system-wide instead of
user-land. `self_update` (`codehound self-update`) keeps codeHOUND itself on
the watch list via `[updates] self_repo`, installs its own RPM/DEB the same
way, and `schedule_relaunch` detaches a helper that waits for the dashboard
to exit and starts the new build.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import __version__, db
from .config import DATA_DIR
from .providers.base import SyncError, http_json, run
from .safepath import safe_rmtree

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


def _gh_token() -> str:
    """Token from a logged-in gh CLI; '' when gh is missing or logged out."""
    if shutil.which("gh") is None:
        return ""
    try:
        return run(["gh", "auth", "token"], timeout=10).strip()
    except SyncError:
        return ""


def _auth_headers(cfg) -> dict:
    """Auth headers for asset downloads — same ladder as `_api_get`:
    configured PAT first, then gh CLI login, then anonymous."""
    for account in cfg.accounts.get("github", []):
        token = getattr(account, "token", "")
        if token:
            return {"Authorization": f"Bearer {token}"}
    token = _gh_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _asset_href(asset: dict) -> str:
    """The URL to actually fetch one asset from.

    A private repo's `browser_download_url` 404s even when a token is sent —
    GitHub only serves it to a logged-in web session. The API asset endpoint
    (its `url`, fetched with Accept: application/octet-stream) is the
    authenticated path, and it works for public repos too.
    """
    return asset.get("url") or asset.get("browser_download_url") or ""


def _asset_href_for(release: dict | None, asset_name: str) -> str:
    """`_asset_href` for the release asset with this exact name, '' if absent."""
    for a in (release or {}).get("assets", []):
        if (a.get("name") or "") == asset_name:
            return _asset_href(a)
    return ""


def _latest_release(cfg, repo: str) -> dict | None:
    """Newest non-draft, non-prerelease release, or None if the repo has none."""
    releases = _api_get(cfg, f"repos/{repo}/releases?per_page=10")
    published = [r for r in releases if not r.get("draft") and not r.get("prerelease")]
    return published[0] if published else None


# ---------------------------------------------------------------- installation

# Formats phase 2 can install without admin rights. rpm/deb payloads are
# unpacked user-land (rpm2cpio/ar); `system_package_install` installs them
# system-wide via pkexec instead.
_INSTALLABLE = (".appimage", ".zip", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2",
                ".tar.zst", ".rpm", ".deb")

_HEX64 = re.compile(r"\b([0-9a-f]{64})\b")


def downloads_dir() -> Path:
    d = DATA_DIR / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def installs_dir() -> Path:
    d = DATA_DIR / "installs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def user_bin_dir() -> Path:
    d = Path.home() / ".local" / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_roots() -> tuple[Path, Path]:
    """The only directories recursive deletes may touch: our downloads and
    installs trees. Passed to safe_rmtree so a bad path can never reach
    anything outside them."""
    return (downloads_dir(), installs_dir())


def _opt_path(value) -> Path | None:
    """A path stored in a DB column, or None when unset. Never Path("") —
    that is the current directory, and rmtree on it wipes whatever cwd is."""
    value = (value or "").strip()
    return Path(value) if value else None


def _staged_path(row: sqlite3.Row) -> Path | None:
    """The row's staging dir, only if it really is one inside downloads_dir()."""
    stage = _opt_path(row["staged_dir"])
    if stage is None or not stage.is_dir():
        return None
    if not stage.resolve().is_relative_to(downloads_dir().resolve()):
        return None
    return stage


def slug(repo: str) -> str:
    s = repo.split("/", 1)[1].lower().replace(" ", "-")
    # defence in depth: a catalogue row is normally owner/repo from the URL
    # parser, but a separator here would let the ~/.local/bin symlink escape
    return s.replace("/", "-").replace("\\", "-")


def download_asset(url: str, dest: Path, headers: dict | None = None) -> Path:
    """Stream a release asset to disk (GitHub redirects to the CDN).

    The Accept header matters when `url` is the API asset endpoint — without
    `application/octet-stream` GitHub answers with asset JSON instead of the
    file. Harmless on the browser download URL, which redirects to the CDN.
    """
    req = Request(url, headers={"User-Agent": "codehound",
                                "Accept": "application/octet-stream",
                                **(headers or {})})
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    except (OSError, ValueError) as e:
        tmp.unlink(missing_ok=True)
        raise SyncError(f"download failed: {e}") from e
    tmp.rename(dest)
    return dest


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def published_sha256(release: dict, asset_name: str,
                     headers: dict | None = None) -> str:
    """Expected SHA-256 for an asset, from a SHA256SUMS asset or the release
    body; '' when the publisher did not provide one."""
    for a in release.get("assets", []):
        name = (a.get("name") or "").lower()
        href = _asset_href(a)
        if name.startswith("sha256sums") and name.endswith((".txt", ".asc", "")) and href:
            try:
                req = Request(href,
                              headers={"User-Agent": "codehound",
                                       "Accept": "application/octet-stream",
                                       **(headers or {})})
                with urlopen(req, timeout=60) as resp:
                    text = resp.read().decode("utf-8", "replace")
            except (OSError, ValueError, KeyError):
                continue
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].lstrip("*") == asset_name:
                    return parts[0].lower()
    body = release.get("body") or ""
    # <hash>  <name> or <name> ... <hash> lines in release notes
    for line in body.splitlines():
        if asset_name.lower() in line.lower():
            m = _HEX64.search(line)
            if m:
                return m.group(1)
    return ""


def extract_archive(archive: Path, dest: Path) -> None:
    """Unpack a release asset into dest (created). Handles the phase-2 formats."""
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".appimage"):
        shutil.copy2(archive, dest / archive.name)
        (dest / archive.name).chmod(0o755)
        return
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            _safe_zip_members(zf, dest)
        return
    if name.endswith(".rpm"):
        if not (shutil.which("rpm2cpio") and shutil.which("cpio")):
            raise SyncError("rpm payload needs rpm2cpio and cpio")
        run(["sh", "-c",
             f"cd {shlex.quote(str(dest))} && "
             f"rpm2cpio {shlex.quote(str(archive))} | cpio -idm --quiet"])
        return
    if name.endswith(".deb"):
        if not shutil.which("ar"):
            raise SyncError("deb payload needs ar")
        work = dest / "_deb"
        work.mkdir(parents=True, exist_ok=True)
        if _ar_supports_output():
            run(["ar", "x", str(archive), "--output", str(work)])
        else:
            run(["sh", "-c",
                 f"cd {shlex.quote(str(work))} && ar x {shlex.quote(str(archive))}"])
        data = next((f for f in work.iterdir()
                     if f.name.startswith("data.tar")), None)
        if not data:
            raise SyncError("deb has no data.tar member")
        _extract_tar(data, dest)
        safe_rmtree(work, data_roots(), "deb temp dir")
        return
    if ".tar" in name:
        _extract_tar(archive, dest)
        return
    raise SyncError(f"cannot install asset format: {archive.name}")


def _ar_supports_output() -> bool:
    try:
        return "--output" in run(["ar", "--help"], timeout=10)
    except SyncError:
        return False


def _safe_zip_members(zf: zipfile.ZipFile, dest: Path) -> None:
    root = dest.resolve()
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        if not str(target).startswith(str(root)):
            raise SyncError(f"unsafe path in zip: {info.filename}")
        if info.is_dir():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        mode = info.external_attr >> 16
        if mode:
            target.chmod(mode & 0o7777)


def _extract_tar(archive: Path, dest: Path) -> None:
    name = archive.name.lower()
    if name.endswith(".tar.zst"):
        # python tarfile gained zstd only in 3.14 — use bsdtar when available
        if shutil.which("bsdtar"):
            run(["bsdtar", "-xf", str(archive), "-C", str(dest)])
            return
        if "r:zstd" not in getattr(tarfile, "compressions", {}):
            raise SyncError("zstd archives need bsdtar (or Python 3.14+)")
    try:
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as e:
        raise SyncError(f"extract failed: {e}") from e


def find_executable(root: Path, want: str) -> Path | None:
    """Best guess at the runnable app inside an unpacked release.

    Prefers an exact/loose name match (repo name), then a lone executable at
    the top level, then AppRun, then bin/<anything>. Returns None when the
    layout is ambiguous — the caller asks the user to point at it.
    """
    want_l = want.lower().replace("-", "").replace("_", "")
    # real executables first — RPM/DEB payloads often ship dangling /usr/bin
    # symlinks whose target only exists once installed system-wide
    files = [p for p in root.rglob("*")
             if p.is_file() and not p.is_symlink() and os.access(p, os.X_OK)]
    if not files:
        files = [p for p in root.rglob("*") if p.is_file() and os.access(p, os.X_OK)]
    if not files:
        return None
    # strip common single-wrapper dirs (AppImage dirs, <name>-<ver>/)
    def depth(p: Path) -> int:
        return len(p.relative_to(root).parts)
    exact = [p for p in files if p.name.lower().replace("-", "").replace("_", "") == want_l]
    if exact:
        return min(exact, key=depth)
    apprun = [p for p in files if p.name == "AppRun"]
    if apprun:
        return apprun[0]
    top = [p for p in files if depth(p) == 1]
    if len(top) == 1:
        return top[0]
    bins = [p for p in files if p.parent.name == "bin" and depth(p) == 2]
    if len(bins) == 1:
        return bins[0]
    return None


def _existing_target(row: sqlite3.Row) -> Path | None:
    """The executable a previous codeHOUND install (or the clone's app binary)
    put on PATH — replaced in place when it exists."""
    if row["install_exe"]:
        p = Path(row["install_exe"])
        if p.is_file() or p.is_symlink():
            return p
    link = Path.home() / ".local" / "bin" / slug(row["repo"])
    if link.is_file() or link.is_symlink():
        return link
    return None


def install_update(cfg, conn: sqlite3.Connection, repo: str, progress=print,
                   exe_name: str | None = None) -> str:
    """Download and install the matched asset for one repo. Returns a summary.

    Ladder: custom install_path > replace existing managed binary in place >
    managed dir with a ~/.local/bin symlink. Keeps the previous version dir
    for rollback. Raises SyncError on any failure (nothing is moved until the
    download and checksum pass).
    """
    row = _check_installable(conn, repo)
    _drop_staged(conn, row, progress)   # a fresh download supersedes any staged copy
    stage, exe = _fetch_verify_extract(cfg, row, progress, exe_name)
    return _activate(conn, row, stage, exe, progress)


def _check_installable(conn: sqlite3.Connection, repo: str) -> sqlite3.Row:
    row = db.get_update(conn, repo)
    if row is None:
        raise SyncError(f"{repo} is not on the watch list")
    if not row["available"] or not row["asset_url"] or not row["asset_name"]:
        raise SyncError(f"{repo}: no compatible update to install "
                        f"({row['reason'] or 'run updates --check first'})")
    if not row["asset_name"].lower().endswith(_INSTALLABLE):
        raise SyncError(f"{repo}: cannot install {row['asset_name']} yet "
                        "(unsupported asset format)")
    return row


def _stage_dir_for(row: sqlite3.Row) -> Path:
    fname = f"{row['repo'].replace('/', '__')}__{row['asset_name']}"
    return downloads_dir() / f"stage-{fname}"


def _download_verify(cfg, row: sqlite3.Row, progress,
                     keep_archive: bool = True) -> Path:
    """Download the matched asset and verify its published SHA-256. Returns
    the archive path; raises (and deletes the file) on a checksum mismatch."""
    fname = f"{row['repo'].replace('/', '__')}__{row['asset_name']}"
    archive = downloads_dir() / fname
    # the release by tag gives us the API asset href (private repos 404 on the
    # browser URL) and any published SHA-256; if that lookup fails we can still
    # try the browser URL for a public repo, just without a checksum
    try:
        release = _release_by_tag(cfg, row["repo"], row["latest_tag"])
    except SyncError:
        release = None
    href = _asset_href_for(release, row["asset_name"]) or row["asset_url"]
    headers = _auth_headers(cfg)
    progress(f"downloading {row['asset_name']}…")
    download_asset(href, archive, headers)
    try:
        expected = published_sha256(release, row["asset_name"], headers) if release else ""
    except SyncError:
        expected = ""
    actual = sha256_of(archive)
    if expected:
        if actual != expected:
            archive.unlink(missing_ok=True)
            raise SyncError(f"{row['repo']}: SHA-256 mismatch — refusing to install "
                            f"(expected {expected[:12]}…, got {actual[:12]}…)")
        progress("sha256 verified")
    else:
        progress("no published checksum found"
                 + ("" if keep_archive else " — staged unverified"))
    return archive


def _fetch_verify_extract(cfg, row: sqlite3.Row, progress,
                          exe_name: str | None = None,
                          keep_archive: bool = True) -> tuple[Path, Path]:
    """Download the matched asset, verify its checksum, unpack it, and locate
    the executable. Nothing installed is touched at this stage."""
    archive = _download_verify(cfg, row, progress, keep_archive)

    stage = _stage_dir_for(row)
    safe_rmtree(stage, data_roots(), "staging dir")
    stage.mkdir(parents=True)
    extract_archive(archive, stage)
    if not keep_archive:
        archive.unlink(missing_ok=True)
    exe = find_executable(stage, slug(row["repo"]))
    if exe is None and exe_name:
        exe = next((p for p in stage.rglob(exe_name) if p.is_file()), None)
    if exe is None:
        safe_rmtree(stage, data_roots(), "staging dir")
        raise SyncError(f"{row['repo']}: could not find the app executable in "
                        f"{row['asset_name']} — pass --exe NAME")
    return stage, exe


def _activate(conn: sqlite3.Connection, row: sqlite3.Row, stage: Path,
              exe: Path, progress) -> str:
    """Move an unpacked + verified release into place (the install ladder)."""
    custom = (row["install_path"] or "").strip()
    if custom:
        return _install_custom(conn, row, stage, exe, Path(custom), progress)
    existing = _existing_target(row)
    if existing is not None:
        return _install_inplace(conn, row, stage, exe, existing, progress)
    return _install_managed(conn, row, stage, exe, progress)


# ---------------------------------------------------------------- auto-updates

def effective_mode(row: sqlite3.Row, global_mode: str) -> str:
    """'silent' (install now), 'stage' (prepare, ask first) or 'skip'."""
    if row["auto"] == 0:
        return "skip"
    if row["auto"] == 1 or global_mode == "silent":
        return "silent"
    return "stage"


def stage_update(cfg, conn: sqlite3.Connection, repo: str, progress=print) -> str:
    """Download + verify + unpack an update without installing it.

    The unpacked release sits in the downloads dir and the row remembers it
    (`staged_dir`); `activate_staged` moves it into place when the user says
    go. Re-staging a newer release replaces the old staging dir.
    """
    row = _check_installable(conn, repo)
    if row["staged_tag"] == row["latest_tag"] and _staged_path(row):
        return f"{repo} {row['latest_tag']} already staged"
    _drop_staged(conn, row, progress)
    stage, _exe = _fetch_verify_extract(cfg, row, progress, keep_archive=False)
    db.set_staged(conn, repo, row["latest_tag"], str(stage))
    conn.commit()
    progress(f"staged {row['repo']} {row['latest_tag']} — confirm with "
             f"'codehound update {row['repo']}' or click the tray message")
    return f"{row['repo']} {row['latest_tag']} staged (verified, not installed)"


def activate_staged(conn: sqlite3.Connection, repo: str, progress=print) -> str:
    """Install a release that was staged earlier."""
    row = db.get_update(conn, repo)
    if row is None:
        raise SyncError(f"{repo} is not on the watch list")
    stage = _staged_path(row)
    if stage is None:
        raise SyncError(f"{repo}: nothing staged to activate")
    if row["available"] and row["staged_tag"] != row["latest_tag"]:
        raise SyncError(f"{repo}: staged {row['staged_tag']} is stale "
                        f"({row['latest_tag']} is newer) — run 'codehound update {repo}'")
    exe = find_executable(stage, slug(repo))
    if exe is None:
        _drop_staged(conn, row, progress)
        raise SyncError(f"{repo}: staged release has no executable — re-run the check")
    summary = _activate(conn, row, stage, exe, progress)
    db.clear_staged(conn, repo)
    conn.commit()
    return summary


def _drop_staged(conn: sqlite3.Connection, row: sqlite3.Row, progress) -> None:
    """Delete a repo's staging dir + row state (stale or superseded)."""
    stage = _staged_path(row)
    if stage is not None:
        safe_rmtree(stage, data_roots(), "staged release")
        progress(f"dropped staged {row['repo']} {row['staged_tag']}")
    if row["staged_tag"] or row["staged_dir"]:
        db.clear_staged(conn, row["repo"])


def apply_updates(cfg, conn: sqlite3.Connection, progress=print) -> tuple[list[str], list[str], list[str]]:
    """Apply the auto-update policy to every compatible update.

    Per repo: `auto=0` skips, `auto=1` installs silently, otherwise the global
    `[updates] mode` decides — "stage" downloads + verifies and waits for a
    click, "silent" installs straight away. Returns (installed, staged, errors).
    """
    installed: list[str] = []
    staged: list[str] = []
    errors: list[str] = []
    mine = self_repo(cfg)
    for row in db.available_updates(conn):
        if not row["compatible"] or not row["asset_name"].lower().endswith(_INSTALLABLE):
            continue
        if row["repo"] == mine:
            # codeHOUND updating itself needs a quit-and-swap (the app is
            # running); that's `codehound self-update`, not the silent policy pass.
            progress(f"{row['repo']}: self-update runs with 'codehound self-update'")
            continue
        mode = effective_mode(row, cfg.updates_mode)
        if mode == "skip":
            progress(f"{row['repo']}: auto-update is off for this repo")
            continue
        try:
            if mode == "silent":
                installed.append(install_update(cfg, conn, row["repo"], progress))
            else:
                staged.append(stage_update(cfg, conn, row["repo"], progress))
        except SyncError as e:
            errors.append(f"{row['repo']}: {e}")
            progress(f"{row['repo']}: {e}")
    return installed, staged, errors


def _release_by_tag(cfg, repo: str, tag: str) -> dict | None:
    try:
        rel = _api_get(cfg, f"repos/{repo}/releases/tags/{tag}")
        return rel if isinstance(rel, dict) else None
    except SyncError:
        return _latest_release(cfg, repo)


def _finalize(conn, row, mode, install_dir, install_exe, prev_dir, progress):
    db.set_install(conn, row["repo"], mode=mode, install_dir=str(install_dir),
                   install_exe=str(install_exe), prev_dir=str(prev_dir or ""),
                   installed_tag=row["latest_tag"], prev_tag=row["installed_tag"] or "",
                   )
    db.clear_staged(conn, row["repo"])
    conn.commit()
    progress(f"installed {row['repo']} {row['latest_tag']} ({mode}) → {install_exe}")
    return f"{row['repo']} {row['latest_tag']} installed ({mode}): {install_exe}"


def _safe_component(value: str, what: str) -> str:
    """A DB string used as a single path component — reject anything that
    could escape its parent (absolute paths, '..', separators, empties)."""
    v = (value or "").strip()
    if not v or v in (".", "..") or "/" in v or "\\" in v or Path(v).is_absolute():
        raise SyncError(f"unsafe {what} in the catalogue: {value!r}")
    return v


def _safe_repo_parts(repo: str) -> str:
    """owner/repo validated component-by-component; returns it unchanged so
    installs_dir()/repo stays a two-level path with no traversal."""
    parts = (repo or "").strip().split("/")
    if len(parts) != 2:
        raise SyncError(f"unsafe repo in the catalogue: {repo!r}")
    return "/".join(_safe_component(p, "repo") for p in parts)


def _install_managed(conn, row, stage, exe, progress) -> str:
    repo_part = _safe_repo_parts(row["repo"])
    tag_part = _safe_component(row["latest_tag"], "installed tag")
    dest = installs_dir() / repo_part / tag_part
    prev = installs_dir() / repo_part / _safe_component(row["installed_tag"],
                                                        "previous tag") \
        if row["installed_tag"] else None
    safe_rmtree(dest, data_roots(), "previous managed install")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(stage), str(dest))
    rel = exe.relative_to(stage)
    target = dest / rel
    if not target.is_symlink():
        target.chmod(target.stat().st_mode | 0o755)
    link = user_bin_dir() / slug(row["repo"])
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    progress(f"symlinked {link} → {target}")
    return _finalize(conn, row, "managed", dest, link,
                     prev if prev and prev.is_dir() else "", progress)


def _install_inplace(conn, row, stage, exe, existing: Path, progress) -> str:
    """Replace the binary we manage in place (AppImage-style single file)."""
    if existing.is_symlink():
        # symlink into ~/.local/bin: repoint it at a new managed dir instead
        return _install_managed(conn, row, stage, exe, progress)
    backup = existing.with_suffix(existing.suffix + ".codehound-bak")
    shutil.copy2(existing, backup)
    try:
        if not exe.is_symlink() and exe.stat().st_mode & 0o111 == 0:
            exe.chmod(exe.stat().st_mode | 0o755)
        shutil.move(str(exe), str(existing))
    except OSError as e:
        raise SyncError(f"in-place replace failed: {e}") from e
    safe_rmtree(stage, data_roots(), "staging dir")
    progress(f"previous binary kept at {backup.name}")
    return _finalize(conn, row, "inplace", existing.parent, existing, "", progress)


def _install_custom(conn, row, stage, exe, custom: Path, progress) -> str:
    """Install to the user's chosen location (a directory or a file path)."""
    if custom.is_dir():
        target = custom / exe.name
    else:
        target = custom
        target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(target.name + ".codehound-bak")
        shutil.copy2(target, backup)
        progress(f"previous binary kept at {backup.name}")
    shutil.move(str(exe), str(target))
    if not target.is_symlink():
        target.chmod(target.stat().st_mode | 0o755)
    safe_rmtree(stage, data_roots(), "staging dir")
    return _finalize(conn, row, "custom", target.parent, target, "", progress)


def rollback_update(conn: sqlite3.Connection, repo: str, progress=print) -> str:
    """Put the previously installed version back."""
    row = db.get_update(conn, repo)
    if row is None:
        raise SyncError(f"{repo} is not on the watch list")
    mode = row["install_mode"] or ""
    if mode == "managed":
        prev = _opt_path(row["prev_dir"])
        if prev is None or not prev.is_dir():
            raise SyncError(f"{repo}: no previous managed install to roll back to")
        link = Path(row["install_exe"] or (user_bin_dir() / slug(repo)))
        if link.is_symlink() or link.exists():
            link.unlink()
        exe = find_executable(prev, slug(repo)) or next(
            (p for p in prev.rglob("*") if p.is_file() and os.access(p, os.X_OK)), None)
        if exe is None:
            raise SyncError(f"{repo}: previous install has no executable")
        link.symlink_to(exe)
        db.set_install(conn, repo, mode="managed", install_dir=str(prev),
                       install_exe=str(link), prev_dir="",
                       installed_tag=row["prev_tag"] or "", prev_tag="")
        conn.commit()
        progress(f"rolled back {repo} to {row['prev_tag']}")
        return f"{repo} rolled back to {row['prev_tag']}: {link}"
    if mode in ("inplace", "custom"):
        target = _opt_path(row["install_exe"])
        if target is None:
            raise SyncError(f"{repo}: no backup binary to roll back to")
        backup = target.with_name(target.name + ".codehound-bak")
        if not target.is_file() or not backup.is_file():
            raise SyncError(f"{repo}: no backup binary to roll back to")
        shutil.move(str(backup), str(target))
        db.set_install(conn, repo, mode=mode, install_dir=str(target.parent),
                       install_exe=str(target), prev_dir="",
                       installed_tag=row["prev_tag"] or "", prev_tag="")
        conn.commit()
        progress(f"rolled back {repo} to {row['prev_tag']}")
        return f"{repo} rolled back to {row['prev_tag']}: {target}"
    if mode == "system":
        raise SyncError(f"{repo}: installed by your package manager — roll back "
                        "with it (dnf downgrade / apt install)")
    raise SyncError(f"{repo}: nothing installed by codeHOUND to roll back")


# ------------------------------------------------------- system package installs

# Package managers we can drive through pkexec, in preference order per format.
_PACKAGE_INSTALLERS = {
    ".rpm": [("dnf", "install", "-y"), ("yum", "install", "-y"),
             ("rpm", "-U", "--replacepkgs")],
    ".deb": [("apt-get", "install", "-y"), ("apt", "install", "-y"),
             ("dnf", "install", "-y"), ("dpkg", "-i")],
}


def _package_installer(archive: Path) -> list[str]:
    """The first installed command that can install this package format."""
    for ext, cmds in _PACKAGE_INSTALLERS.items():
        if archive.name.lower().endswith(ext):
            for cmd in cmds:
                if shutil.which(cmd[0]):
                    return list(cmd)
    raise SyncError(f"no package manager found for {archive.name} "
                    "(tried dnf, yum, apt, dpkg, rpm)")


def system_package_install(cfg, conn: sqlite3.Connection, repo: str,
                           progress=print) -> str:
    """Download + verify the matched RPM/DEB and install it system-wide via pkexec.

    The distro package manager owns the files afterwards, so `--rollback`
    defers to it (`dnf downgrade` / `apt install`) rather than to our
    prev_dir bookkeeping. The archive is deleted once the install succeeds.
    """
    row = _check_installable(conn, repo)
    if not row["asset_name"].lower().endswith((".rpm", ".deb")):
        raise SyncError(f"{repo}: {row['asset_name']} is not an RPM or DEB — "
                        "system installs need a distribution package")
    if shutil.which("pkexec") is None:
        raise SyncError("pkexec not found — system installs need a polkit agent")
    _drop_staged(conn, row, progress)
    archive = _download_verify(cfg, row, progress)
    cmd = ["pkexec", *_package_installer(archive), str(archive)]
    progress(f"installing system-wide: {' '.join(cmd)}")
    try:
        run(cmd, timeout=1800)
    except SyncError as e:
        raise SyncError(f"system install failed: {e}") from e
    archive.unlink(missing_ok=True)
    exe = Path("/usr/bin") / slug(repo)
    return _finalize(conn, row, "system", Path("/usr/bin"), exe, "", progress)


# ---------------------------------------------------------------- self-update

def self_repo(cfg) -> str:
    """The owner/repo codeHOUND self-updates from ('' = self-update off)."""
    return (getattr(cfg, "self_repo", "") or "").strip()


def check_self_update(cfg, conn: sqlite3.Connection,
                      progress=print) -> sqlite3.Row | None:
    """Refresh the watch row for codeHOUND's own release feed.

    The row's `current_tag` is the *installed* version, not a clone's tag —
    self-update cares about what is running, not what is checked out. Returns
    the row, or None when self-update is off or the repo publishes nothing.
    """
    repo = self_repo(cfg)
    if not repo:
        return None
    try:
        release = _latest_release(cfg, repo)
    except SyncError as e:
        progress(f"  {repo}: self-update check failed — {e}")
        return None
    if release is None:
        return None
    tag = release.get("tag_name") or ""
    asset, reason = pick_asset(release.get("assets", []))
    is_new = newer(tag, __version__)
    db.upsert_update(conn, {
        "repo": repo, "path": "", "name": "codeHOUND",
        "current_tag": __version__, "latest_tag": tag,
        "published_at": release.get("published_at"),
        "asset_name": (asset or {}).get("name"),
        "asset_url": (asset or {}).get("browser_download_url"),
        "available": 1 if is_new else 0,
        "compatible": 1 if (is_new and asset) else 0,
        "reason": "" if (is_new and asset) else (
            "up to date" if not is_new else reason),
        "last_checked": db.now()})
    conn.commit()
    return db.get_update(conn, repo)


def self_update(cfg, conn: sqlite3.Connection, progress=print,
                stage_only: bool = False) -> str:
    """Update codeHOUND itself from its own GitHub releases.

    RPM/DEB releases install system-wide through pkexec — the running process
    keeps its old files until it exits, so the new build only starts after a
    relaunch (`schedule_relaunch` arranges that for the dashboard). Other
    formats can't replace a running install, so they stage for a manual swap.
    """
    repo = self_repo(cfg)
    if not repo:
        raise SyncError("self-update is off — set [updates] self_repo in config.toml")
    row = db.get_update(conn, repo)
    if row is None or not row["available"]:
        row = check_self_update(cfg, conn, progress) or row
    if row is None:
        raise SyncError(f"{repo}: no published releases to update from")
    if not row["available"]:
        return f"codeHOUND {__version__} is already the newest release"
    name = row["asset_name"].lower()
    if stage_only or not name.endswith((".rpm", ".deb")):
        if not name.endswith(_INSTALLABLE):
            raise SyncError(f"{repo}: {row['asset_name']} cannot be installed "
                            "automatically")
        return stage_update(cfg, conn, repo, progress)
    summary = system_package_install(cfg, conn, repo, progress)
    return summary + " — restart codeHOUND to run the new version"


def schedule_relaunch(when_pid: int | None = None,
                      launch: list[str] | None = None, progress=print) -> str:
    """Detach a helper that waits for `when_pid` to exit, then starts codeHOUND again.

    Used after a system self-update so the dashboard can quit and come back
    on the new build. The helper is a plain detached shell loop — it creates
    and deletes no files, so it never touches safe_rmtree territory.
    """
    pid = when_pid or os.getpid()
    cmd = launch or [sys.executable, "-m", "codehound.cli", "app"]
    quoted = " ".join(shlex.quote(a) for a in cmd)
    script = (f"while kill -0 {pid} 2>/dev/null; do sleep 0.5; done; "
              f"exec {quoted}")
    subprocess.Popen(["sh", "-c", script], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    progress(f"relaunch scheduled after pid {pid} exits: {quoted}")
    return f"relaunch scheduled: {quoted}"


def check_updates(cfg, conn: sqlite3.Connection, progress=print) -> list[sqlite3.Row]:
    """Refresh the updates table for every watched repo; returns available rows.

    Individual repo failures (404, rate limit) are reported and skipped — one
    unreachable repo must not sink the whole check.
    """
    authed = _is_authed(cfg)
    targets = watch_targets(conn)
    if not targets:
        progress("no catalogued projects with a GitHub remote — nothing to watch")
    mine = self_repo(cfg)
    for t in targets:
        if t.repo == mine:
            continue   # codeHOUND's own row is owned by check_self_update
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
