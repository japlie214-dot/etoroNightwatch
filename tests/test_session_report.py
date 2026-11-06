"""Session reports and persisted wake times, without network calls."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json

import httpx
from unittest.mock import Mock

import pytest

from nightwatch import cli
from nightwatch.engine import session
from nightwatch.engine.halt import write_halt
from nightwatch.engine.preflight import GateSpec, gate_1_halt
from nightwatch.notify.commands import _next_reply
from .test_notify_commands import _config
from .test_notify_poll import FakeNotifier, update
from .test_session_skeleton import config, no_network, FakeClient, fake_gates
from .test_session_market import FakeTiingo


DAY = date(2026, 10, 8)
NOW = datetime(2026, 10, 8, 14, tzinfo=timezone.utc)
NEXT = "2026-10-08T19:05:00+00:00"


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
    assert repo.get_session(result.session_id).next_run_at == "2026-10-09T19:05:00+00:00"


@pytest.mark.parametrize("instant,expected", [
    ("2026-10-08T19:10:00+00:00", "2026-10-09T19:05:00+00:00"),
    ("2026-11-25T21:00:00+00:00", "2026-11-27T17:05:00+00:00"),
    ("2026-10-30T20:00:00+00:00", "2026-11-02T20:05:00+00:00"),
    ("2026-03-06T21:00:00+00:00", "2026-03-09T19:05:00+00:00"),
])
def test_report_matches_android_wake(repo, host, config, core_result, instant, expected):
    from nightwatch.android_entry import next_session_wake_epoch_ms
    from nightwatch.market.calendar import session_close_datetime

    result, _, _ = core_result()
    now = datetime.fromisoformat(instant)
    notifier = Mock()
    session.run_session(
        host=host, config=config, repo=repo, client=FakeClient(), tiingo=FakeTiingo(),
        trading_date=DAY, now=lambda: now, notifier=notifier,
    )
    persisted = repo.get_session(result.session_id).next_run_at
    wake = datetime.fromtimestamp(next_session_wake_epoch_ms(int(now.timestamp() * 1000)) / 1000,
                                  timezone.utc)
    assert persisted == expected == wake.isoformat(timespec="seconds")
    assert wake == session_close_datetime(wake.astimezone(session_close_datetime(DAY).tzinfo).date()) - timedelta(minutes=55)
    assert notifier.send.call_args.args[0].endswith(f"next run: {expected}")
    assert _next_reply(repo) == f"next run: {expected}"


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


@pytest.mark.parametrize("surface", ["cli", "android"])
@pytest.mark.parametrize("telegram_enabled", [False, True])
def test_production_client_sends_one_redacted_drift(host, config, monkeypatch, surface, telegram_enabled):
    from nightwatch import android_entry
    from nightwatch.db.repo import Repo, default_db_path
    from nightwatch.etoro.client import EtoroClient
    from nightwatch.notify.telegram import TelegramNotifier

    marker = "PRIVATE_PAYLOAD_MARKER"
    config = replace(config,
        etoro=replace(config.etoro, secrets={
            "ETORO_PUBLIC_KEY": "test-public", "ETORO_DEMO_PRIVATE_KEY": marker,
        }),
        telegram=replace(config.telegram, enabled=telegram_enabled, secrets={
            "TELEGRAM_BOT_TOKEN": "test-bot", "TELEGRAM_CHAT_ID": "test-chat",
        }),
    )
    sent, clients, notifiers = [], [], []

    def send(http, request, **kwargs):
        if request.url.host == "api.telegram.org":
            assert request.url.path.endswith("/sendMessage")
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"result": {"message_id": 1}}, request=request)
        assert request.method == "GET"
        return httpx.Response(200, json={"rates": [{
            "instrumentID": 424242, "ask": 2.0, "lastExecution": 1.9,
            "date": "now", "privateData": marker,
        }]}, request=request)

    def make_client(*args, **kwargs):
        client = EtoroClient(*args, **kwargs)
        clients.append(client)
        return client

    def make_notifier(*args, **kwargs):
        notifier = TelegramNotifier(*args, **kwargs)
        notifiers.append(notifier)
        return notifier

    def run_session(**kwargs):
        assert kwargs["client"]._contract_notifier is kwargs["notifier"]
        for _ in range(2):
            assert kwargs["client"].get_rates([424242])[424242].bid == 0.0
        return session.SessionResult(None, "would_have_done", None, "offline")

    module = cli if surface == "cli" else android_entry
    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(module, "load_config", lambda **kwargs: config)
    monkeypatch.setattr(module, "EtoroClient", make_client)
    monkeypatch.setattr(module, "TelegramNotifier", make_notifier)
    monkeypatch.setattr(module, "TiingoClient", lambda *args: FakeTiingo())
    monkeypatch.setattr(module, "run_session", run_session)
    if surface == "cli":
        monkeypatch.setattr(cli, "get_host", lambda: host)
        assert cli.main(["run", "--date", DAY.isoformat(), "--no-wait"]) == 0
    else:
        result = json.loads(android_entry.run_session_dry(
            str(host.root), now_utc=datetime(2026, 10, 8, 19, 10, tzinfo=timezone.utc),
        ))
        assert result["status"] == "would_have_done"
    assert len(clients) == len(notifiers) == 1
    assert clients[0]._http.is_closed and notifiers[0]._client.is_closed
    with Repo.open(default_db_path(host), host=host) as repo:
        rows = repo.conn.execute("SELECT event, body, status FROM notifications").fetchall()
    assert len(rows) == 1
    assert rows[0]["event"] == "etoro_contract_drift"
    assert rows[0]["status"] == ("sent" if telegram_enabled else "skipped")
    assert "rates payload contract drift" in rows[0]["body"]
    assert "missing" in rows[0]["body"] and "bid" in rows[0]["body"]
    assert marker not in rows[0]["body"] and "424242" not in rows[0]["body"]
    assert len(sent) == int(telegram_enabled)
    if telegram_enabled:
        assert sent[0]["text"] == rows[0]["body"]


@pytest.mark.parametrize("resume", [False, True])
def test_pending_resume_applied_before_gate_1(repo, host, monkeypatch, resume):
    write_halt(host, reason="original", source="session", now=NOW)
    assert not gate_1_halt(host).passed
    pending = [update(1, "/resume ok", ts=int(NOW.timestamp()) + 1)] if resume else []
    notifier = FakeNotifier(pending, []) if resume else FakeNotifier([])
    called = []

    def gates(**kwargs):
        return [GateSpec(1, "halt", lambda: gate_1_halt(host)), *fake_gates(called, 2)[1:]]

    monkeypatch.setattr(session, "build_preflight_gates", gates)
    result = run(host, _config(), repo, notifier=notifier)
    assert result.failed_gate == (2 if resume else 1)
    assert called == ([2] if resume else [])
    checks = repo.get_preflight_checks(result.session_id)
    assert checks[0].passed is resume
    assert host.halt_flag_path().exists() is not resume
    assert notifier.calls[0] == ("get_updates", None)
    if resume:
        assert notifier.calls[2] == ("get_updates", 2)
        assert notifier.sent[0][1] == {"event": "command_reply"}
    assert notifier.sent[-1][1]["event"] == "session_report"


def test_poll_failure_does_not_block_session(repo, host, config, core_result, caplog):
    result, core, _ = core_result()
    notifier = Mock()
    notifier.get_updates.side_effect = RuntimeError("fake poll failure")
    assert run(host, config, repo, notifier=notifier) is result
    core.assert_called_once()
    notifier.send.assert_called_once()
    assert "Session command poll failed: fake poll failure" in caplog.text
