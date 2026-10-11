"""Copy a project into the organised destination (~/repos) under a unique name."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from . import db
from .config import Config
from .safepath import safe_rmtree


def suggest_name(cfg: Config, src: str) -> str:
    """Unique destination name: basename, then basename-<parent>, then -2, -3…"""
    dest = Path(cfg.adopt_dest)
    base = re.sub(r"[^\w.-]+", "-", os.path.basename(src.rstrip("/"))) or "project"
    if not (dest / base).exists():
        return base
    parent = re.sub(r"[^\w.-]+", "-", os.path.basename(os.path.dirname(src.rstrip("/"))))
    if parent and not (dest / f"{base}-{parent}").exists():
        return f"{base}-{parent}"
    n = 2
    while (dest / f"{base}-{n}").exists():
        n += 1
    return f"{base}-{n}"


def adopt(cfg: Config, src: str, name: str, progress=None) -> str:
    """Copy src into adopt_dest/name. Returns the destination path."""
    say = progress or (lambda m: None)
    dest_dir = Path(cfg.adopt_dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    src = os.path.normpath(src)
    if os.path.normpath(str(dest_dir)) == src or str(dest_dir).startswith(src + os.sep):
        raise ValueError("destination is inside the source directory")

    say(f"copying {src} → {dest}")
    if shutil.which("rsync"):
        r = subprocess.run(
            ["rsync", "-a", src + "/", str(dest) + "/"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            safe_rmtree(dest, (dest_dir,), "adopt rollback")
            raise RuntimeError(f"rsync failed: {r.stderr.strip()[:500]}")
    else:
        shutil.copytree(src, dest, symlinks=True)

    conn = db.connect()
    db.set_adopted(conn, src, str(dest))
    conn.commit()
    conn.close()
    say("copy complete")
    return str(dest)
