"""T084: the between-session Telegram poll entry. Offline; nothing real is constructed."""

import json
from types import SimpleNamespace

import pytest

from nightwatch import android_entry as entry
from nightwatch.config import EtoroEnvironment
from nightwatch.host import get_host, set_host


@pytest.fixture
def wired(tmp_path, monkeypatch):
    previous = get_host()
    home = tmp_path / "nightwatch_demo"
    (home / "data").mkdir(parents=True)
    state = SimpleNamespace(closed=[], polled=[], repo_closed=[], hosts_seen=[])
    host = SimpleNamespace(data_dir=lambda: home / "data")
    monkeypatch.setattr(entry, "DesktopHost", lambda path: host)
    monkeypatch.setattr(entry, "load_config", lambda host: SimpleNamespace(
        environment=EtoroEnvironment.DEMO, etoro=SimpleNamespace(is_real_money=False),
        telegram=object()))
    monkeypatch.setattr(entry, "default_db_path", lambda h: home / "data" / "n.db")
    monkeypatch.setattr(entry.Repo, "open", staticmethod(
        lambda path, host: SimpleNamespace(close=lambda: state.repo_closed.append(1))))
    monkeypatch.setattr(entry, "TelegramNotifier", lambda *a, **k: SimpleNamespace(
        close=lambda: state.closed.append(1)))

    def process(notifier, repo, host_, config):
        state.polled.append(1)
        state.hosts_seen.append(get_host())
        return ["r1", "r2"]

    monkeypatch.setattr("nightwatch.notify.poll.process_pending", process)
    state.home, state.host = home, host
    yield state
    set_host(previous)


def test_polls_once_closes_resources_and_restores_host(wired):
    before = get_host()
    result = json.loads(entry.poll_telegram(str(wired.home)))
    assert result == {"status": "ok", "processed": 2}
    assert wired.polled == [1] and wired.closed == [1] and wired.repo_closed == [1]
    assert wired.hosts_seen == [wired.host]
    assert get_host() is before


def test_refuses_any_home_that_is_not_the_demo_home(wired, tmp_path):
    other = tmp_path / "nightwatch_config"
    other.mkdir()
    result = json.loads(entry.poll_telegram(str(other)))
    assert result["status"] == "refused" and result["reason_code"] == "wrong_home"
    assert wired.polled == []


def test_refuses_real_money_config(wired, monkeypatch):
    monkeypatch.setattr(entry, "load_config", lambda host: SimpleNamespace(
        environment=EtoroEnvironment.DEMO, etoro=SimpleNamespace(is_real_money=True), telegram=object()))
    result = json.loads(entry.poll_telegram(str(wired.home)))
    assert result["status"] == "refused" and result["reason_code"] == "real_not_allowed"
    assert wired.polled == []


def test_does_not_overlap_a_running_session(wired):
    with entry._session_lock(wired.home / "data" / "session.lock") as acquired:
        assert acquired
        result = json.loads(entry.poll_telegram(str(wired.home)))
    assert result["status"] == "busy" and result["reason_code"] == "session_locked"
    assert wired.polled == []


def test_failure_is_reported_without_text_and_host_restored(wired, monkeypatch):
    before = get_host()

    def boom(*a, **k):
        raise RuntimeError("SECRET_MARKER_123 token=abc")

    monkeypatch.setattr("nightwatch.notify.poll.process_pending", boom)
    raw = entry.poll_telegram(str(wired.home))
    result = json.loads(raw)
    assert result == {"status": "error", "processed": 0, "error_type": "RuntimeError"}
    assert "SECRET_MARKER" not in raw
    assert get_host() is before and wired.closed == [1]


def test_never_touches_the_broker_or_orders():
    import inspect
    src = inspect.getsource(entry.poll_telegram)
    for forbidden in ("EtoroClient", "place_", "run_session", "operator_write_authorized", "TiingoClient"):
        assert forbidden not in src
