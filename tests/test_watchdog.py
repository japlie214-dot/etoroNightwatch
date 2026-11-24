"""T075: missed-session watchdog. Real market calendar, fake repo and notifier, no network."""

from datetime import datetime, timezone
from types import SimpleNamespace

from nightwatch.market.calendar import EASTERN
from nightwatch.notify import watchdog


def et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=EASTERN).astimezone(timezone.utc)


class Repo:
    """Dates not given explicitly count as finished, so each test isolates one date."""

    def __init__(self, **rows):
        self.rows = rows

    def get_session_by_date(self, key):
        return self.rows[key] if key in self.rows else row("finished")


def row(state, gate=None):
    return SimpleNamespace(state=state, preflight_failed_gate=gate)


MISSING = {"2026-11-24": None}


def alerts(repo, now, alerted=()):
    return watchdog.missed_session_alerts(repo, now, set(alerted))


def test_no_alert_before_the_order_cutoff_has_passed():
    assert alerts(Repo(**MISSING), et(2026, 11, 24, 15, 39)) == []  # close 16:00, alerts from 15:40
    assert len(alerts(Repo(**MISSING), et(2026, 11, 24, 15, 40))) == 1


def test_missing_session_names_the_date():
    (key, text), = alerts(Repo(**MISSING), et(2026, 11, 24, 18, 0))
    assert key == "2026-11-24" and text.startswith("Missed session 2026-11-24")


def test_finished_session_is_silent():
    assert alerts(Repo(**{"2026-11-24": row("finished")}), et(2026, 11, 24, 18, 0)) == []


def test_unfinished_session_alerts_with_its_state():
    (_, text), = alerts(Repo(**{"2026-11-24": row("executing")}), et(2026, 11, 24, 18, 0))
    assert "did not finish (state executing)" in text


def test_state_text_cannot_inject_foreign_content():
    (_, text), = alerts(Repo(**{"2026-11-24": row("x; TOKEN=abc123")}), et(2026, 11, 24, 18, 0))
    assert "abc123" not in text and "state unknown" in text


def test_network_abort_alerts_but_other_aborts_do_not():
    net = Repo(**{"2026-11-24": row("aborted", 3)})
    (_, text), = alerts(net, et(2026, 11, 24, 18, 0))
    assert "gate 3" in text
    for gate in (None, 1, 5, 11):  # those sessions report themselves
        assert alerts(Repo(**{"2026-11-24": row("aborted", gate)}), et(2026, 11, 24, 18, 0)) == []


def test_yesterday_is_still_checked_after_the_et_date_flips():
    now = et(2026, 11, 24, 1, 0)  # just after midnight ET: Monday's problem must not be lost
    out = alerts(Repo(**{"2026-11-23": row("aborted", 3)}), now)
    assert [k for k, _ in out] == ["2026-11-23"]


def test_weekends_and_holidays_are_never_alerted():
    none = {k: None for k in ("2026-11-26", "2026-11-27", "2026-11-28")}
    assert all(k != "2026-11-28" for k, _ in alerts(Repo(**none), et(2026, 11, 28, 18, 0)))  # Saturday
    assert all(k != "2026-11-26" for k, _ in alerts(Repo(**none), et(2026, 11, 26, 18, 0)))  # Thanksgiving


def test_early_close_day_alerts_at_its_own_time():
    assert alerts(Repo(**{"2026-11-27": None}), et(2026, 11, 27, 12, 39)) == []
    out = alerts(Repo(**{"2026-11-27": None}), et(2026, 11, 27, 12, 40))  # closes 13:00
    assert [k for k, _ in out] == ["2026-11-27"]


def test_already_alerted_dates_are_skipped():
    assert alerts(Repo(**MISSING), et(2026, 11, 24, 18, 0), alerted={"2026-11-24"}) == []


class Notifier:
    def __init__(self, statuses, enabled=True):
        self.statuses, self.sent, self.enabled = list(statuses), [], enabled

    def send(self, text, **kw):
        self.sent.append((text, kw))
        return SimpleNamespace(status=self.statuses.pop(0))


def test_run_records_only_confirmed_sends_and_retries_failures(tmp_path):
    host = SimpleNamespace(data_dir=lambda: tmp_path)
    repo, now = Repo(**MISSING), et(2026, 11, 24, 18, 0)
    failing = Notifier(["failed"])
    assert watchdog.run_watchdog(failing, repo, host, now) == 0
    assert watchdog.load_alerted(host) == set()  # not recorded, so it is retried
    ok = Notifier(["sent"])
    assert watchdog.run_watchdog(ok, repo, host, now) == 1
    assert ok.sent[0][1] == {"event": "missed_session", "trading_date": "2026-11-24"}
    assert watchdog.load_alerted(host) == {"2026-11-24"}
    again = Notifier([])
    assert watchdog.run_watchdog(again, repo, host, now) == 0 and again.sent == []  # once per date


def test_disabled_notifier_sends_nothing(tmp_path):
    host = SimpleNamespace(data_dir=lambda: tmp_path)
    off = Notifier([], enabled=False)
    assert watchdog.run_watchdog(off, Repo(**MISSING), host, et(2026, 11, 24, 18, 0)) == 0 and off.sent == []


def test_watchdog_source_touches_no_broker_or_order_path():
    import inspect
    src = inspect.getsource(watchdog)
    for forbidden in ("EtoroClient", "place_", "run_session", "TiingoClient", "get_portfolio"):
        assert forbidden not in src
