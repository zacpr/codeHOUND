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
    return 1 if result.errors and not result.projects else 0


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

    p = sub.add_parser("app", help="run the tray app + dashboard")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("adopt", help="copy a project into the organised repos dir")
    p.add_argument("path", help="project directory to adopt")
    p.add_argument("--name", help="destination name (default: auto-unique)")
    p.set_defaults(func=cmd_adopt)

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
