"""Ollama client for project descriptions and classification (stdlib only)."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import db
from .config import Config

README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")
README_CHARS = 1500
LISTING_LIMIT = 40


def _post(url: str, payload: dict, timeout: int) -> dict | None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def server_up(cfg: Config) -> bool:
    try:
        with urllib.request.urlopen(f"{cfg.ollama_url}/api/tags", timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def model_available(cfg: Config) -> bool:
    try:
        with urllib.request.urlopen(f"{cfg.ollama_url}/api/tags", timeout=5) as resp:
            models = json.loads(resp.read()).get("models", [])
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return False
    names = {m.get("name", "") for m in models}
    return cfg.ollama_model in names or f"{cfg.ollama_model}:latest" in names


def pull_model(cfg: Config, progress=None) -> bool:
    """Pull the configured model, streaming status lines. Returns success."""
    say = progress or (lambda m: None)
    say(f"pulling {cfg.ollama_model} (this can take a while)…")
    req = urllib.request.Request(
        f"{cfg.ollama_url}/api/pull",
        data=json.dumps({"model": cfg.ollama_model}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            last = ""
            for line in resp:
                try:
                    status = json.loads(line).get("status", "")
                except json.JSONDecodeError:
                    continue
                if status != last:
                    say(f"  {status}")
                    last = status
            return True
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        say(f"pull failed: {e}")
        return False


def _generate(cfg: Config, prompt: str, num_predict: int = 80) -> str | None:
    resp = _post(
        f"{cfg.ollama_url}/api/generate",
        {
            "model": cfg.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": num_predict},
        },
        cfg.ollama_timeout,
    )
    if resp and resp.get("response"):
        return resp["response"].strip()
    return None


def _project_context(path: str) -> str:
    parts = []
    try:
        names = sorted(os.listdir(path))[:LISTING_LIMIT]
        parts.append("Files: " + ", ".join(names))
    except OSError:
        return ""
    for rn in README_NAMES:
        p = os.path.join(path, rn)
        if os.path.isfile(p):
            try:
                with open(p, "r", errors="replace") as f:
                    parts.append("README excerpt:\n" + f.read(README_CHARS))
            except OSError:
                pass
            break
    return "\n".join(parts)


def describe(cfg: Config, path: str, name: str, language: str | None) -> str | None:
    ctx = _project_context(path)
    if not ctx:
        return None
    prompt = (
        f"You catalogue software projects. Based on the info below, write ONE short sentence "
        f"(max 20 words) describing what the project '{name}'"
        f"{f' ({language})' if language else ''} is or does. "
        f"Reply with only the sentence, no preamble.\n\n{ctx}"
    )
    desc = _generate(cfg, prompt)
    if desc:
        desc = desc.splitlines()[0].strip().strip('"')[:200]
    return desc or None


def classify(cfg: Config, path: str) -> bool:
    """Ask the model whether an ambiguous directory is a software project."""
    ctx = _project_context(path)
    if not ctx:
        return False
    prompt = (
        f"Is the directory '{os.path.basename(path)}' a software project "
        f"(source code someone is developing), as opposed to downloads, backups, media, "
        f"or application data? Answer with exactly YES or NO.\n\n{ctx}"
    )
    ans = _generate(cfg, prompt, num_predict=5)
    return bool(ans) and ans.upper().startswith("YES")


def enrich(cfg: Config, progress=None) -> int:
    """Generate descriptions for projects with stale/missing ones. Returns count."""
    say = progress or (lambda m: None)
    if not cfg.ollama_enabled or not cfg.ollama_enrich:
        return 0
    if not server_up(cfg):
        say("ollama unreachable — skipping enrichment")
        return 0
    if not model_available(cfg):
        if not pull_model(cfg, progress):
            return 0
    conn = db.connect()
    rows = db.stale_descriptions(conn)
    if rows:
        say(f"describing {len(rows)} projects with {cfg.ollama_model}…")
    done = 0
    for row in rows:
        desc = describe(cfg, row["path"], row["name"], row["language"])
        if desc:
            db.set_description(conn, row["path"], desc, row["modified"] or "")
            conn.commit()
            done += 1
    conn.close()
    if rows:
        say(f"described {done}/{len(rows)}")
    return done
