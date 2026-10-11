"""codehound command-line entry point."""
from __future__ import annotations

import argparse
import sys

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
            _say(f"classifying {len(result.candidates)} ambiguous dirs…")
            conn = db.connect()
            import os
            for path in result.candidates:
                if ollama.classify(cfg, path):
                    proj = {
                        "path": path, "kind": "ungitted",
                        "name": os.path.basename(path.rstrip("/")),
                        "root": next((r for r in cfg.roots if path.startswith(r)), ""),
                        "language": None, "branch": None, "remote_url": None,
                        "last_commit_date": None, "last_commit_msg": None,
                        "dirty": 0, "created": None, "modified": None, "size_approx": None,
                    }
                    db.upsert_project(conn, proj)
                    if args.verbose:
                        _say(f"  [llm] {path}")
            conn.commit()
            conn.close()
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


def cmd_install_timer(args) -> int:
    from .install import install_timer
    return install_timer(every=args.every)


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

    p = sub.add_parser("detect", help="detect installed editors, AI agent CLIs, and local AI services")
    p.set_defaults(func=cmd_detect)

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
