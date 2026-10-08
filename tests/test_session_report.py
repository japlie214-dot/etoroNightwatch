"""Session reports and persisted wake times, without network calls."""

from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

from nightwatch import cli
from nightwatch.engine import session
from nightwatch.notify.commands import _next_reply
from .test_session_skeleton import config, no_network, FakeClient, fake_gates
from .test_session_market import FakeTiingo


DAY = date(2026, 10, 8)
NOW = datetime(2026, 10, 8, 14, tzinfo=timezone.utc)
NEXT = "2026-10-08T19:15:00+00:00"


def test_format_session_report():
    result = session.SessionResult(12, "would_have_done", None, "would have done <buy>; snapshot 1")
    assert session.format_session_report(result, trading_date=DAY, environment="demo", next_run_at=NEXT) == (
        "[DEMO] NIGHTWATCH 2026-10-08 session 12: would_have_done\n"
        "would have done <buy>; snapshot 1\n"
        f"next run: {NEXT}"
    )
    result = session.SessionResult(None, "preflight_failed", 10, None)
    assert session.format_session_report(result, trading_date=DAY, environment="real", next_run_at=NEXT) == (
        "[REAL] NIGHTWATCH 2026-10-08 no session: preflight_failed (gate 10)\n"
        f"(no reason)\nnext run: {NEXT}"
    )


@pytest.fixture
def core_result(repo, monkeypatch):
    row = repo.create_session(DAY.isoformat(), "dry_run", "demo", False)
    repo.finish_session(row.id, state="finished", outcome="original outcome")
    before = repo.get_session(row.id)

    def install(status="would_have_done", gate=None, session_id=row.id):
        result = session.SessionResult(session_id, status, gate, "full reason: <buy>; arming refused; snapshot 1")
        core = Mock(return_value=result)
        monkeypatch.setattr(session, "_run_session_core", core)
        return result, core, before

    return install


def run(host, config, repo, **kwargs):
    return session.run_session(
        host=host, config=config, repo=repo, client=FakeClient(), tiingo=FakeTiingo(),
        trading_date=DAY, now=lambda: NOW, **kwargs,
    )


@pytest.mark.parametrize("status,gate", [
    ("would_have_done", None), ("preflight_failed", 10), ("decide_failed", None),
])
def test_report_and_next_run(repo, host, config, core_result, status, gate):
    result, core, before = core_result(status, gate)
    notifier = Mock()
    sleep_until = Mock()
    assert _next_reply(repo) == "no scheduled run known yet"
    assert run(host, config, repo, notifier=notifier, wait=False, sleep_until=sleep_until) is result
    args = core.call_args.kwargs
    assert args["host"] is host and args["config"] is config and args["repo"] is repo
    assert isinstance(args["client"], FakeClient) and isinstance(args["tiingo"], FakeTiingo)
    assert args["trading_date"] == DAY and args["wait"] is False
    assert args["sleep_until"] is sleep_until
    notifier.send.assert_called_once_with(
        session.format_session_report(result, trading_date=DAY, environment=config.environment.value, next_run_at=NEXT),
        event="session_report", session_id=result.session_id, trading_date=DAY.isoformat(),
    )
    after = repo.get_session(result.session_id)
    assert after.next_run_at == NEXT
    assert after.finished_at == before.finished_at
    assert after.state == before.state and after.outcome == before.outcome
    assert _next_reply(repo) == f"next run: {NEXT}"


def test_after_wake_uses_next_trading_day(repo, host, config, core_result):
    result, _, _ = core_result()
    assert session.run_session(
        host=host, config=config, repo=repo, client=FakeClient(), tiingo=FakeTiingo(),
        trading_date=DAY, now=lambda: datetime(2026, 10, 8, 20, tzinfo=timezone.utc),
    ) is result
    assert repo.get_session(result.session_id).next_run_at == "2026-10-09T19:15:00+00:00"


def test_already_ran_is_noop(repo, host, config, core_result, monkeypatch):
    result, _, before = core_result("already_ran")
    notifier = Mock()
    update = Mock(side_effect=AssertionError("must not write"))
    monkeypatch.setattr(repo, "update_session", update)
    monkeypatch.setattr(session, "next_close_minus", Mock(side_effect=AssertionError("must return immediately")))
    assert run(host, config, repo, notifier=notifier) is result
    notifier.send.assert_not_called()
    update.assert_not_called()
    assert repo.get_session(result.session_id) == before


def test_refused_armed_reports_without_write(repo, host, config, core_result, monkeypatch):
    result, _, before = core_result("refused_armed", session_id=None)
    update = Mock(side_effect=AssertionError("must not write"))
    monkeypatch.setattr(repo, "update_session", update)
    notifier = Mock()
    assert run(host, config, repo, notifier=notifier) is result
    notifier.send.assert_called_once()
    assert "no session: refused_armed" in notifier.send.call_args.args[0]
    assert notifier.send.call_args.kwargs == {
        "event": "session_report", "session_id": None, "trading_date": DAY.isoformat(),
    }
    update.assert_not_called()
    assert repo.get_session(before.id) == before


@pytest.mark.parametrize("raises", [False, True])
def test_optional_or_failing_notifier(repo, host, config, core_result, caplog, raises):
    result, _, before = core_result()
    notifier = Mock() if raises else None
    if raises:
        notifier.send.side_effect = RuntimeError("fake send failure")
    assert run(host, config, repo, notifier=notifier) is result
    after = repo.get_session(result.session_id)
    assert after.next_run_at == NEXT and after.finished_at == before.finished_at
    if raises:
        notifier.send.assert_called_once()
        assert "Session report failed: fake send failure" in caplog.text


def test_update_failure_still_reports(repo, host, config, core_result, monkeypatch, caplog):
    result, _, before = core_result()
    monkeypatch.setattr(repo, "update_session", Mock(side_effect=RuntimeError("fake database failure")))
    notifier = Mock()
    assert run(host, config, repo, notifier=notifier) is result
    notifier.send.assert_called_once()
    assert repo.get_session(result.session_id) == before
    assert "Session next_run_at update failed: fake database failure" in caplog.text


def test_real_core_preflight_failure_reports(repo, host, config, monkeypatch):
    called = []
    monkeypatch.setattr(session, "build_preflight_gates", lambda **kwargs: fake_gates(called, 10))
    notifier = Mock()
    result = run(host, config, repo, notifier=notifier)
    assert result.status == "preflight_failed" and result.failed_gate == 10
    assert called == list(range(1, 11))
    notifier.send.assert_called_once()
    assert result.reason in notifier.send.call_args.args[0]
    assert repo.get_session(result.session_id).next_run_at == NEXT


def test_cli_notifier_lifecycle(repo, host, config, monkeypatch, capsys):
    client, tiingo, notifier = FakeClient(), FakeTiingo(), Mock()
    make_notifier = Mock(return_value=notifier)
    core = Mock(return_value=session.SessionResult(None, "refused_armed", None, "refused"))
    monkeypatch.setattr(cli, "get_host", lambda: host)
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)
    monkeypatch.setattr(cli, "EtoroClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(cli, "TiingoClient", lambda *args: tiingo)
    monkeypatch.setattr(cli, "TelegramNotifier", make_notifier)
    monkeypatch.setattr(cli, "run_session", core)
    assert cli.main(["run", "--date", DAY.isoformat(), "--no-wait"]) == 1
    assert make_notifier.call_args.args[0] is config.telegram
    assert make_notifier.call_args.args[1] is core.call_args.kwargs["repo"]
    assert make_notifier.call_args.kwargs == {"environment": config.environment.value}
    assert core.call_args.kwargs["notifier"] is notifier
    notifier.close.assert_called_once()
    assert client.closed and tiingo.closed
    assert capsys.readouterr().out == "status: refused_armed\nsession_id: None\nfailed_gate: None\nreason: refused\n"
