"""Ask the configured AI about one project or across the whole catalogue."""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from . import db
from .ai import AIError, get_backend
from .ai.config_like import AiSettings

MAX_README_CHARS = 4000
MAX_DOC_CHARS = 2000
MAX_DOCS = 6
MAX_MANIFEST_CHARS = 1200
MAX_MANIFESTS = 4
MAX_TODO_LINES = 15
MAX_TREE_LINES = 60
TREE_DEPTH = 2
MAX_PROJECT_CHARS = 16000
MAX_LOG_LINES = 15
MAX_CATALOGUE_CHARS = 14000

# Any of these suffixes at the top level (or in docs/) is likely human- or
# agent-written commentary worth showing the model.
DOC_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}
DOC_DIRS = ("docs", "doc")
AGENT_STEMS = {"CLAUDE", "AGENTS", "GEMINI", ".CURSORRULES", "COPILOT-INSTRUCTIONS"}
DOC_STEMS = {
    "CONTRIBUTING", "TODO", "CHANGELOG", "CHANGES", "NEWS",
    "HACKING", "DESIGN", "ROADMAP", "ARCHITECTURE", "INSTRUCTIONS",
}
LOW_VALUE_STEMS = {"LICENSE", "LICENCE", "COPYING", "NOTICE"}
NESTED_INSTRUCTION_FILES = (".cursorrules", ".github/copilot-instructions.md", ".github/AGENTS.md")
MANIFEST_NAMES = (
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "CMakeLists.txt",
    "meson.build", "setup.py", "setup.cfg", "requirements.txt", "Makefile",
    "build.gradle", "pom.xml",
)
TREE_IGNORE = {
    ".git", "node_modules", "__pycache__", "build", "dist", "target",
    ".venv", "venv", ".cache", ".tox", ".mypy_cache", "out",
}

SYSTEM_PROJECT = (
    "You are the assistant inside codeHOUND, a tool that catalogues the user's "
    "software projects. Answer questions about the project using the context "
    "provided (git state, file tree, README/docs, agent instructions, build "
    "manifests, TODO markers). Be concise and concrete; mention file/commit "
    "evidence when relevant."
)
SYSTEM_CATALOGUE = (
    "You are the assistant inside codeHOUND, a tool that catalogues the user's "
    "software projects. The user asks questions across their whole catalogue — "
    "locating projects, comparing them, recalling what they contain. Answer from "
    "the project list provided (name, path, language, branch, dirty state, last "
    "commit, AI description). Cite matching projects by name; say so plainly if "
    "nothing matches. Be concise."
)
SYSTEM_DIGEST = (
    "You are the assistant inside codeHOUND, a tool that catalogues the user's "
    "software projects. Write a compact digest of the catalogue that lets you "
    "later answer locate/compare/recall questions WITHOUT seeing the raw list "
    "again. Cover: what each project is and its tech stack, groupings (by "
    "language, host, purpose), notable implementation details mentioned in "
    "descriptions or commits (crates/libraries, tricky code), recently active "
    "and stale projects. Use one bullet per project, name = path. Be dense and "
    "factual — this replaces the raw list."
)
SYSTEM_CATALOGUE_DIGEST = (
    "You are the assistant inside codeHOUND, a tool that catalogues the user's "
    "software projects. The user asks questions across their whole catalogue — "
    "locating projects, comparing them, recalling what they contain. Answer from "
    "the AI-written catalogue digest provided (and the compact project appendix, "
    "if present). Cite matching projects by name; say so plainly if nothing "
    "matches. Be concise."
)


def _git(path: str, args: list[str]) -> str:
    try:
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _read_excerpt(p: Path, limit: int) -> str:
    try:
        return p.read_text(errors="replace")[:limit]
    except OSError:
        return ""


def _file_tree(root: Path) -> str:
    lines: list[str] = []

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        for e in entries:
            if len(lines) >= MAX_TREE_LINES:
                return
            if e.name in TREE_IGNORE or (depth == 0 and e.name.startswith(".")):
                continue
            lines.append("  " * depth + e.name + ("/" if e.is_dir() else ""))
            if e.is_dir() and depth + 1 < TREE_DEPTH:
                walk(e, depth + 1)

    walk(root, 0)
    if len(lines) >= MAX_TREE_LINES:
        lines.append("… (tree truncated)")
    return "\n".join(lines)


def _doc_priority(p: Path) -> int:
    stem = p.stem.upper()
    if stem.startswith("README"):
        return 0
    if stem in AGENT_STEMS:
        return 1
    if stem in DOC_STEMS:
        return 2
    if stem in LOW_VALUE_STEMS:
        return 4
    return 3


def _is_commentary(e: Path) -> bool:
    return e.is_file() and e.suffix.lower() in DOC_SUFFIXES and e.name not in MANIFEST_NAMES


def _find_docs(p: Path) -> list[Path]:
    """Commentary files (.md/.txt/…), most authoritative first."""
    found: list[Path] = []
    try:
        found += [e for e in p.iterdir() if _is_commentary(e)]
    except OSError:
        pass
    for rel in NESTED_INSTRUCTION_FILES:
        f = p / rel
        if f.is_file():
            found.append(f)
    for d in DOC_DIRS:
        sub = p / d
        if sub.is_dir():
            try:
                found += [e for e in sub.iterdir() if _is_commentary(e)]
            except OSError:
                pass
    found.sort(key=lambda f: (_doc_priority(f), f.name.lower()))
    return found[:MAX_DOCS]


def _todo_scan(path: str) -> str:
    out = _git(path, ["grep", "-n", "-I", "-E", r"\b(TODO|FIXME|XXX|HACK)\b"])
    if not out:
        return ""
    lines = out.splitlines()
    extra = len(lines) - MAX_TODO_LINES
    if extra > 0:
        lines = lines[:MAX_TODO_LINES] + [f"… ({extra} more)"]
    return "\n".join(lines)


def project_context(path: str) -> str:
    p = Path(path)
    parts = [f"Project: {p.name}", f"Path: {path}"]
    if branch := _git(path, ["branch", "--show-current"]):
        parts.append(f"Branch: {branch}")
    if remote := _git(path, ["remote", "get-url", "origin"]):
        parts.append(f"Remote: {remote}")
    if log := _git(path, ["log", f"-{MAX_LOG_LINES}", "--pretty=%ad %an: %s", "--date=short"]):
        parts.append(f"Recent commits:\n{log}")
    if status := _git(path, ["status", "--porcelain"]):
        n = len(status.splitlines())
        parts.append(f"Working tree: {n} uncommitted change(s)")
    if tree := _file_tree(p):
        parts.append(f"File tree (depth {TREE_DEPTH}):\n{tree}")
    for doc in _find_docs(p):
        limit = MAX_README_CHARS if _doc_priority(doc) == 0 else MAX_DOC_CHARS
        label = doc.name if doc.parent == p else str(doc.relative_to(p))
        if text := _read_excerpt(doc, limit):
            parts.append(f"{label} (excerpt):\n{text}")
    manifests = [f for f in (p / m for m in MANIFEST_NAMES) if f.is_file()][:MAX_MANIFESTS]
    for m in manifests:
        if text := _read_excerpt(m, MAX_MANIFEST_CHARS):
            parts.append(f"{m.name} (excerpt):\n{text}")
    if todos := _todo_scan(path):
        parts.append(f"TODO/FIXME markers:\n{todos}")
    text = "\n\n".join(parts)
    if len(text) > MAX_PROJECT_CHARS:
        text = text[:MAX_PROJECT_CHARS] + "\n… (project context truncated)"
    return text


def ask_about_project(cfg: AiSettings, path: str, question: str) -> str:
    prompt = f"{project_context(path)}\n\nQuestion: {question}"
    return get_backend(cfg).ask(SYSTEM_PROJECT, prompt)


def catalogue_rows() -> list:
    conn = db.connect()
    try:
        rows = db.all_projects(conn)
    finally:
        conn.close()
    return [r for r in rows if not r["missing"]]


def _row_line(r) -> str:
    bits = [r["name"], r["path"]]
    if r["language"]:
        bits.append(f"lang={r['language']}")
    if r["branch"]:
        bits.append(f"branch={r['branch']}")
    if r["dirty"]:
        bits.append("dirty")
    if r["last_commit_msg"]:
        bits.append(f"last={r['last_commit_date'] or ''} {r['last_commit_msg'][:80]}".strip())
    if r["description"]:
        bits.append(f"desc={r['description']}")
    return " | ".join(bits)


def catalogue_context() -> str:
    text = "\n".join(_row_line(r) for r in catalogue_rows())
    if len(text) > MAX_CATALOGUE_CHARS:
        text = text[:MAX_CATALOGUE_CHARS] + "\n… (catalogue truncated)"
    return text


# ----- model-written catalogue digest -----
def get_digest() -> tuple[str | None, str | None]:
    """The stored (digest, generated-at) pair, if any."""
    conn = db.connect()
    try:
        return db.kv_get(conn, "ai_digest"), db.kv_get(conn, "ai_digest_at")
    finally:
        conn.close()


def digest_stale(digest_at: str | None) -> bool:
    """Is the digest older than the newest live project's modified stamp?

    The scanner refreshes `modified` from directory mtimes each scan, so this is
    a cheap guide to which summaries might need a re-up — no file watching.
    """
    if not digest_at:
        return True
    try:
        gen = datetime.fromisoformat(digest_at)
    except ValueError:
        return True
    return any(
        (m := r["modified"]) and datetime.fromisoformat(m) > gen
        for r in catalogue_rows()
    )


def digest_status() -> tuple[str | None, bool]:
    """(generated-at, stale?) for display purposes."""
    digest, generated = get_digest()
    return generated, (digest_stale(generated) if digest else False)


def generate_digest(cfg: AiSettings) -> str:
    rows = catalogue_rows()
    if not rows:
        raise AIError("no projects in the catalogue yet — run a scan first")
    prompt = (
        f"Catalogue ({len(rows)} projects, one per line):\n"
        f"{catalogue_context()}\n\n"
        "Write the catalogue digest."
    )
    return get_backend(cfg).ask(SYSTEM_DIGEST, prompt)


def refresh_digest(cfg: AiSettings) -> str:
    """Regenerate and store the digest; returns it."""
    digest = generate_digest(cfg)
    conn = db.connect()
    try:
        db.kv_set(conn, "ai_digest", digest)
        db.kv_set(conn, "ai_digest_at", db.now())
        conn.commit()
    finally:
        conn.close()
    return digest


def _catalogue_appendix(limit: int) -> str:
    text = "\n".join(
        " | ".join(b for b in (r["name"], r["path"], r["language"] or "", r["description"] or "") if b)
        for r in catalogue_rows()
    )
    return text[:limit]


def ask_about_catalogue(cfg: AiSettings, question: str) -> str:
    if not cfg.ai_catalogue_summary:
        prompt = f"Projects (one per line):\n{catalogue_context()}\n\nQuestion: {question}"
        return get_backend(cfg).ask(SYSTEM_CATALOGUE, prompt)

    digest, generated = get_digest()
    if not digest:
        raise AIError(
            "no catalogue digest yet — click Ask AI ▸ Regenerate summary, "
            "or run `codehound ask --refresh-digest ok`"
        )
    budget = MAX_CATALOGUE_CHARS - len(digest) - len(question) - 400
    stale_note = (
        "Note: this digest may be out of date — projects changed since it was written.\n\n"
        if digest_stale(generated) else ""
    )
    parts = [f"Catalogue digest (generated {generated}):\n{stale_note}{digest}"]
    if budget > 800:
        parts.append(f"Compact appendix (name | path | lang | desc):\n{_catalogue_appendix(budget)}")
    parts.append(f"Question: {question}")
    return get_backend(cfg).ask(SYSTEM_CATALOGUE_DIGEST, "\n\n".join(parts))
