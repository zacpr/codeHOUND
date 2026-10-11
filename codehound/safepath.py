"""One door for every destructive delete in codeHOUND.

Every recursive delete in the app goes through :func:`safe_rmtree`, which
refuses unless the path passes BOTH checks:

1. **Blacklist** — filesystem roots, system directories and the user's home
   directory (per-OS lists below). This caps the blast radius even if a
   containment root is computed wrongly.
2. **Containment** — the path must sit *strictly inside* one of the caller's
   allowed roots (its own data dirs, the configured clone destination, ...),
   resolved through symlinks so a staging dir cannot point somewhere else.

An empty, blank or relative path is always refused. ``Path("")`` is the
current directory, and that is exactly how the checkout was wiped on
2026-10-11: a NULL DB column became ``Path("")`` and ``rmtree`` deleted
whatever cwd the process happened to run in (under the systemd timer, $HOME).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a delete is refused. Subclasses ValueError so callers that
    already guard on ValueError keep working."""


# Directories that must never be removed, whatever a caller claims.
_POSIX_SYSTEM = (
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64",
    "/media", "/mnt", "/opt", "/proc", "/root", "/run", "/sbin", "/srv",
    "/sys", "/tmp", "/usr", "/var", "/snap",
)
_MACOS_SYSTEM = (
    "/Applications", "/Library", "/System", "/Users", "/Volumes",
    "/cores", "/net", "/private",
)


def _forbidden_posix() -> set[Path]:
    names = list(_POSIX_SYSTEM)
    if os.uname().sysname == "Darwin":
        names += list(_MACOS_SYSTEM)
    names.append(str(Path.home()))
    return {Path(n).resolve() for n in names}


def _forbidden_windows() -> set[Path]:
    drives = [d for d in "CDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{d}:\\").exists()]
    names = ["Windows", "Program Files", "Program Files (x86)", "ProgramData",
             "PerfLogs", "Users", "Recovery", "$Recycle.Bin"]
    out: set[Path] = {Path(f"{d}:\\") for d in drives}
    out |= {Path(f"{d}\\{n}") for d in drives for n in names}
    if sysroot := os.environ.get("SystemRoot"):
        out.add(Path(sysroot))
    out.add(Path.home())
    return {p.resolve() if p.exists() else p for p in out}


def forbidden_dirs() -> set[Path]:
    """The protected directories for the current OS, fully resolved."""
    return _forbidden_windows() if os.name == "nt" else _forbidden_posix()


def _under_foreign_home(resolved: Path) -> bool:
    """True if the path sits in some *other* user's home (/home/x, /Users/x).

    codeHOUND only ever writes under its own home, so a delete aimed at a
    neighbour's tree is always a bug — even when a caller wrongly roots at
    /home itself.
    """
    if os.name == "nt":
        return False
    home = Path.home().resolve()
    for home_root in (Path("/home"), Path("/Users")):
        if resolved.is_relative_to(home_root) and not resolved.is_relative_to(home):
            return True
    return False


def check_path(path, roots, label: str = "") -> Path:
    """Resolve ``path`` and prove it is safe to delete.

    ``roots`` are the only directories whose *contents* codeHOUND may ever
    remove. Returns the resolved path; raises :class:`UnsafePathError` if the
    path is empty, relative, protected, a root itself, or outside every root.
    """
    where = f"{label}: " if label else ""
    raw = str(path).strip()
    if not raw:
        raise UnsafePathError(
            f"{where}empty path means the current directory — refusing")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise UnsafePathError(
            f"{where}relative path {raw!r} depends on the current "
            "directory — refusing")
    resolved = p.resolve()
    if resolved in forbidden_dirs():
        raise UnsafePathError(f"{where}{resolved} is a protected directory")
    if _under_foreign_home(resolved):
        raise UnsafePathError(
            f"{where}{resolved} belongs to another user's home directory")
    allowed = [Path(r).expanduser().resolve() for r in roots]
    for root in allowed:
        if resolved == root:
            raise UnsafePathError(
                f"{where}refusing to delete {root} itself, only its contents")
        if resolved.is_relative_to(root):
            return resolved
    raise UnsafePathError(
        f"{where}{resolved} is outside "
        + " and ".join(str(r) for r in allowed))


def safe_rmtree(path, roots, label: str = "") -> None:
    """Remove a directory tree, but only if :func:`check_path` allows it.

    Unlike ``shutil.rmtree(..., ignore_errors=True)`` a refusal or a failure
    raises, so a bug surfaces as an error instead of silent data loss.
    """
    target = check_path(path, roots, label)
    if not target.exists():
        return
    if not target.is_dir():
        raise UnsafePathError(f"{label}: {target} is not a directory")
    shutil.rmtree(target)
