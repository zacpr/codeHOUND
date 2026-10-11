"""Load, create and save codeHOUND configuration (~/.config/codehound/config.toml)."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .providers import PROVIDERS

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
    "checkpoints",   # AI-coding extension task snapshots (Roo Code, Kilo Code, …)
]
# Absolute paths pruned exactly (add slow or irrelevant subtrees here)
exclude_paths = [
    # AI tool / editor app data — configuration and caches, not projects
    "{home}/.config/Claude",
    "{home}/.config/Code/User/History",
    "{home}/.config/Code/User/globalStorage",
    "{home}/.config/Code - Insiders/User/globalStorage",
]
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

# AI used by `codehound ask` and the dashboard's Ask AI actions.
# Providers: ollama (local, default), openai, gemini, kimi, custom.
# api_key falls back to the provider's env var (OPENAI_API_KEY / GEMINI_API_KEY / MOONSHOT_API_KEY).
[ai]
provider = "ollama"
model = ""        # blank = provider default
api_key = ""      # blank = env var / not needed
base_url = ""     # blank = provider default; set for custom endpoints
# Model writes a compact catalogue digest after each scan; catalogue questions are
# answered from the digest instead of re-feeding the whole project list every time.
catalogue_summary = false

[clone]
dest = "{home}/repos"   # synced account repos land in <dest>/<host>/<owner>/<repo>

# Janitor removes local clones whose remote work is done (merged/abandoned/archived).
# Always dry-run by default; only clean, fully-pushed clones are ever removed.
[janitor]
enabled = true
exclude = []            # repo names or full paths the janitor must never touch

# Watches GitHub releases for catalogued projects with a GitHub remote and
# notifies when a newer compatible release exists (`codehound updates`).
# mode = "stage"  download + verify automatically, activate on confirmation
# mode = "silent" fully automatic install for opted-in repos, notify after
# Per-repo override: codehound updates --auto <repo> (auto / never / default).
[updates]
enabled = true
mode = "stage"

# Accounts mirrored by `codehound sync` (also editable in the app's Settings → Accounts).
# Tokens are optional: GitHub falls back to the gh CLI login, Azure DevOps to az.
# [[accounts.github]]
# user = ""         # display label; the authenticated user is mirrored either way
# token = ""        # PAT with repo scope; blank = use gh CLI
# orgs = []         # also mirror these organisations
# forks = false
# archived = false
#
# [[accounts.azure]]
# org = "myorg"     # dev.azure.com/<org>
# pat = ""          # blank = use Azure CLI (az) login
# projects = []     # blank = all projects in the org
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
    clone_dest: str = str(Path.home() / "repos")
    accounts: dict[str, list] = field(default_factory=dict)  # provider key -> [Account]
    ai_provider: str = "ollama"
    ai_model: str = ""
    ai_api_key: str = ""
    ai_base_url: str = ""
    ai_catalogue_summary: bool = False
    janitor_enabled: bool = True
    janitor_exclude: list[str] = field(default_factory=list)
    updates_enabled: bool = True
    updates_mode: str = "stage"   # "stage" = confirm before activating, "silent" = auto-install


def ensure_config() -> Path:
    """Create the default config file if it doesn't exist; return its path."""
    if not CONFIG_PATH.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(DEFAULT_CONFIG.format(home=Path.home()))
    return CONFIG_PATH


def _toml_str(value: str) -> str:
    # json.dumps output is a valid TOML basic string
    return json.dumps(value, ensure_ascii=False)


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def save_config(cfg: Config) -> Path:
    """Write the config back to CONFIG_PATH, keeping the explanatory comments."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    launchers = "\n".join(f"{name} = {_toml_str(cmd)}" for name, cmd in cfg.launchers.items())
    blocks = []
    for key, provider in PROVIDERS.items():
        for account in cfg.accounts.get(key, []):
            blocks.append(f"[[accounts.{key}]]\n{provider.dump_account(account)}")
    accounts = "\n\n" + "\n\n".join(blocks) if blocks else ""
    text = f"""\
# codeHOUND configuration

[scan]
roots = {_toml_list(cfg.roots)}
# Directory names pruned anywhere in the tree
exclude_dirs = {_toml_list(cfg.exclude_dirs)}
# Absolute paths pruned exactly (add slow or irrelevant subtrees here)
exclude_paths = {_toml_list(cfg.exclude_paths)}
max_depth = {cfg.max_depth}
one_filesystem = {str(cfg.one_filesystem).lower()}   # don't cross mount points inside a root

[ollama]
enabled = {str(cfg.ollama_enabled).lower()}
url = {_toml_str(cfg.ollama_url)}
model = {_toml_str(cfg.ollama_model)}
enrich = {str(cfg.ollama_enrich).lower()}       # one-line project descriptions
classify = {str(cfg.ollama_classify).lower()}    # LLM decides whether ambiguous dirs are software projects
timeout = {cfg.ollama_timeout}        # seconds per request

[launchers]  # ordered; {{path}} is substituted; shown in context menu
{launchers}

[adopt]
dest = {_toml_str(cfg.adopt_dest)}

# AI used by `codehound ask` and the dashboard's Ask AI actions.
# Providers: ollama (local, default), openai, gemini, kimi, custom.
# api_key falls back to the provider's env var (OPENAI_API_KEY / GEMINI_API_KEY / MOONSHOT_API_KEY).
[ai]
provider = {_toml_str(cfg.ai_provider)}
model = {_toml_str(cfg.ai_model)}        # blank = provider default
api_key = {_toml_str(cfg.ai_api_key)}      # blank = env var / not needed
base_url = {_toml_str(cfg.ai_base_url)}     # blank = provider default; set for custom endpoints
# Model writes a compact catalogue digest after each scan; catalogue questions are
# answered from the digest instead of re-feeding the whole project list every time.
catalogue_summary = {str(cfg.ai_catalogue_summary).lower()}

[clone]
dest = {_toml_str(cfg.clone_dest)}   # synced account repos land in <dest>/<host>/<owner>/<repo>

# Janitor removes local clones whose remote work is done (merged/abandoned/archived).
# Always dry-run by default; only clean, fully-pushed clones are ever removed.
[janitor]
enabled = {str(cfg.janitor_enabled).lower()}
exclude = {_toml_list(cfg.janitor_exclude)}            # repo names or full paths the janitor must never touch

# Watches GitHub releases for catalogued projects with a GitHub remote.
[updates]
enabled = {str(cfg.updates_enabled).lower()}
mode = {_toml_str(cfg.updates_mode)}   # "stage" = confirm before activating, "silent" = auto-install
{accounts}
"""
    CONFIG_PATH.write_text(text)
    return CONFIG_PATH


def load_config() -> Config:
    ensure_config()
    with open(CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f)
    scan = raw.get("scan", {})
    ollama = raw.get("ollama", {})
    adopt = raw.get("adopt", {})
    clone = raw.get("clone", {})
    ai = raw.get("ai", {})
    janitor = raw.get("janitor", {})
    updates = raw.get("updates", {})
    raw_accounts = raw.get("accounts", {})
    accounts = {
        key: provider.load_accounts(raw_accounts.get(key, []))
        for key, provider in PROVIDERS.items()
    }
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
        clone_dest=clone.get("dest", str(Path.home() / "repos")),
        accounts=accounts,
        ai_provider=ai.get("provider", "ollama"),
        ai_model=ai.get("model", ""),
        ai_api_key=ai.get("api_key", ""),
        ai_base_url=ai.get("base_url", ""),
        ai_catalogue_summary=ai.get("catalogue_summary", False),
        janitor_enabled=janitor.get("enabled", True),
        janitor_exclude=list(janitor.get("exclude", [])),
        updates_enabled=updates.get("enabled", True),
        updates_mode=updates.get("mode", "stage"),
    )
