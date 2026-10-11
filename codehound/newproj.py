"""Create, clone, and branch projects — the write side of codeHOUND.

These functions touch disk and run real git commands, so the dashboard reaches
them through `codehound new|clone|worktree` (see cli.py) and streams the
progress lines into its Activity dock, exactly like adopt and sync do. When an
operation succeeds the new directory is written straight into the catalogue
database, so it shows up in the dashboard without waiting for the next scan.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from . import db, gitinfo, scanner
from .ai import AIError, get_backend, is_configured
from .ai.base import strip_think
from .config import Config
from .providers.base import SyncError, run

CLONE_TIMEOUT = 1800   # a big clone over a slow link is normal
MAX_FILES = 25         # scaffold guardrails
MAX_FILE_CHARS = 20000
MAX_TOTAL_CHARS = 120000

_REMOTE_RE = re.compile(r"^(?:https?://|ssh://|git://|git@[\w.-]+:)")


class NewProjectError(Exception):
    """A create/clone/worktree failure with a user-readable message."""


# ----- git helpers -------------------------------------------------------

def _git(path: str, *args: str, timeout: int = 600) -> str:
    """Run git in `path`; raise NewProjectError carrying git's own complaint."""
    try:
        return run(["git", "-C", str(path), *args], timeout=timeout)
    except SyncError as e:
        raise NewProjectError(f"git {' '.join(args)}: {e}") from e


def _git_out(path: str, *args: str) -> str | None:
    """Run git in `path`, returning stdout or None — for probes, no exception."""
    try:
        r = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True, text=True, timeout=gitinfo.GIT_TIMEOUT, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _git_ok(path: str, *args: str) -> bool:
    """True when the git command exits 0 — for probes, where failure is normal."""
    return _git_out(path, *args) is not None


def _stream(cmd: list[str], progress, timeout: int = CLONE_TIMEOUT) -> None:
    """Run cmd, handing each output line to progress; raise on non-zero exit.

    git clone/fetch write their progress to stderr, so both channels merge.
    """
    say = progress or (lambda m: None)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except FileNotFoundError as exc:
        raise NewProjectError(f"command not found: {cmd[0]}") from exc
    with proc.stdout:
        for line in proc.stdout:
            line = line.strip("\r\n")
            if line.strip():
                say(line)
    if proc.wait(timeout=timeout) != 0:
        raise NewProjectError(f"{' '.join(cmd[:2])} failed")


def default_branch(path: str) -> str:
    """The repo's default branch: origin/HEAD, else the checked-out branch."""
    ref = _git_out(path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if ref and ref.startswith("refs/remotes/origin/"):
        return ref[len("refs/remotes/origin/"):]
    return _git_out(path, "rev-parse", "--abbrev-ref", "HEAD") or "main"


# ----- naming ------------------------------------------------------------

def unique_name(dest_dir: Path, base: str) -> str:
    """Filesystem-safe name under dest_dir: base, then base-2, base-3…"""
    clean = re.sub(r"[^\w.-]+", "-", (base or "").strip()).strip("-.") or "project"
    if not (dest_dir / clean).exists():
        return clean
    n = 2
    while (dest_dir / f"{clean}-{n}").exists():
        n += 1
    return f"{clean}-{n}"


def _dest_root(dest: str | None, fallback: str) -> Path:
    root = Path(dest or fallback).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise NewProjectError(f"cannot create {root}: {e}") from e
    return root


def _name_from_source(source: str) -> str:
    """Repo name out of a URL or path: strip the scheme and any .git suffix."""
    tail = source.rstrip("/")
    if _REMOTE_RE.match(source) and ":" in source and not tail.startswith(("http://", "https://", "ssh://", "git://")):
        tail = tail.split(":")[-1]
    else:
        tail = tail.split("/")[-1]
    return re.sub(r"\.git$", "", tail) or "project"


# ----- catalogue registration -------------------------------------------

def register(cfg: Config, path: str, progress=None) -> None:
    """Add a freshly created directory to the catalogue without a full scan.

    Builds the row exactly as the scanner would (scanner._make_project) so the
    dashboard sees the same shape it always does. A failure here is reported but
    never fails the operation — the next scan picks the project up anyway.
    """
    say = progress or (lambda m: None)
    norm = os.path.normpath(os.path.abspath(path))
    try:
        with os.scandir(norm) as it:
            entries = {e.name: e for e in it}
        roots = [os.path.normpath(os.path.expanduser(r)) for r in cfg.roots]
        root = max(
            (r for r in roots if norm.startswith(r + os.sep)), key=len,
            default=os.path.dirname(norm),
        )
        proj = scanner.make_project(norm, "git", root, entries, set(cfg.exclude_dirs))
        conn = db.connect()
        try:
            db.upsert_project(conn, proj)
            conn.commit()
        finally:
            conn.close()
        say(f"catalogued {norm}")
    except OSError as e:
        say(f"catalogue entry skipped ({e}) — it will appear on the next scan")


# ----- create ------------------------------------------------------------

SYSTEM_SCAFFOLD = (
    "You are the scaffolder inside codeHOUND, a tool that manages a developer's "
    "projects. Reply with ONLY a JSON object mapping relative file paths to their "
    "full text content — no prose, no markdown fences, no commentary. Paths must "
    "be relative (never absolute, never '..') and never inside .git. Produce 3 to "
    "12 small, coherent, runnable files that match the project described, "
    "including a README.md."
)


def llm_scaffold(cfg: Config, name: str, description: str | None, progress=None) -> dict[str, str]:
    """Ask the configured model for a file map. Raises AIError if unusable."""
    say = progress or (lambda m: None)
    if not is_configured(cfg):
        raise AIError("no AI provider configured (Settings → AI)")
    backend = get_backend(cfg)
    prompt = (
        f"Project name: {name}\n"
        f"What it should be: {(description or '').strip() or 'a small, clean starting point'}\n\n"
        "Return the file map now."
    )
    say(f"asking {cfg.ai_model or 'the configured model'} to scaffold {name}…")
    files = _parse_file_map(backend.ask(SYSTEM_SCAFFOLD, prompt))
    if not files:
        raise AIError("the model returned no usable files")
    say(f"scaffold: {len(files)} file(s)")
    return files


def _safe_relpath(rel: str) -> str | None:
    """Normalise a model-supplied path, rejecting anything that escapes the project."""
    if not isinstance(rel, str) or not rel.strip():
        return None
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return None
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p == ".git" or p.startswith(".git/") for p in parts):
        return None
    return "/".join(parts)


def _parse_file_map(text: str) -> dict[str, str]:
    """Pull the outermost JSON object out of a model reply, with guardrails."""
    body = strip_think(text or "")
    fence = re.search(r"```(?:json)?\s*(.+?)```", body, re.DOTALL)
    if fence:
        body = fence.group(1)
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        data = json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    total = 0
    for key, value in data.items():
        safe = _safe_relpath(key)
        if safe is None or not isinstance(value, str) or safe in out:
            continue
        value = value[:MAX_FILE_CHARS]
        total += len(value)
        if total > MAX_TOTAL_CHARS or len(out) >= MAX_FILES:
            break
        out[safe] = value
    return out


def _readme(name: str, description: str | None) -> str:
    text = f"# {name}\n"
    body = (description or "").strip()
    if body:
        text += f"\n{body}\n"
    return text


def _write_files(target: Path, files: dict[str, str], say) -> None:
    for rel, content in files.items():
        safe = _safe_relpath(rel)
        if safe is None:
            say(f"  skipped unsafe path: {rel!r}")
            continue
        path = target / safe
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        say(f"  wrote {safe}")


def create_project(cfg: Config, name: str, description: str | None = None,
                   scaffold: bool | str = False, dest: str | None = None,
                   progress=None) -> str:
    """Create dest/name as a fresh git repo. Returns the new path.

    With `scaffold`, the configured model writes the starter files; any AI or
    parse failure falls back to the plain README scaffold rather than failing.
    """
    say = progress or (lambda m: None)
    root = _dest_root(dest, cfg.adopt_dest)
    target = root / unique_name(root, name)
    say(f"creating {target}")
    try:
        target.mkdir(parents=True)
    except OSError as e:
        raise NewProjectError(f"cannot create {target}: {e}") from e

    files: dict[str, str] = {}
    if scaffold:
        try:
            files = llm_scaffold(cfg, target.name, description, progress=say)
        except AIError as e:
            say(f"scaffold skipped: {e}")
    readme = files.pop("README.md", None) or _readme(target.name, description)
    _write_files(target, {"README.md": readme, **files}, say)

    if not _git_ok(str(target), "init", "-b", "main"):
        _git(str(target), "init", timeout=60)
    _git(str(target), "add", "-A", timeout=60)
    try:
        # The first commit of a brand-new repo must not trip the user's own
        # commit hooks (a global "no direct commits to main" guard would
        # block it), so disable hooks for this one commit.
        _git(str(target), "-c", "core.hooksPath=", "commit",
             "-m", f"Initial commit: {target.name}", timeout=60)
    except NewProjectError as e:
        # Never invent a git identity for the user — leave the tree staged.
        say(f"initial commit skipped (set user.name / user.email in git): {e}")
    register(cfg, str(target), progress=say)
    return str(target)


# ----- clone -------------------------------------------------------------

def clone_project(cfg: Config, source: str, name: str | None = None,
                  dest: str | None = None, progress=None) -> str:
    """Clone a remote URL or a local repository into dest. Returns the new path."""
    say = progress or (lambda m: None)
    src = (source or "").strip()
    if not src:
        raise NewProjectError("nothing to clone — a git URL or a local repo path is required")
    if not _REMOTE_RE.match(src) and not src.endswith(".git"):
        local = Path(src).expanduser()
        if not local.is_dir():
            raise NewProjectError(f"{src} is neither a git URL nor an existing directory")
        local = local.resolve()
        if not _git_ok(str(local), "rev-parse", "--git-dir"):
            raise NewProjectError(f"{local} is not a git repository")
        src = str(local)
    root = _dest_root(dest, cfg.clone_dest)
    target = root / unique_name(root, name or _name_from_source(src))
    say(f"cloning {src} → {target}")
    _stream(["git", "clone", "--progress", src, str(target)], progress=say)
    register(cfg, str(target), progress=say)
    return str(target)


# ----- worktree ----------------------------------------------------------

def add_worktree(cfg: Config, src_path: str, branch: str,
                 dest: str | None = None, progress=None) -> str:
    """Add a worktree for `branch` beside the project at src_path. Returns its path.

    An existing local branch is checked out as-is; a branch that exists only on
    origin gets a tracking branch; anything else branches from the repo's
    default branch (falling back to HEAD when offline).
    """
    say = progress or (lambda m: None)
    src = Path(src_path).expanduser().resolve()
    if not _git_ok(str(src), "rev-parse", "--git-dir"):
        raise NewProjectError(f"{src} is not a git repository")
    branch = (branch or "").strip()
    if subprocess.run(["git", "check-ref-format", "--branch", branch],
                      capture_output=True, check=False).returncode != 0:
        raise NewProjectError(f"{branch!r} is not a valid branch name")

    parent = Path(dest).expanduser() if dest else src.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise NewProjectError(f"cannot create {parent}: {e}") from e
    target = parent / unique_name(parent, f"{src.name}-{branch.replace('/', '-')}")
    if target.exists():
        raise NewProjectError(f"{target} already exists")

    say(f"fetching {src.name}…")
    try:
        _git(str(src), "fetch", "--all", "--prune", timeout=CLONE_TIMEOUT)
    except NewProjectError as e:
        say(f"fetch skipped: {e}")

    if _git_ok(str(src), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
        say(f"adding a worktree on existing branch {branch}")
        _git(str(src), "worktree", "add", str(target), branch, timeout=300)
    elif _git_ok(str(src), "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"):
        say(f"tracking origin/{branch}")
        _git(str(src), "branch", "--track", branch, f"origin/{branch}", timeout=60)
        _git(str(src), "worktree", "add", str(target), branch, timeout=300)
    else:
        base = default_branch(str(src))
        if not _git_ok(str(src), "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{base}"):
            base = "HEAD"
        say(f"branching {branch} from {base}")
        _git(str(src), "worktree", "add", "-b", branch, str(target), base, timeout=300)

    register(cfg, str(target), progress=say)
    return str(target)
