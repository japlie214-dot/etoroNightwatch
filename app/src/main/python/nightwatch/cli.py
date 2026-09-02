"""Desktop dev CLI (DESIGN §2 ``cli.py``, §10 "desktop dev + tests").

Subcommands: ``run | seed | webapp | status | halt | resume | initdb``.

``initdb``, ``halt``, ``resume`` and ``status`` are real today -- they only
touch the database and the halt flag, neither of which depends on any
not-yet-built module. The rest (``run``, ``seed``, ``webapp``) need pieces
that later build steps deliver (DESIGN §10) and print a clear
"not implemented yet" message naming the blocking step, then exit non-zero,
rather than silently doing nothing or crashing with a traceback.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .config import ConfigError, load_config
from .db.repo import Repo, default_db_path
from .host import get_host

__all__ = ["main", "build_parser"]

#: Which build step (DESIGN §10) each not-yet-implemented subcommand needs.
_NOT_YET_IMPLEMENTED = {
    "run": (6, "the session engine: preflight, executor, idempotency, reconciliation"),
    "seed": (3, "the market calendar + bar store + seeding"),
    "webapp": (8, "the embedded FastAPI dashboard"),
}


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="nightwatch",
        description="etoroNightwatch desktop CLI (dev + tests). See doc/DESIGN.md.",
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="path to config.toml (default: host config dir)"
    )
    parser.add_argument(
        "--secrets", type=Path, default=None, help="path to .secrets.env (default: host config dir)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run one trading session (build step 6)")
    seed = sub.add_parser("seed", help="seed daily bar history (build step 3)")
    seed.add_argument("--count", type=int, default=400, help="bars to seed per symbol")
    sub.add_parser("webapp", help="serve the embedded dashboard (build step 8)")
    sub.add_parser("status", help="print database, schema and halt status")
    sub.add_parser("halt", help="set the HALT flag (preflight gate 1 will abort every session)")
    sub.add_parser("resume", help="clear the HALT flag")
    sub.add_parser("initdb", help="create/migrate the SQLite database")

    return parser


def _cmd_initdb(args: argparse.Namespace) -> int:
    host = get_host()
    db_path = default_db_path(host)
    with Repo.open(db_path, host=host) as repo:
        version = repo.schema_version()
        tables = repo.table_names()
    print(f"database ready: {db_path}")
    print(f"schema version: {version}")
    print(f"tables ({len(tables)}): {', '.join(tables)}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    host = get_host()
    db_path = default_db_path(host)

    try:
        config = load_config(config_path=args.config, secrets_path=args.secrets, host=host)
        print(config.describe())
    except ConfigError as exc:
        print(f"config: ERROR - {exc}")

    halt_path = host.halt_flag_path()
    halted = halt_path.exists()
    print(f"halt: {'HALTED' if halted else 'not halted'} ({halt_path})")

    if not db_path.exists():
        print(f"database: not created yet ({db_path}) - run 'nightwatch initdb'")
        return 0

    with Repo.open(db_path, host=host) as repo:
        version = repo.schema_version()
        sessions = repo.conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
        session_count = int(sessions["n"])
        latest = repo.latest_session()

    print(f"database: {db_path}")
    print(f"schema version: {version}")
    print(f"sessions: {session_count}")
    if latest is None:
        print("latest session: none")
    else:
        print(
            "latest session: "
            f"{latest.trading_date} state={latest.state} "
            f"verdict={latest.preflight_verdict} outcome={latest.outcome!r}"
        )
    return 0


def _cmd_halt(args: argparse.Namespace) -> int:
    host = get_host()
    path = host.halt_flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("halted by nightwatch.cli halt\n", encoding="utf-8")
    print(f"HALT flag set: {path}")
    print("preflight gate 1 will abort every future session until 'nightwatch resume'.")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    host = get_host()
    path = host.halt_flag_path()
    if path.exists():
        path.unlink()
        print(f"HALT flag cleared: {path}")
    else:
        print(f"HALT flag was not set: {path}")
    return 0


def _cmd_not_implemented(command: str) -> int:
    step, what = _NOT_YET_IMPLEMENTED[command]
    print(
        f"nightwatch {command}: not implemented yet (build step {step}: {what}). "
        "See doc/DESIGN.md §10.",
        file=sys.stderr,
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "initdb":
        return _cmd_initdb(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "halt":
        return _cmd_halt(args)
    if args.command == "resume":
        return _cmd_resume(args)
    if args.command in _NOT_YET_IMPLEMENTED:
        return _cmd_not_implemented(args.command)

    parser.error(f"unknown command {args.command!r}")
    return 2  # pragma: no cover - argparse.error() exits before this


if __name__ == "__main__":
    raise SystemExit(main())
