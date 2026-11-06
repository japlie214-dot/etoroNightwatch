"""Desktop-host sessions on Android: dry by default, explicitly armed DEMO only.

Power gates are assumed satisfied (T046); Kotlin latches the Python HALT file.
Provision a fresh, separately migrated phone DB, never the desktop book:
reconciliation against existing positions may legitimately fail.

The in-window start bounds the T-30 wait to <=35 minutes (no engine deadline
API); network retries remain bounded only by existing client timeouts.
Manual recovery: stop alarms, copy the phone DB off-device, inspect the session
and preflight/decision rows, then mark via a reviewed PM ticket. Never delete
rows on the phone.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import errno
import json
import os
from pathlib import Path

from .config import EtoroEnvironment, load_config
from .db.repo import Repo, default_db_path
from .engine.session import run_session
from .etoro.client import EtoroClient
from .host import DesktopHost, get_host, set_host
from .market.calendar import EASTERN, is_trading_day, next_close_minus, session_close_datetime
from .market.source import TiingoClient
from .notify.telegram import TelegramNotifier


@contextmanager
def _session_lock(path: Path):
    # Keep the inode: unlinking would let a second process lock a different file.
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                if path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            yield False
        else:
            yield True
        # Closing releases the kernel lock, including when the body raises.


_STATUSES = frozenset({
    "would_have_done", "already_ran", "preflight_failed", "rerun_failed",
    "signal_failed", "decide_failed", "refused_armed",
    "market_data_failed", "aborted_before_orders", "executed", "executed_partial",
    "halted_before_execution", "armed_refused_intents",
})


def next_session_wake_epoch_ms(now_epoch_ms: int | None = None) -> int:
    """Next NYSE close minus 55 minutes; the lead is a margin, not a timing guarantee."""
    now = (datetime.now(timezone.utc) if now_epoch_ms is None else
           datetime.fromtimestamp(now_epoch_ms / 1000, timezone.utc))
    return int(next_close_minus(55, after=now).timestamp() * 1000)


def run_session_dry(base_dir: str, *, now_utc: datetime | None = None) -> str:
    """Run disarmed, preserving the existing removal of NIGHTWATCH_ARM."""
    return _run_entry(base_dir, armed=False, now_utc=now_utc)


def run_session_armed_demo(base_dir: str, *, now_utc: datetime | None = None) -> str:
    """Run only the explicitly armed, dedicated demo home."""
    return _run_entry(base_dir, armed=True, now_utc=now_utc)


def _run_entry(base_dir: str, *, armed: bool, now_utc: datetime | None) -> str:
    """Return one sanitized JSON envelope. ``now_utc`` is an aware test hook only."""
    if now_utc is not None and (
        not isinstance(now_utc, datetime) or now_utc.utcoffset() is None
    ):
        raise ValueError("now_utc must be an aware datetime")
    envelope = dict(status="error", armed=False, session_id=None, failed_gate=None,
                    reason_code="entry_error", error_type=None, gates=[])
    previous_host = get_host()
    previous_arm = os.environ.get("NIGHTWATCH_ARM")
    try:
        if not armed:
            os.environ.pop("NIGHTWATCH_ARM", None)
        refusal = None
        if armed and Path(base_dir).name != "nightwatch_demo":
            refusal = "wrong_home"
        elif armed and not (Path(base_dir) / "ARM_DEMO").exists():
            refusal = "not_armed_on_device"
        else:
            if armed:
                os.environ["NIGHTWATCH_ARM"] = "1"
            host = DesktopHost(Path(base_dir))
            set_host(host)
            config = load_config(host=host)
            if armed and (config.environment is not EtoroEnvironment.DEMO or config.etoro.is_real_money):
                refusal = "real_not_allowed"
            elif armed and not config.is_armed:
                refusal = "config_not_live"
            elif not armed and config.is_armed:
                refusal = "armed_not_allowed"
        if refusal:
            envelope.update(status="refused", reason_code=refusal)
        else:
            now = (now_utc or datetime.now(timezone.utc)).astimezone(EASTERN)
            trading_date = now.date()
            close = session_close_datetime(trading_date)
            if not is_trading_day(trading_date) or not (
                close - timedelta(minutes=60) <= now <= close - timedelta(minutes=25)
            ):
                envelope.update(status="out_of_window", reason_code="outside_session_window")
            else:
                with _session_lock(host.data_dir() / "session.lock") as acquired:
                    if not acquired:
                        envelope.update(status="busy", reason_code="session_locked")
                    else:
                        with ExitStack() as resources:
                            repo = Repo.open(default_db_path(host), host=host)
                            resources.callback(repo.close)
                            try:
                                previous = repo.get_session_by_date(trading_date.isoformat())
                                if previous is not None and not (
                                    armed and previous.state not in ("finished", "aborted", "failed")
                                ):
                                    envelope["session_id"] = previous.id
                                    # The engine persists prose, not a status column. This is its
                                    # dry-run success prefix plus terminal state, not just existence.
                                    successful = (previous.state == "finished" and previous.armed) if armed else (
                                        previous.state == "finished" and previous.finished_at is not None
                                        and not previous.armed and previous.error is None
                                        and (previous.outcome or "").startswith("would have done ")
                                    )
                                    envelope.update(
                                        status="already_ran" if successful else "previous_unfinished",
                                        reason_code="already_ran" if successful else "previous_unfinished",
                                        failed_gate=previous.preflight_failed_gate,
                                    )
                                else:
                                    client = EtoroClient(config.etoro, role="write" if armed else "read", repo=repo)
                                    resources.callback(client.close)
                                    notifier = TelegramNotifier(config.telegram, repo, environment=config.environment.value)
                                    resources.callback(notifier.close)
                                    tiingo = TiingoClient(config.tiingo)
                                    if hasattr(tiingo, "close"):
                                        resources.callback(tiingo.close)
                                    envelope["armed"] = armed
                                    result = run_session(
                                        host=host, config=config, repo=repo, client=client,
                                        trading_date=trading_date, tiingo=tiingo, wait=True,
                                        notifier=notifier,
                                    )
                                    status = result.status if result.status in _STATUSES else "error"
                                    envelope.update(status=status, reason_code=status,
                                                    session_id=result.session_id, failed_gate=result.failed_gate)
                            finally:
                                # Also retain attempted gates when the engine raises after persistence.
                                row = repo.get_session_by_date(trading_date.isoformat())
                                if row is not None:
                                    envelope["session_id"] = row.id
                                    envelope["gates"] = [[r.gate, r.name, r.passed]
                                                         for r in repo.get_preflight_checks(row.id)]
    except Exception as exc:
        envelope.update(status="error", reason_code="entry_error", error_type=type(exc).__name__)
    finally:
        set_host(previous_host)
        if armed:
            if previous_arm is None:
                os.environ.pop("NIGHTWATCH_ARM", None)
            else:
                os.environ["NIGHTWATCH_ARM"] = previous_arm
    try:
        envelope["halt"] = (Path(base_dir) / "HALT").exists()
    except Exception:
        envelope["halt"] = False
    try:
        return json.dumps(envelope)
    except Exception as exc:
        # No exception text, even if serialization itself is broken.
        return ('{"status":"error","armed":false,"session_id":null,"failed_gate":null,'
                '"reason_code":"serialization_error","error_type":"' + type(exc).__name__
                + '","gates":[],"halt":' + ('true' if envelope["halt"] else 'false') + '}')
