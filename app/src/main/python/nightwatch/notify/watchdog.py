"""Missed-session watchdog (T075): tell the Boss when a scheduled session did not happen.

A session that never starts, never finishes, or aborts at the network gate cannot report
itself (no alarm, a crashed process, or no network to send with). This runs from the
between-session Telegram poll, so it fires as soon as the phone is reachable again. It reads
the local session table only: no broker, no strategy, no orders.

One alert per trading date, recorded only after Telegram confirms the send, so a failed send
is retried on the next poll instead of being lost.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import re

from ..market.calendar import EASTERN, is_trading_day, session_close_datetime

# Orders stop at close-25; five minutes later nothing that is going to run still can.
ALERT_AFTER_CLOSE_MINUTES = 20
LOOKBACK_DAYS = 2  # the ET date flips mid-morning in Asia; do not lose a missed day across it
TERMINAL = ("finished", "aborted", "failed")
_TOKEN = re.compile(r"^[a-z_]{1,30}$")


def _token(value: object) -> str:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else "unknown"


def missed_session_alerts(repo, now_utc: datetime, alerted: set[str]) -> list[tuple[str, str]]:
    """Return ``(trading_date, text)`` for each date needing an alert. Pure and read-only."""
    now = now_utc.astimezone(EASTERN)
    out: list[tuple[str, str]] = []
    for back in range(LOOKBACK_DAYS - 1, -1, -1):
        d: date = now.date() - timedelta(days=back)
        key = d.isoformat()
        if key in alerted or not is_trading_day(d):
            continue
        if now < session_close_datetime(d) - timedelta(minutes=ALERT_AFTER_CLOSE_MINUTES):
            continue
        row = repo.get_session_by_date(key)
        if row is None:
            out.append((key, f"Missed session {key}: no session was started on the phone."))
        elif row.state not in TERMINAL:
            out.append((key, f"Session {key} did not finish (state {_token(row.state)})."))
        elif row.state in ("aborted", "failed") and row.preflight_failed_gate == 3:
            out.append((key, f"Session {key} aborted at gate 3 (network); the phone could not "
                             "report it at the time."))
    return out


def _state_path(host) -> Path:
    return host.data_dir() / "watchdog_alerted.txt"


def load_alerted(host) -> set[str]:
    try:
        return {line.strip() for line in _state_path(host).read_text(encoding="utf-8").splitlines()
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", line.strip())}
    except OSError:
        return set()


def run_watchdog(notifier, repo, host, now_utc: datetime) -> int:
    """Send any due alerts; return how many were confirmed sent."""
    if not getattr(notifier, "enabled", False):
        return 0
    alerted = load_alerted(host)
    sent = 0
    for key, text in missed_session_alerts(repo, now_utc, alerted):
        notification = notifier.send(text, event="missed_session", trading_date=key)
        if notification.status != "sent":
            continue  # retried on the next poll
        with _state_path(host).open("a", encoding="utf-8") as handle:
            handle.write(key + "\n")
        alerted.add(key)
        sent += 1
    return sent
