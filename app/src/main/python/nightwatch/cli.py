"""Desktop dev CLI (DESIGN §2 ``cli.py``, §10 "desktop dev + tests").

Subcommands: ``run | seed | webapp | status | halt | resume | initdb``.

``run`` performs dry-run preflight only. ``initdb``, ``halt``, ``resume``
and ``status`` touch the database and the halt flag. The rest
(``seed``, ``webapp``) need pieces
that later build steps deliver (DESIGN §10) and print a clear
"not implemented yet" message naming the blocking step, then exit non-zero,
rather than silently doing nothing or crashing with a traceback.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from .config import ConfigError, load_config
from .db.repo import Repo, default_db_path
from .engine.session import run_session
from .etoro.client import EtoroClient
from .host import Host, get_host

__all__ = ["main", "build_parser", "set_halt", "clear_halt"]

#: Which build step (DESIGN §10) each not-yet-implemented subcommand needs.
_NOT_YET_IMPLEMENTED = {
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

    run = sub.add_parser("run", help="run one trading session (dry-run preflight)")
    run.add_argument("--date", type=date.fromisoformat, default=None, help="trading date YYYY-MM-DD")
    seed = sub.add_parser("seed", help="seed daily bar history (build step 3)")
    seed.add_argument("--count", type=int, default=400, help="bars to seed per symbol")
    sub.add_parser("webapp", help="serve the embedded dashboard (build step 8)")
    sub.add_parser("status", help="print database, schema and halt status")
    sub.add_parser("halt", help="set the HALT flag (preflight gate 1 will abort every session)")
    sub.add_parser("resume", help="clear the HALT flag")
    sub.add_parser("initdb", help="create/migrate the SQLite database")
    sub.add_parser(
        "telegram-test",
        help="smoke-test Telegram: report config, send one message, check inbound",
    )

    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    host = get_host()
    try:
        config = load_config(config_path=args.config, secrets_path=args.secrets, host=host)
    except ConfigError as exc:
        print(f"config: ERROR - {exc}", file=sys.stderr)
        return 1
    trading_date = args.date or datetime.now(ZoneInfo("America/New_York")).date()
    with Repo.open(default_db_path(host), host=host) as repo:
        client = EtoroClient(config.etoro, role="read", repo=repo)
        try:
            result = run_session(
                host=host, config=config, repo=repo, client=client, trading_date=trading_date,
            )
            print(f"status: {result.status}")
            print(f"session_id: {result.session_id}")
            print(f"failed_gate: {result.failed_gate}")
            print(f"reason: {result.reason}")
            if result.session_id is not None:
                for row in repo.get_preflight_checks(result.session_id):
                    reason = json.loads(row.detail)["reason"]
                    print(f"gate {row.gate} {row.name} {'PASS' if row.passed else 'FAIL'} {reason}")
        finally:
            client.close()
    return 0 if result.status in ("preflight_passed", "already_ran") else 1


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


def set_halt(host: Host) -> Path:
    """Create the HALT flag file (DESIGN §5 gate 1, §7.2 ``/halt``).

    Shared by the ``halt`` CLI subcommand and the Telegram ``/halt`` command
    (:mod:`nightwatch.notify.commands`) so the two never drift apart.
    """
    path = host.halt_flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("halted by nightwatch.cli halt\n", encoding="utf-8")
    return path


def clear_halt(host: Host) -> bool:
    """Remove the HALT flag file if present. Returns True if it existed.

    Shared by the ``resume`` CLI subcommand and the Telegram ``/resume``
    command (:mod:`nightwatch.notify.commands`).
    """
    path = host.halt_flag_path()
    existed = path.exists()
    if existed:
        path.unlink()
    return existed


def _cmd_halt(args: argparse.Namespace) -> int:
    path = set_halt(get_host())
    print(f"HALT flag set: {path}")
    print("preflight gate 1 will abort every future session until 'nightwatch resume'.")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    host = get_host()
    path = host.halt_flag_path()
    if clear_halt(host):
        print(f"HALT flag cleared: {path}")
    else:
        print(f"HALT flag was not set: {path}")
    return 0


def _cmd_telegram_test(args: argparse.Namespace) -> int:
    """Verify Telegram before any eToro credentials exist.

    Reports whether the token and chat id are configured (never their
    values), sends one test message, polls ``getUpdates`` once, and prints a
    clear PASS/FAIL summary. This is meant to run standalone, with nothing
    else built yet, so it does its own tiny bit of config loading rather
    than depending on the session engine.

    The report is printed as two clearly distinct things: the ``enabled``
    *feature toggle* from ``config.toml`` (does the user want Telegram at
    all) versus the *runtime state* (are credentials actually present) --
    printing both under the same word ("enabled") reads as a
    self-contradiction, so they get separate, unambiguous labels.
    """
    import logging

    from .db.repo import Repo, default_db_path
    from .notify.telegram import TelegramNotifier

    host = get_host()
    ok = True

    try:
        config = load_config(config_path=args.config, secrets_path=args.secrets, host=host)
    except ConfigError as exc:
        print(f"config: ERROR - {exc}", file=sys.stderr)
        print("FAIL: could not load config.toml")
        return 1

    has_token = bool(config.telegram.secrets.get("TELEGRAM_BOT_TOKEN"))
    has_chat_id = bool(config.telegram.secrets.get("TELEGRAM_CHAT_ID"))
    has_command_secret = config.telegram.has_command_secret

    # TelegramNotifier.__init__ logs a warning when disabled, and with no
    # handler configured Python's logging "handler of last resort" prints it
    # straight to stderr -- above everything else we print, burying the
    # header below. We report the same fact ourselves, explicitly and in
    # context, so demote that one log line to DEBUG for the life of this
    # command rather than let it print out of order.
    telegram_logger = logging.getLogger("nightwatch.notify.telegram")
    previous_level = telegram_logger.level
    telegram_logger.setLevel(logging.ERROR)

    print("Telegram configuration")
    print(f"  enabled in config.toml         : {'yes' if config.telegram.enabled else 'no'}")
    print(f"  TELEGRAM_BOT_TOKEN set         : {'yes' if has_token else 'no'}")
    print(f"  TELEGRAM_CHAT_ID set           : {'yes' if has_chat_id else 'no'}")
    print(f"  NIGHTWATCH_COMMAND_SECRET set  : {'yes' if has_command_secret else 'no'}")

    try:
        with Repo.open(default_db_path(host), host=host) as repo:
            notifier = TelegramNotifier(
                config.telegram, repo, environment=config.environment.value
            )
            try:
                if not notifier.enabled:
                    ok = False
                    print(f"  runtime state                  : DISABLED ({notifier.disabled_reason})")
                else:
                    print("  runtime state                  : ENABLED")
                    print("sending one test message...")

                notification = notifier.send(
                    "etoroNightwatch telegram-test: this is a smoke test.",
                    event="message",
                )
                print(f"send status: {notification.status}" + (
                    f" ({notification.error})" if notification.error else ""
                ))
                if notification.status not in ("sent", "skipped"):
                    ok = False

                updates = notifier.get_updates()
                print(f"pending inbound updates: {len(updates)}")
            finally:
                notifier.close()
    finally:
        telegram_logger.setLevel(previous_level)

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


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

    if args.command == "run":
        return _cmd_run(args)
    if args.command == "initdb":
        return _cmd_initdb(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "halt":
        return _cmd_halt(args)
    if args.command == "resume":
        return _cmd_resume(args)
    if args.command == "telegram-test":
        return _cmd_telegram_test(args)
    if args.command in _NOT_YET_IMPLEMENTED:
        return _cmd_not_implemented(args.command)

    parser.error(f"unknown command {args.command!r}")
    return 2  # pragma: no cover - argparse.error() exits before this


if __name__ == "__main__":
    raise SystemExit(main())
