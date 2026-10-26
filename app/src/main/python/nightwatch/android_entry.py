"""Dry-run desktop-host simulation on Android, not an AndroidHost.

Power gates are assumed satisfied; Kotlin's SharedPreferences HALT is not the
Python HALT file. Provision a fresh, separately migrated phone DB, never the
desktop book: reconciliation against existing positions may legitimately fail.
Current alarms are not market-calendar/DST-aware production scheduling.

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

from .config import load_config
from .db.repo import Repo, default_db_path
from .engine.session import run_session
from .etoro.client import EtoroClient
from .host import DesktopHost, set_host
from .market.calendar import EASTERN, is_trading_day, session_close_datetime
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
})


def run_session_dry(base_dir: str, *, now_utc: datetime | None = None) -> str:
    """Return one sanitized JSON envelope. ``now_utc`` is an aware test hook only."""
    if now_utc is not None and (
        not isinstance(now_utc, datetime) or now_utc.utcoffset() is None
    ):
        raise ValueError("now_utc must be an aware datetime")
    envelope = dict(status="error", armed=False, session_id=None, failed_gate=None,
                    reason_code="entry_error", error_type=None, gates=[])
    try:
        os.environ.pop("NIGHTWATCH_ARM", None)
        host = DesktopHost(Path(base_dir))
        set_host(host)
        config = load_config(host=host)
        if config.is_armed:
            envelope.update(status="refused", reason_code="armed_not_allowed")
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
                                if previous is not None:
                                    envelope["session_id"] = previous.id
                                    # The engine persists prose, not a status column. This is its
                                    # dry-run success prefix plus terminal state, not just existence.
                                    successful = (
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
                                    client = EtoroClient(config.etoro, role="read", repo=repo)
                                    resources.callback(client.close)
                                    notifier = TelegramNotifier(config.telegram, repo, environment=config.environment.value)
                                    resources.callback(notifier.close)
                                    tiingo = TiingoClient(config.tiingo)
                                    if hasattr(tiingo, "close"):
                                        resources.callback(tiingo.close)
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
    try:
        return json.dumps(envelope)
    except Exception as exc:
        # No exception text, even if serialization itself is broken.
        return ('{"status":"error","armed":false,"session_id":null,"failed_gate":null,'
                '"reason_code":"serialization_error","error_type":"' + type(exc).__name__
                + '","gates":[]}')
