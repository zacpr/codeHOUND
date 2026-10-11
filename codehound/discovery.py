"""Detect installed editors, AI agent CLIs, and local AI services (stdlib only).

Read-only: nothing here touches config. Callers decide what to do with results.
"""
from __future__ import annotations

import json
import shlex
import shutil
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

PROBE_TIMEOUT = 1.0

EDITOR_CANDIDATES: list[tuple[str, str, str]] = [
    ("VS Code", "code", "code {path}"),
    ("VS Code Insiders", "code-insiders", "code-insiders {path}"),
    ("Cursor", "cursor", "cursor {path}"),
    ("Windsurf", "windsurf", "windsurf {path}"),
    ("Zed", "zed", "zed {path}"),
    ("Sublime Text", "subl", "subl {path}"),
    ("Antigravity", "antigravity", "antigravity {path}"),
    ("IntelliJ IDEA", "idea", "idea {path}"),
    ("PyCharm", "pycharm", "pycharm {path}"),
    ("WebStorm", "webstorm", "webstorm {path}"),
    ("GoLand", "goland", "goland {path}"),
    ("CLion", "clion", "clion {path}"),
    ("RubyMine", "rubymine", "rubymine {path}"),
    ("PhpStorm", "phpstorm", "phpstorm {path}"),
    ("Dolphin", "dolphin", "dolphin {path}"),
    ("Files (Nautilus)", "nautilus", "nautilus {path}"),
    ("Thunar", "thunar", "thunar {path}"),
    ("PCManFM", "pcmanfm", "pcmanfm {path}"),
    ("Nemo", "nemo", "nemo {path}"),
    ("Konsole", "konsole", "konsole --workdir {path}"),
    ("GNOME Terminal", "gnome-terminal", "gnome-terminal --working-directory={path}"),
    ("Alacritty", "alacritty", "alacritty --working-directory {path}"),
    ("Kitty", "kitty", "kitty --directory {path}"),
    ("Foot", "foot", "foot -D {path}"),
]

# Terminal emulators able to wrap a foreground CLI, tried in this order.
# Template carries {path} (substituted at launch time, like all launcher
# commands) and {bin} (substituted here with the detected agent binary).
TERMINAL_WRAPPERS: list[tuple[str, str]] = [
    ("konsole", "konsole --workdir {path} -e {bin}"),
    ("gnome-terminal", "gnome-terminal --working-directory={path} -- {bin}"),
    ("alacritty", "alacritty --working-directory {path} -e {bin}"),
    ("kitty", "kitty --directory {path} {bin}"),
    ("foot", "foot -D {path} {bin}"),
]

AGENT_CLI_CANDIDATES: list[tuple[str, str]] = [
    ("Claude Code", "claude"),
    ("OpenAI Codex CLI", "codex"),
    ("Gemini CLI", "gemini"),
    ("Aider", "aider"),
    ("GitHub Copilot CLI", "copilot"),
    ("Cody CLI", "cody"),
    ("Kimi CLI", "kimi"),
]

# Ollama is excluded here — it already has its own detection (ollama.py) and
# its own Settings tab.
MODEL_RUNNER_CANDIDATES: list[tuple[str, str, str]] = [
    ("lmstudio", "LM Studio", "http://localhost:1234/v1"),
    ("openwebui", "OpenWebUI", "http://localhost:8080/v1"),
    ("openwebui-alt", "OpenWebUI", "http://localhost:3000/v1"),
    ("textgen", "text-generation-webui", "http://localhost:5000/v1"),
    ("localai", "LocalAI", "http://localhost:8080/v1"),
    ("jan", "Jan", "http://localhost:1337/v1"),
]


@dataclass
class DetectedLauncher:
    name: str
    command: str
    kind: str  # "editor" or "agent"


@dataclass
class DetectedProvider:
    key: str
    label: str
    base_url: str
    model: str = ""


def _binary_in_use(binary: str, existing_commands: dict[str, str]) -> bool:
    for cmd in existing_commands.values():
        try:
            parts = shlex.split(cmd)
        except ValueError:
            parts = cmd.split()
        if binary in parts:
            return True
    return False


def _terminal_template() -> str | None:
    for binary, template in TERMINAL_WRAPPERS:
        if shutil.which(binary):
            return template
    return None


def detect_editors(existing_commands: dict[str, str]) -> list[DetectedLauncher]:
    """Editors, file managers and terminals found on PATH and not already configured."""
    found = []
    for name, binary, template in EDITOR_CANDIDATES:
        if shutil.which(binary) and not _binary_in_use(binary, existing_commands):
            found.append(DetectedLauncher(name=name, command=template, kind="editor"))
    return found


def detect_agent_clis(existing_commands: dict[str, str]) -> list[DetectedLauncher]:
    """AI coding-agent CLIs found on PATH, wrapped in whatever terminal is available."""
    template = _terminal_template()
    if not template:
        return []
    found = []
    for name, binary in AGENT_CLI_CANDIDATES:
        if shutil.which(binary) and not _binary_in_use(binary, existing_commands):
            command = template.replace("{bin}", binary)
            found.append(DetectedLauncher(name=name, command=command, kind="agent"))
    return found


def _probe_runner(key: str, label: str, base_url: str, timeout: float) -> DetectedProvider | None:
    req = urllib.request.Request(f"{base_url}/models", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None
    model = ""
    items = data.get("data") if isinstance(data, dict) else None
    if items:
        model = items[0].get("id", "") if isinstance(items[0], dict) else ""
    return DetectedProvider(key=key, label=label, base_url=base_url, model=model)


def detect_model_runners(timeout: float = PROBE_TIMEOUT) -> list[DetectedProvider]:
    """Local OpenAI-compatible model runners reachable right now."""
    found: list[DetectedProvider] = []
    with ThreadPoolExecutor(max_workers=len(MODEL_RUNNER_CANDIDATES)) as pool:
        futures = [
            pool.submit(_probe_runner, key, label, base_url, timeout)
            for key, label, base_url in MODEL_RUNNER_CANDIDATES
        ]
        for future in as_completed(futures):
            result = future.result()
            if result:
                found.append(result)
    return found
