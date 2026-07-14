"""Load and create codeHOUND configuration (~/.config/codehound/config.toml)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "codehound"
CONFIG_PATH = CONFIG_DIR / "config.toml"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "codehound"
DB_PATH = DATA_DIR / "codehound.db"

DEFAULT_CONFIG = """\
# codeHOUND configuration

[scan]
roots = [
    "{home}",
    "/data/1tb",
    "/data/2tb",
]
# Directory names pruned anywhere in the tree
exclude_dirs = [
    "node_modules", ".cache", ".venv", "venv", "__pycache__", ".local",
    "snap", ".cargo", ".rustup", ".npm", ".nvm", "Steam", ".steam",
    ".mozilla", ".thunderbird", ".var", "flatpak", ".gradle", ".m2",
    "site-packages", ".Trash-1000", "lost+found",
]
# Absolute paths pruned exactly (add slow or irrelevant subtrees here)
exclude_paths = []
max_depth = 8
one_filesystem = true   # don't cross mount points inside a root

[ollama]
enabled = true
url = "http://localhost:11434"
model = "gemma4:12b"
enrich = true       # one-line project descriptions
classify = false    # LLM decides whether ambiguous dirs are software projects
timeout = 30        # seconds per request

[launchers]  # ordered; {{path}} is substituted; shown in context menu
dolphin = "dolphin {{path}}"
vscode = "code {{path}}"
antigravity = "antigravity {{path}}"
kimi = "konsole --workdir {{path}} -e kimi"
konsole = "konsole --workdir {{path}}"

[adopt]
dest = "{home}/repos"
"""


@dataclass
class Config:
    roots: list[str]
    exclude_dirs: list[str]
    exclude_paths: list[str]
    max_depth: int
    one_filesystem: bool
    ollama_enabled: bool
    ollama_url: str
    ollama_model: str
    ollama_enrich: bool
    ollama_classify: bool
    ollama_timeout: int
    launchers: dict[str, str] = field(default_factory=dict)
    adopt_dest: str = str(Path.home() / "repos")


def ensure_config() -> Path:
    """Create the default config file if it doesn't exist; return its path."""
    if not CONFIG_PATH.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(DEFAULT_CONFIG.format(home=Path.home()))
    return CONFIG_PATH


def load_config() -> Config:
    ensure_config()
    with open(CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f)
    scan = raw.get("scan", {})
    ollama = raw.get("ollama", {})
    adopt = raw.get("adopt", {})
    return Config(
        roots=scan.get("roots", [str(Path.home())]),
        exclude_dirs=scan.get("exclude_dirs", []),
        exclude_paths=scan.get("exclude_paths", []),
        max_depth=scan.get("max_depth", 8),
        one_filesystem=scan.get("one_filesystem", True),
        ollama_enabled=ollama.get("enabled", True),
        ollama_url=ollama.get("url", "http://localhost:11434"),
        ollama_model=ollama.get("model", "gemma4:12b"),
        ollama_enrich=ollama.get("enrich", True),
        ollama_classify=ollama.get("classify", False),
        ollama_timeout=ollama.get("timeout", 30),
        launchers=raw.get("launchers", {}),
        adopt_dest=adopt.get("dest", str(Path.home() / "repos")),
    )
