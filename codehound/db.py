"""SQLite storage for scanned projects."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    path            TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,          -- git | bare | ungitted
    name            TEXT NOT NULL,
    root            TEXT NOT NULL,
    language        TEXT,
    branch          TEXT,
    remote_url      TEXT,
    last_commit_date TEXT,
    last_commit_msg TEXT,
    dirty           INTEGER DEFAULT 0,
    created         TEXT,
    modified        TEXT,
    size_approx     INTEGER,
    description     TEXT,
    desc_mtime      TEXT,                   -- modified value when description was generated
    adopted_to      TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    missing         INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started         TEXT NOT NULL,
    finished        TEXT,
    roots           TEXT,
    projects_found  INTEGER,
    errors          TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key             TEXT PRIMARY KEY,
    value           TEXT
);
CREATE TABLE IF NOT EXISTS updates (
    repo          TEXT PRIMARY KEY,     -- owner/repo on GitHub
    path          TEXT,                 -- local clone path
    name          TEXT,                 -- catalogue display name
    current_tag   TEXT,                 -- tag checked out locally ('' = unknown)
    latest_tag    TEXT,
    published_at  TEXT,
    asset_name    TEXT,                 -- best asset for this OS/arch ('' = none)
    asset_url     TEXT,
    available     INTEGER DEFAULT 0,    -- newer release exists
    compatible    INTEGER DEFAULT 0,    -- and an installable asset matched
    reason        TEXT,                 -- why not compatible / no update
    auto          INTEGER,              -- NULL = follow default, 1 = auto-install, 0 = never
    seen          INTEGER DEFAULT 1,
    last_checked  TEXT,
    last_install  TEXT,
    install_mode  TEXT,                 -- managed | inplace | custom ('' = never installed)
    install_dir   TEXT,                 -- where the current version lives
    install_exe   TEXT,                 -- executable we manage/symlink
    prev_dir      TEXT,                 -- previous version dir, for rollback
    install_path  TEXT,                 -- user-chosen custom install location
    installed_tag TEXT,                 -- version currently installed by us
    prev_tag      TEXT                  -- previous installed version (rollback target)
);
"""

# Columns added after the first release of the updates table — older databases
# get them ALTERed in on connect.
_UPDATE_COLUMNS = {
    "install_mode": "TEXT",
    "install_dir": "TEXT",
    "install_exe": "TEXT",
    "prev_dir": "TEXT",
    "install_path": "TEXT",
    "installed_tag": "TEXT",
    "prev_tag": "TEXT",
}

UPSERT_COLS = [
    "path", "kind", "name", "root", "language", "branch", "remote_url",
    "last_commit_date", "last_commit_msg", "dirty", "created", "modified",
    "size_approx",
]


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate_updates(conn)
    return conn


def _migrate_updates(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(updates)")}
    for col, decl in _UPDATE_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE updates ADD COLUMN {col} {decl}")


def upsert_project(conn: sqlite3.Connection, proj: dict) -> None:
    ts = now()
    cols = ", ".join(UPSERT_COLS)
    ph = ", ".join(f":{c}" for c in UPSERT_COLS)
    sets = ", ".join(f"{c}=excluded.{c}" for c in UPSERT_COLS if c != "path")
    conn.execute(
        f"""INSERT INTO projects ({cols}, first_seen, last_seen, missing)
            VALUES ({ph}, :ts, :ts, 0)
            ON CONFLICT(path) DO UPDATE SET {sets}, last_seen=excluded.last_seen, missing=0""",
        {**{c: proj.get(c) for c in UPSERT_COLS}, "ts": ts},
    )


def mark_missing(conn: sqlite3.Connection, scanned_roots: list[str], seen_paths: set[str]) -> int:
    """Flag projects under the scanned roots that were not seen this scan."""
    n = 0
    for row in conn.execute("SELECT path, root FROM projects WHERE missing = 0"):
        if row["root"] in scanned_roots and row["path"] not in seen_paths:
            conn.execute("UPDATE projects SET missing = 1 WHERE path = ?", (row["path"],))
            n += 1
    return n


def start_scan(conn: sqlite3.Connection, roots: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO scans (started, roots) VALUES (?, ?)", (now(), ", ".join(roots))
    )
    return cur.lastrowid


def finish_scan(conn: sqlite3.Connection, scan_id: int, found: int, errors: list[str]) -> None:
    conn.execute(
        "UPDATE scans SET finished = ?, projects_found = ?, errors = ? WHERE id = ?",
        (now(), found, "\n".join(errors) if errors else None, scan_id),
    )


def all_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM projects ORDER BY modified DESC").fetchall()


def duplicate_remotes(conn: sqlite3.Connection) -> set[str]:
    """Remote URLs that appear at more than one live path."""
    rows = conn.execute(
        """SELECT remote_url FROM projects
           WHERE remote_url IS NOT NULL AND remote_url != '' AND missing = 0
           GROUP BY remote_url HAVING COUNT(*) > 1"""
    ).fetchall()
    return {r["remote_url"] for r in rows}


def stale_descriptions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Projects needing (re-)description: never described or modified since."""
    return conn.execute(
        """SELECT path, name, kind, language, modified FROM projects
           WHERE missing = 0
             AND (description IS NULL OR desc_mtime IS NULL OR desc_mtime != modified)"""
    ).fetchall()


def set_description(conn: sqlite3.Connection, path: str, description: str, mtime: str) -> None:
    conn.execute(
        "UPDATE projects SET description = ?, desc_mtime = ? WHERE path = ?",
        (description, mtime, path),
    )


def set_adopted(conn: sqlite3.Connection, path: str, dest: str) -> None:
    conn.execute("UPDATE projects SET adopted_to = ? WHERE path = ?", (dest, path))


def last_scan(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM scans WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


# ----- updates (release watch) -----

def upsert_update(conn: sqlite3.Connection, u: dict) -> None:
    """Record the latest check result for one watched repo, keeping user flags."""
    conn.execute(
        """INSERT INTO updates (repo, path, name, current_tag, latest_tag, published_at,
                                asset_name, asset_url, available, compatible, reason,
                                seen, last_checked)
           VALUES (:repo, :path, :name, :current_tag, :latest_tag, :published_at,
                   :asset_name, :asset_url, :available, :compatible, :reason,
                   1, :last_checked)
           ON CONFLICT(repo) DO UPDATE SET
               path=excluded.path, name=excluded.name, current_tag=excluded.current_tag,
               latest_tag=excluded.latest_tag, published_at=excluded.published_at,
               asset_name=excluded.asset_name, asset_url=excluded.asset_url,
               available=excluded.available, compatible=excluded.compatible,
               reason=excluded.reason, last_checked=excluded.last_checked""",
        u,
    )


def get_update(conn: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM updates WHERE repo = ?", (repo,)).fetchone()


def available_updates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM updates WHERE available = 1
           ORDER BY published_at DESC, repo"""
    ).fetchall()


def set_update_auto(conn: sqlite3.Connection, repo: str, mode: str | None) -> None:
    """mode: 'auto' -> 1, 'never' -> 0, 'default'/None -> NULL (follow global)."""
    value = {"auto": 1, "never": 0}.get(mode)
    conn.execute("UPDATE updates SET auto = ? WHERE repo = ?", (value, repo))


def mark_updates_seen(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE updates SET seen = 1 WHERE available = 1")


def note_update_install(conn: sqlite3.Connection, repo: str) -> None:
    conn.execute(
        "UPDATE updates SET last_install = ?, available = 0, seen = 1 WHERE repo = ?",
        (now(), repo),
    )


def set_install(conn: sqlite3.Connection, repo: str, *, mode: str, install_dir: str,
                install_exe: str, prev_dir: str, installed_tag: str,
                prev_tag: str) -> None:
    """Record where a repo's app is installed and what to roll back to."""
    conn.execute(
        """UPDATE updates SET install_mode=?, install_dir=?, install_exe=?, prev_dir=?,
                          installed_tag=?, prev_tag=?, last_install=?, available=0, seen=1
           WHERE repo=?""",
        (mode, install_dir, install_exe, prev_dir, installed_tag, prev_tag, now(), repo),
    )


def set_update_install_path(conn: sqlite3.Connection, repo: str, path: str) -> None:
    conn.execute("UPDATE updates SET install_path = ? WHERE repo = ?", (path, repo))


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
