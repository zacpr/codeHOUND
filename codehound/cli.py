"""codehound command-line entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import CONFIG_PATH, ensure_config, load_config


def _say(msg: str) -> None:
    print(msg, flush=True)


def cmd_scan(args) -> int:
    from . import db, ollama, scanner

    cfg = load_config()
    result = scanner.run_scan(cfg, roots=args.root or None, verbose=args.verbose, progress=_say)

    if cfg.ollama_enabled and cfg.ollama_classify and result.candidates:
        if ollama.server_up(cfg) and (ollama.model_available(cfg) or ollama.pull_model(cfg, _say)):
            total = len(result.candidates)
            _say(f"classifying {total} ambiguous dirs with {cfg.ollama_model} at {cfg.ollama_url}…")
            conn = db.connect()
            import os
            import time
            found = skipped = undecided = 0
            for i, path in enumerate(result.candidates, 1):
                _say(f"[{i}/{total}] {path} is unclassified, asking {cfg.ollama_model} to analyse it…")
                started = time.monotonic()
                verdict, note = ollama.classify_detail(cfg, path)
                took = f"{time.monotonic() - started:.1f}s"
                if verdict is None:
                    undecided += 1
                    _say(f"  → no verdict ({note}, {took}), moving on")
                elif verdict:
                    found += 1
                    _say(f"  → classified as a software project ({took}), adding to catalogue")
                else:
                    skipped += 1
                    _say(f"  → classified as not a coding project ({took}), moving on")
                remaining = total - i
                if remaining:
                    _say(f"  {remaining} ambiguous dirs remain, next: {result.candidates[i]}")
                if verdict:
                    proj = {
                        "path": path, "kind": "ungitted",
                        "name": os.path.basename(path.rstrip("/")),
                        "root": next((r for r in cfg.roots if path.startswith(r)), ""),
                        "language": None, "branch": None, "remote_url": None,
                        "last_commit_date": None, "last_commit_msg": None,
                        "dirty": 0, "created": None, "modified": None, "size_approx": None,
                    }
                    db.upsert_project(conn, proj)
                    conn.commit()
            conn.close()
            _say(
                f"classification done: {found} projects · {skipped} not projects · "
                f"{undecided} undecided"
            )
        else:
            _say("ollama unavailable — skipping classification")

    if not args.no_enrich:
        ollama.enrich(cfg, progress=_say)

    if cfg.ai_catalogue_summary:
        from . import ask
        from .ai import AIError

        try:
            _say("updating AI catalogue summary…")
            ask.refresh_digest(cfg)
        except AIError as e:
            _say(f"catalogue summary skipped: {e}")
    return 1 if result.errors and not result.projects else 0


def cmd_sync(args) -> int:
    from . import sync

    cfg = load_config()
    if not any(cfg.accounts.values()):
        _say("no accounts configured — add some in Settings → Accounts or in config.toml")
        return 0
    result = sync.sync_accounts(cfg, progress=_say)
    _say(
        f"sync done: {result.remote} remote · {len(result.cloned)} cloned · "
        f"{len(result.updated)} updated · {result.unchanged} unchanged · "
        f"{len(result.skipped_dirty)} dirty (skipped pull) · {len(result.failed)} failed"
    )
    for failure in result.failed:
        _say(f"  FAILED {failure}")
    if cfg.ai_catalogue_summary and (result.cloned or result.updated):
        from . import ask
        from .ai import AIError

        try:
            _say("updating AI catalogue summary…")
            ask.refresh_digest(cfg)
        except AIError as e:
            _say(f"catalogue summary skipped: {e}")
    return 1 if result.failed else 0


def cmd_ask(args) -> int:
    from . import ask
    from .ai import AIError

    cfg = load_config()
    try:
        if args.refresh_digest:
            digest = ask.refresh_digest(cfg)
            _say(f"digest refreshed ({len(digest)} chars)")
            return 0
        if cfg.ai_catalogue_summary:
            from .ask import digest_status
            generated, stale = digest_status()
            state = "stale — projects changed since" if stale else "up to date"
            _say(f"(catalogue summary: {state}, generated {generated or 'never'})")
        if not args.question:
            print("ask: a question is required (unless --refresh-digest)", file=sys.stderr)
            return 1
        if args.path:
            answer = ask.ask_about_project(cfg, args.path, args.question)
        else:
            answer = ask.ask_about_catalogue(cfg, args.question)
    except AIError as e:
        print(f"ask: {e}", file=sys.stderr)
        return 1
    _say(answer)
    return 0


def cmd_open(args) -> int:
    from . import db, launchers

    cfg = load_config()
    needle = args.name.lower()
    conn = db.connect()
    try:
        rows = [r for r in db.all_projects(conn) if not r["missing"]]
    finally:
        conn.close()
    matches = [r for r in rows if needle in r["name"].lower() or needle in r["path"].lower()]
    if not matches:
        print(f"open: no project matching {args.name!r}", file=sys.stderr)
        return 1
    # exact name match wins, then shortest path
    exact = [r for r in matches if r["name"].lower() == needle]
    target = (exact or sorted(matches, key=lambda r: len(r["path"])))[0]
    names = launchers.launcher_names(cfg)
    if not names:
        print("open: no launchers configured", file=sys.stderr)
        return 1
    launchers.launch(cfg, names[0], target["path"])
    _say(f"opened {target['path']} in {names[0]}")
    return 0


def cmd_janitor(args) -> int:
    from . import janitor

    cfg = load_config()
    if not cfg.janitor_enabled:
        _say("janitor is disabled in config ([janitor] enabled = false)")
        return 0
    result = janitor.run(cfg, dry_run=not args.force, progress=_say)
    verb = "would remove" if not args.force else "removed"
    _say(
        f"janitor: {len(result.candidates)} candidates, {len(result.removed)} {verb}, "
        f"{len(result.skipped)} skipped, {len(result.errors)} errors"
    )
    if result.skipped:
        _say("skipped:")
        for path, why in result.skipped:
            _say(f"  {path}  ({why})")
    return 1 if result.errors else 0


def cmd_app(args) -> int:
    from .app import run_app
    return run_app()


def cmd_adopt(args) -> int:
    from . import adopt as adopt_mod

    cfg = load_config()
    name = args.name or adopt_mod.suggest_name(cfg, args.path)
    dest = adopt_mod.adopt(cfg, args.path, name, progress=_say)
    _say(f"adopted to {dest}")
    return 0


def _newproj_fail(action: str, e: Exception) -> int:
    print(f"{action}: {e}", file=sys.stderr)
    return 1


def cmd_new(args) -> int:
    from . import newproj

    cfg = load_config()
    try:
        dest = newproj.create_project(
            cfg, args.name, args.description, args.scaffold, args.dest, progress=_say)
    except (newproj.NewProjectError, FileExistsError, ValueError) as e:
        return _newproj_fail("new", e)
    _say(f"new project ready at {dest}")
    return 0


def cmd_clone(args) -> int:
    from . import newproj

    cfg = load_config()
    try:
        dest = newproj.clone_project(cfg, args.source, args.name, args.dest, progress=_say)
    except (newproj.NewProjectError, FileExistsError, ValueError) as e:
        return _newproj_fail("clone", e)
    _say(f"clone ready at {dest}")
    return 0


def cmd_worktree(args) -> int:
    from . import newproj

    cfg = load_config()
    try:
        dest = newproj.add_worktree(cfg, args.path, args.branch, args.dest, progress=_say)
    except (newproj.NewProjectError, FileExistsError, ValueError) as e:
        return _newproj_fail("worktree", e)
    _say(f"worktree ready at {dest}")
    return 0


def cmd_detect(args) -> int:
    from . import discovery

    cfg = load_config()
    editors = discovery.detect_editors(cfg.launchers)
    agents = discovery.detect_agent_clis(cfg.launchers)
    runners = discovery.detect_model_runners()

    _say("Editors / tools:")
    for d in editors:
        _say(f"  {d.name} — {d.command}")
    if not editors:
        _say("  none found")

    _say("AI agent CLIs:")
    for d in agents:
        _say(f"  {d.name} — {d.command}")
    if not agents:
        _say("  none found (or no terminal emulator detected to launch them in)")

    _say("Local AI services:")
    for r in runners:
        note = f" (model: {r.model})" if r.model else ""
        _say(f"  {r.label} — {r.base_url}{note}")
    if not runners:
        _say("  none found")
    return 0


def cmd_updates(args) -> int:
    from . import db, updates
    from .providers.base import SyncError

    cfg = load_config()
    if not cfg.updates_enabled and not args.force:
        _say("updates watching is disabled — enable it in config.toml [updates] "
             "(or pass --force)")
        return 0
    conn = db.connect()
    try:
        if args.auto:
            repo = args.auto.lower()
            if db.get_update(conn, repo) is None:
                _say(f"{repo} is not on the watch list — it needs a catalogued "
                     "clone with a GitHub remote")
                return 1
            db.set_update_auto(conn, repo, args.mode)
            conn.commit()
            state = {"auto": "auto-install", "never": "never auto-install",
                     "default": "follow the global mode"}[args.mode]
            _say(f"{repo}: {state}")
            return 0
        if args.activate:
            staged = db.staged_updates(conn)
            if not staged:
                _say("nothing staged — run 'codehound updates --check' first")
                return 0
            failed = 0
            for row in staged:
                if row["repo"] == updates.self_repo(cfg):
                    _say(f"{row['repo']}: self-update runs with 'codehound self-update'")
                    continue
                try:
                    _say(updates.activate_staged(conn, row["repo"], progress=_say))
                except SyncError as e:
                    _say(f"activate: {e}")
                    failed += 1
            return 1 if failed else 0
        if args.check:
            _say(f"checking GitHub releases for {len(updates.watch_targets(conn))} watched repos…")
            try:
                available = updates.check_updates(cfg, conn, progress=_say)
            except SyncError as e:
                _say(f"updates: {e}")
                return 1
            if updates.self_repo(cfg):
                updates.check_self_update(cfg, conn, progress=_say)
                available = db.available_updates(conn)
            if available and not args.no_auto:
                installed, newly_staged, _errs = updates.apply_updates(
                    cfg, conn, progress=_say)
                if installed:
                    _say(f"auto-installed {len(installed)} update(s)")
                if newly_staged:
                    _say(f"{len(newly_staged)} update(s) staged — confirm with "
                         "'codehound update REPO' or 'codehound updates --activate'")
        else:
            available = db.available_updates(conn)
        if not available:
            _say("no updates available" + ("" if args.check else " (run with --check to refresh)"))
            return 0
        _say(f"{len(available)} update(s) available:")
        for row in available:
            mark = "✓ installable here" if row["compatible"] else f"✗ {row['reason'] or 'no compatible asset'}"
            have = row["current_tag"] or "unknown"
            _say(f"  {row['repo']}: {have} → {row['latest_tag']}  [{mark}]")
            if row["asset_name"]:
                _say(f"      asset: {row['asset_name']}")
            if row["installed_tag"]:
                _say(f"      installed by codeHOUND: {row['installed_tag']} ({row['install_mode']})")
            if (row["staged_dir"] or "") and Path(row["staged_dir"]).is_dir():
                _say(f"      staged: {row['staged_tag']} — 'codehound update {row['repo']}' installs it")
        return 0
    finally:
        conn.close()


def cmd_install_timer(args) -> int:
    from .install import install_timer
    return install_timer(every=args.every)


def _resolve_repo(conn, name: str) -> str | None:
    """owner/repo from an exact match, a repo-name fragment, or fuzzy name."""
    import difflib
    rows = conn.execute("SELECT repo, name FROM updates").fetchall()
    name_l = name.lower()
    for r in rows:
        if r["repo"].lower() == name_l:
            return r["repo"]
    hits = [r["repo"] for r in rows if name_l in r["repo"].lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        _say(f"{name} is ambiguous: " + ", ".join(sorted(hits)))
        return None
    close = difflib.get_close_matches(name_l, [r["repo"].lower() for r in rows], n=1)
    if close:
        _say(f"did you mean {close[0]}?")
    return None


def cmd_update(args) -> int:
    from . import db, updates
    from .providers.base import SyncError

    conn = db.connect()
    try:
        repo = _resolve_repo(conn, args.repo)
        if repo is None:
            _say(f"{args.repo} is not on the watch list — run 'codehound updates --check' first")
            return 1
        if args.rollback:
            try:
                _say(updates.rollback_update(conn, repo, progress=_say))
            except SyncError as e:
                _say(f"rollback: {e}")
                return 1
            return 0
        if args.set_path is not None:
            if args.set_path:
                p = Path(args.set_path).expanduser()
                if p.exists() and not p.is_dir() and not p.is_file():
                    _say(f"{p} is not a file or directory")
                    return 1
            db.set_update_install_path(conn, repo, args.set_path)
            conn.commit()
            _say(f"{repo}: custom install location "
                 + (f"set to {args.set_path}" if args.set_path else "cleared"))
            return 0
        try:
            row = db.get_update(conn, repo)
            if repo == updates.self_repo(cfg):
                _say(f"{repo} is codeHOUND itself — use 'codehound self-update' "
                     "(it quits and swaps the running app)")
                return 1
            staged = (row["staged_dir"] or "") if row else ""
            if not args.system and staged and not args.exe \
                    and Path(staged).is_dir() \
                    and row["staged_tag"] == row["latest_tag"]:
                # already downloaded + verified — just move it into place
                _say(updates.activate_staged(conn, repo, progress=_say))
            elif args.system:
                _say(updates.system_package_install(cfg, conn, repo, progress=_say))
            else:
                _say(updates.install_update(cfg=load_config(), conn=conn, repo=repo,
                                            progress=_say, exe_name=args.exe))
        except SyncError as e:
            _say(f"update: {e}")
            return 1
        return 0
    finally:
        conn.close()


def cmd_self_update(args) -> int:
    from . import db, updates
    from .providers.base import SyncError

    cfg = load_config()
    conn = db.connect()
    try:
        _say(updates.self_update(cfg, conn, progress=_say,
                                 stage_only=args.stage))
        return 0
    except SyncError as e:
        _say(f"self-update: {e}")
        return 1
    finally:
        conn.close()


def cmd_install_autostart(args) -> int:
    from .install import install_autostart
    return install_autostart()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codehound",
        description="Hunts down git repos and stray software projects across your disks.",
    )
    parser.add_argument("--version", action="version", version=f"codehound {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="scan configured roots and update the database")
    p.add_argument("--root", action="append", help="scan only this root (repeatable)")
    p.add_argument("--verbose", "-v", action="store_true", help="print each project found")
    p.add_argument("--no-enrich", action="store_true", help="skip Ollama description pass")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("sync", help="clone/fetch all repos from configured accounts")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("ask", help="ask the configured AI about a project or the whole catalogue")
    p.add_argument("question", nargs="?", help="question to ask")
    p.add_argument("--path", help="ask about this project; without it, asks across the catalogue")
    p.add_argument("--refresh-digest", action="store_true",
                   help="regenerate the model-written catalogue summary and exit")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("open", help="open a project by name (fuzzy) in the first launcher")
    p.add_argument("name", help="project name or path fragment")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("janitor", help="remove local clones whose remote work is done")
    p.add_argument("--force", action="store_true", help="actually remove (default is dry-run)")
    p.set_defaults(func=cmd_janitor)

    p = sub.add_parser("app", help="run the tray app + dashboard")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("adopt", help="copy a project into the organised repos dir")
    p.add_argument("path", help="project directory to adopt")
    p.add_argument("--name", help="destination name (default: auto-unique)")
    p.set_defaults(func=cmd_adopt)

    p = sub.add_parser("new", help="create a brand-new git project in the repos dir")
    p.add_argument("name", help="project name")
    p.add_argument("--description", "-d", help="what the project is (goes in the README)")
    p.add_argument("--scaffold", action="store_true",
                   help="ask the configured AI to write starter files (falls back to a plain README)")
    p.add_argument("--dest", help="destination directory (default: [adopt] dest)")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("clone", help="clone a git URL or local repo into the clone dir")
    p.add_argument("source", help="git URL or path to a local repository")
    p.add_argument("--name", help="destination name (default: from the source)")
    p.add_argument("--dest", help="destination directory (default: [clone] dest)")
    p.set_defaults(func=cmd_clone)

    p = sub.add_parser("worktree", help="add a branch as a sibling worktree of a project")
    p.add_argument("path", help="existing git project to branch from")
    p.add_argument("branch", help="branch to create or check out")
    p.add_argument("--dest", help="parent directory for the worktree (default: beside the project)")
    p.set_defaults(func=cmd_worktree)

    p = sub.add_parser("detect", help="detect installed editors, AI agent CLIs, and local AI services")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("updates", help="check GitHub releases of watched repos for updates")
    p.add_argument("--check", action="store_true", help="refresh from GitHub first")
    p.add_argument("--force", action="store_true", help="run even if watching is disabled")
    p.add_argument("--auto", metavar="REPO", help="set auto-install mode for owner/repo")
    p.add_argument("--mode", choices=["auto", "never", "default"], default="default",
                   help="mode for --auto (default: follow global)")
    p.add_argument("--activate", action="store_true",
                   help="install every staged (downloaded + verified) update")
    p.add_argument("--no-auto", action="store_true",
                   help="with --check, skip the auto-update policy pass")
    p.set_defaults(func=cmd_updates)

    p = sub.add_parser("update", help="install (or roll back) a watched repo's latest release")
    p.add_argument("repo", help="owner/repo (or a name fragment) on the watch list")
    p.add_argument("--rollback", action="store_true", help="restore the previously installed version")
    p.add_argument("--exe", help="executable name inside the asset, when it can't be guessed")
    p.add_argument("--set-path", metavar="PATH",
                   help="install this repo here instead (blank clears the override)")
    p.add_argument("--system", action="store_true",
                   help="install an RPM/DEB system-wide via pkexec instead of user-land")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("self-update", help="update codeHOUND itself from its own releases")
    p.add_argument("--stage", action="store_true",
                   help="download + verify only, don't install")
    p.set_defaults(func=cmd_self_update)

    p = sub.add_parser("install-timer", help="install + enable the systemd user scan timer")
    p.add_argument("--every", default="6h", help="scan interval (systemd time span, default 6h)")
    p.set_defaults(func=cmd_install_timer)

    p = sub.add_parser("install-autostart", help="install .desktop entries (app menu + login autostart)")
    p.set_defaults(func=cmd_install_autostart)

    args = parser.parse_args(argv)
    ensure_config()
    if args.command == "scan":
        _say(f"config: {CONFIG_PATH}")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
