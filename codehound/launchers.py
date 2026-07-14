"""Launch configured applications at a project path."""
from __future__ import annotations

import shlex
import subprocess

from .config import Config


def launch(cfg: Config, launcher: str, path: str) -> None:
    template = cfg.launchers[launcher]
    cmd = [
        part.replace("{path}", path)
        for part in shlex.split(template)
    ]
    subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def launcher_names(cfg: Config) -> list[str]:
    return list(cfg.launchers)
