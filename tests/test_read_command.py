"""T075: read-only command parity over FCM. Offline; nothing real is constructed."""

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
    state = SimpleNamespace(sent=[], dispatched=[], home=home)
    host = SimpleNamespace(data_dir=lambda: home / "data")
    monkeypatch.setattr(entry, "DesktopHost", lambda path: host)
    monkeypatch.setattr(entry, "load_config", lambda host: SimpleNamespace(
        environment=EtoroEnvironment.DEMO, etoro=SimpleNamespace(is_real_money=False), telegram=object()))
    monkeypatch.setattr(entry, "default_db_path", lambda h: home / "data" / "n.db")
    monkeypatch.setattr(entry.Repo, "open", staticmethod(lambda path, host: SimpleNamespace(close=lambda: None)))

    def send(text, **kw):
        state.sent.append((text, kw))
        return SimpleNamespace(status="sent")

    monkeypatch.setattr(entry, "TelegramNotifier", lambda *a, **k: SimpleNamespace(close=lambda: None, send=send))

    def dispatch(command, repo, host_, config, **kw):
        state.dispatched.append(command)
        return f"reply:{command}"

    monkeypatch.setattr("nightwatch.notify.commands._dispatch", dispatch)
    yield state
    set_host(previous)


@pytest.mark.parametrize("command", ["positions", "pnl", "today", "next"])
def test_each_read_command_is_answered_by_telegram(wired, command):
    result = json.loads(entry.run_read_command(str(wired.home), command))
    assert result == {"status": "ok", "sent": True}
    assert wired.dispatched == [command]
    assert wired.sent[0][0] == f"reply:{command}"


@pytest.mark.parametrize("command", ["halt", "resume", "status", "logs", "dashboard", "../x", ""])
def test_anything_else_is_refused_before_any_work(wired, command):
    result = json.loads(entry.run_read_command(str(wired.home), command))
    assert result["status"] == "refused" and result["reason_code"] == "not_read_only"
    assert wired.dispatched == [] and wired.sent == []


def test_refuses_a_non_demo_home_and_a_busy_session(wired, tmp_path):
    other = tmp_path / "nightwatch_config"
    other.mkdir()
    assert json.loads(entry.run_read_command(str(other), "pnl"))["reason_code"] == "wrong_home"
    with entry._session_lock(wired.home / "data" / "session.lock") as acquired:
        assert acquired
        assert json.loads(entry.run_read_command(str(wired.home), "pnl"))["status"] == "busy"
    assert wired.dispatched == []


def test_failure_reports_only_the_error_type(wired, monkeypatch):
    monkeypatch.setattr("nightwatch.notify.commands._dispatch",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("token=SECRET_MARKER")))
    raw = entry.run_read_command(str(wired.home), "pnl")
    assert json.loads(raw) == {"status": "error", "error_type": "RuntimeError"}
    assert "SECRET_MARKER" not in raw


def test_never_touches_the_broker_or_orders():
    import inspect
    src = inspect.getsource(entry.run_read_command)
    for forbidden in ("EtoroClient", "place_", "run_session", "operator_write_authorized", "TiingoClient"):
        assert forbidden not in src
