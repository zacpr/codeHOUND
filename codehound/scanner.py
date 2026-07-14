"""Filesystem walk that hunts git repos and un-gitted software projects."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import db, gitinfo
from .config import Config

# marker filename (lowercased) -> language; suffix markers checked separately
MARKERS = {
    "package.json": "JS/TS",
    "pyproject.toml": "Python",
    "setup.py": "Python",
    "requirements.txt": "Python",
    "cargo.toml": "Rust",
    "go.mod": "Go",
    "cmakelists.txt": "C/C++",
    "makefile": "C/C++",
    "meson.build": "C/C++",
    "pom.xml": "Java",
    "build.gradle": "Java",
    "build.gradle.kts": "Kotlin",
    "composer.json": "PHP",
    "gemfile": "Ruby",
    "mix.exs": "Elixir",
    "pubspec.yaml": "Dart",
    "docker-compose.yml": "Docker",
    "docker-compose.yaml": "Docker",
}
SUFFIX_MARKERS = {
    ".sln": "C#",
    ".csproj": "C#",
    ".gemspec": "Ruby",
    ".cabal": "Haskell",
}
# loose code files that make a marker-less dir a classification candidate
CODE_SUFFIXES = {".py", ".sh", ".js", ".ts", ".rs", ".go", ".c", ".cpp", ".java", ".rb", ".php", ".ps1"}

MAX_STAT_FILES = 800   # cap per-project mtime/size walk
STAT_DEPTH = 3


@dataclass
class ScanResult:
    projects: list[dict] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)  # ambiguous dirs for LLM classification
    errors: list[str] = field(default_factory=list)
    dirs_walked: int = 0
    duration: float = 0.0


def _detect_language(entries: dict[str, os.DirEntry]) -> str | None:
    lower = {n.lower() for n in entries}
    for name, lang in MARKERS.items():
        if name in lower:
            return lang
    for name in lower:
        for suf, lang in SUFFIX_MARKERS.items():
            if name.endswith(suf):
                return lang
    return None


def _project_stats(path: str, exclude: set[str]) -> tuple[str | None, int]:
    """Bounded walk: newest file mtime (ISO) and approximate size in bytes."""
    newest, total, seen = 0.0, 0, 0
    stack = [(path, 0)]
    while stack and seen < MAX_STAT_FILES:
        d, depth = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if seen >= MAX_STAT_FILES:
                        break
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if depth < STAT_DEPTH and e.name not in exclude and e.name != ".git":
                                stack.append((e.path, depth + 1))
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            newest = max(newest, st.st_mtime)
                            total += st.st_size
                            seen += 1
                    except OSError:
                        continue
        except OSError:
            continue
    mtime = datetime.fromtimestamp(newest).astimezone().isoformat(timespec="seconds") if newest else None
    return mtime, total


def _make_project(path: str, kind: str, root: str, entries: dict[str, os.DirEntry],
                  exclude: set[str]) -> dict:
    proj = {
        "path": path,
        "kind": kind,
        "name": os.path.basename(path.rstrip("/")),
        "root": root,
        "language": _detect_language(entries),
        "branch": None, "remote_url": None, "last_commit_date": None,
        "last_commit_msg": None, "dirty": 0, "created": None,
    }
    proj["modified"], proj["size_approx"] = _project_stats(path, exclude)
    if kind == "git":
        proj.update({k: v for k, v in gitinfo.collect(path).items() if v is not None})
        if proj.get("last_commit_date") and not proj["modified"]:
            proj["modified"] = proj["last_commit_date"]
    if not proj.get("created"):
        try:
            st = os.stat(path)
            birth = getattr(st, "st_birthtime", None) or st.st_ctime
            proj["created"] = datetime.fromtimestamp(birth).astimezone().isoformat(timespec="seconds")
        except OSError:
            pass
    return proj


def scan(cfg: Config, roots: list[str] | None = None, verbose: bool = False,
         progress=None) -> ScanResult:
    """Walk roots, detect projects. `progress(msg)` is called with status lines."""
    result = ScanResult()
    t0 = time.monotonic()
    exclude = set(cfg.exclude_dirs)
    exclude_paths = {os.path.normpath(p) for p in cfg.exclude_paths}
    say = progress or (lambda m: None)

    for root in roots or cfg.roots:
        root = os.path.normpath(os.path.expanduser(root))
        if not os.path.isdir(root):
            result.errors.append(f"root not found: {root}")
            continue
        say(f"scanning {root}")
        try:
            root_dev = os.stat(root).st_dev
        except OSError as e:
            result.errors.append(f"{root}: {e}")
            continue

        stack: list[tuple[str, int]] = [(root, 0)]
        while stack:
            dirpath, depth = stack.pop()
            result.dirs_walked += 1
            if result.dirs_walked % 5000 == 0:
                say(f"  … {result.dirs_walked} dirs, {len(result.projects)} projects")
            try:
                with os.scandir(dirpath) as it:
                    entries = {e.name: e for e in it}
            except OSError as e:
                result.errors.append(f"{dirpath}: {e}")
                continue

            kind = None
            if ".git" in entries:  # dir for normal clones, file for worktrees/submodules
                kind = "git"
            elif gitinfo.is_bare_repo(Path(dirpath)) and dirpath != root:
                kind = "bare"
            elif _detect_language(entries) and dirpath != root:
                kind = "ungitted"

            if kind:
                proj = _make_project(dirpath, kind, root, entries, exclude)
                result.projects.append(proj)
                if verbose:
                    say(f"  [{kind}] {dirpath}")
                continue  # prune: don't descend into a found project

            has_code = any(
                e.is_file(follow_symlinks=False) and os.path.splitext(n)[1] in CODE_SUFFIXES
                for n, e in entries.items()
            )
            subdirs = []
            for name, e in entries.items():
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if name in exclude or name.startswith(".") and name not in (".config",):
                    continue
                if os.path.normpath(e.path) in exclude_paths:
                    continue
                if cfg.one_filesystem:
                    try:
                        if e.stat(follow_symlinks=False).st_dev != root_dev:
                            continue
                    except OSError:
                        continue
                subdirs.append(e.path)

            if has_code and dirpath != root:
                result.candidates.append(dirpath)
            if depth < cfg.max_depth:
                stack.extend((d, depth + 1) for d in subdirs)

    result.duration = time.monotonic() - t0
    return result


def run_scan(cfg: Config, roots: list[str] | None = None, verbose: bool = False,
             progress=None) -> ScanResult:
    """Scan and persist to the database."""
    conn = db.connect()
    scanned_roots = [os.path.normpath(os.path.expanduser(r)) for r in (roots or cfg.roots)]
    scan_id = db.start_scan(conn, scanned_roots)
    conn.commit()
    result = scan(cfg, roots, verbose, progress)
    seen = set()
    for proj in result.projects:
        db.upsert_project(conn, proj)
        seen.add(proj["path"])
    gone = db.mark_missing(conn, scanned_roots, seen)
    db.finish_scan(conn, scan_id, len(result.projects), result.errors)
    conn.commit()
    conn.close()
    if progress:
        progress(
            f"done: {len(result.projects)} projects ({gone} now missing), "
            f"{result.dirs_walked} dirs in {result.duration:.1f}s, {len(result.errors)} errors"
        )
    return result
