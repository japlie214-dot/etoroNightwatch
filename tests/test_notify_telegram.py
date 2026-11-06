"""Tests for nightwatch.notify.telegram (DESIGN §8.1).

Entirely offline: a fake httpx-shaped client stands in for the network, so no
test here ever makes a real HTTP call.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from nightwatch.config import TelegramConfig
from nightwatch.db.repo import Repo
from nightwatch.host import DesktopHost
from nightwatch.logging_setup import configure_logging, log_path_for_today
from nightwatch.notify.telegram import MESSAGE_LIMIT, TelegramNotifier, split_message

FAKE_TOKEN = "123456789:AAFakeTelegramTokenForTestsOnly12345"
FAKE_CHAT_ID = "987654321"


def _configured_telegram(**overrides: Any) -> TelegramConfig:
    secrets = {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": FAKE_CHAT_ID}
    return TelegramConfig(enabled=True, secrets=secrets, **overrides)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        json_body: dict[str, Any] | None = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text or (str(json_body) if json_body is not None else "")
        self.headers = headers or {}

    def json(self) -> Any:
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


class FakeClient:
    """Stands in for httpx.Client. ``responses`` is consumed in order for
    POSTs (one entry per HTTP attempt); a callable entry raises instead of
    returning, to simulate a network error."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.post_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self._get_response: Any = FakeResponse(200, {"result": []})

    def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
        self.post_calls.append({"url": url, "json": json})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url: str, params: dict[str, Any]) -> FakeResponse:
        self.get_calls.append({"url": url, "params": params})
        if isinstance(self._get_response, Exception):
            raise self._get_response
        return self._get_response

    def set_get_response(self, response: Any) -> None:
        self._get_response = response


def _no_sleep(seconds: float) -> None:
    pass


# -- split_message -------------------------------------------------------


def test_split_message_under_limit_is_unchanged() -> None:
    assert split_message("hello") == ["hello"]


def test_split_message_over_limit_splits_with_markers() -> None:
    text = "x" * (MESSAGE_LIMIT * 2 + 500)
    chunks = split_message(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MESSAGE_LIMIT
    # every chunk beyond the first (and this one) carries a "(i/n)" marker
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        assert chunk.endswith(f"({i}/{total})")
    # reconstructing the body (stripping markers) recovers the original text
    rebuilt = "".join(c.rsplit(" (", 1)[0] for c in chunks)
    assert rebuilt == text


def test_split_message_exactly_at_limit_is_unchanged() -> None:
    text = "y" * MESSAGE_LIMIT
    assert split_message(text) == [text]


# -- disabled mode: must never raise --------------------------------------


def test_disabled_when_secrets_absent_never_raises(repo: Repo) -> None:
    config = TelegramConfig(enabled=True, secrets={})
    # Attach our own handler directly to the module logger rather than
    # relying on caplog's root-attached handler: another test module may
    # have set nightwatch.propagate = False via configure_logging(), which
    # would otherwise stop records reaching a root-attached capture handler.
    logger = logging.getLogger("nightwatch.notify.telegram")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        notifier = TelegramNotifier(config, repo, client=FakeClient())
    finally:
        logger.removeHandler(handler)
    assert notifier.enabled is False
    assert any("disabled" in r.getMessage().lower() for r in records)

    notification = notifier.send("should be a no-op", event="heartbeat")
    assert notification.status == "skipped"
    assert notification.error is not None


def test_disabled_when_telegram_enabled_false(repo: Repo) -> None:
    config = TelegramConfig(
        enabled=False,
        secrets={"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": FAKE_CHAT_ID},
    )
    notifier = TelegramNotifier(config, repo, client=FakeClient())
    assert notifier.enabled is False
    notification = notifier.send("no-op")
    assert notification.status == "skipped"


def test_disabled_get_updates_returns_empty_list(repo: Repo) -> None:
    config = TelegramConfig(enabled=True, secrets={})
    notifier = TelegramNotifier(config, repo, client=FakeClient())
    assert notifier.get_updates() == []


# -- successful send --------------------------------------------------------


def test_successful_send_records_sent_notification(repo: Repo) -> None:
    client = FakeClient(responses=[FakeResponse(200, {"result": {"message_id": 42}})])
    notifier = TelegramNotifier(
        _configured_telegram(), repo, environment="demo", client=client, sleep=_no_sleep
    )
    notification = notifier.send("hello", event="heartbeat", trading_date="2026-01-02")
    assert notification.status == "sent"
    assert notification.telegram_message_id == "42"
    assert len(client.post_calls) == 1
    assert client.post_calls[0]["json"]["chat_id"] == FAKE_CHAT_ID
    assert client.post_calls[0]["json"]["text"] == "hello"


def test_send_notification_row_written_before_http_call(repo: Repo) -> None:
    """A notifications row must exist even if the HTTP call fails outright."""
    client = FakeClient(responses=[httpx.ConnectError("boom")] * 5)
    notifier = TelegramNotifier(
        _configured_telegram(), repo, client=client, sleep=_no_sleep
    )
    before = len(repo.pending_notifications())
    notifier.send("will fail", event="abort")
    # a failed row exists (not pending, not silently dropped)
    row = repo.conn.execute(
        "SELECT status FROM notifications ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "failed"


# -- retry-then-succeed -------------------------------------------------------


def test_retries_transient_network_error_then_succeeds(repo: Repo) -> None:
    client = FakeClient(
        responses=[
            httpx.ConnectError("connection reset"),
            httpx.ConnectError("connection reset"),
            FakeResponse(200, {"result": {"message_id": 7}}),
        ]
    )
    notifier = TelegramNotifier(
        _configured_telegram(), repo, client=client, sleep=_no_sleep
    )
    notification = notifier.send("retry me", event="fill")
    assert notification.status == "sent"
    assert len(client.post_calls) == 3


def test_retries_5xx_then_succeeds(repo: Repo) -> None:
    client = FakeClient(
        responses=[
            FakeResponse(502, text="bad gateway"),
            FakeResponse(200, {"result": {"message_id": 8}}),
        ]
    )
    notifier = TelegramNotifier(
        _configured_telegram(), repo, client=client, sleep=_no_sleep
    )
    notification = notifier.send("retry 5xx", event="fill")
    assert notification.status == "sent"
    assert len(client.post_calls) == 2


# -- 429 honours retry_after ---------------------------------------------------


def test_429_honours_retry_after_then_succeeds(repo: Repo) -> None:
    sleeps: list[float] = []
    client = FakeClient(
        responses=[
            FakeResponse(429, {"ok": False, "parameters": {"retry_after": 3}}),
            FakeResponse(200, {"result": {"message_id": 9}}),
        ]
    )
    notifier = TelegramNotifier(
        _configured_telegram(), repo, client=client, sleep=sleeps.append
    )
    notification = notifier.send("rate limited", event="fill")
    assert notification.status == "sent"
    assert sleeps == [3.0]


def test_429_exhausting_retries_is_recorded_failed(repo: Repo) -> None:
    client = FakeClient(
        responses=[FakeResponse(429, {"parameters": {"retry_after": 0.01}})] * 5
    )
    notifier = TelegramNotifier(
        _configured_telegram(), repo, client=client, sleep=_no_sleep
    )
    notification = notifier.send("always limited", event="fill")
    assert notification.status == "failed"
    assert notification.error is not None
    assert "429" in notification.error


@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize("source", ["body", "header"])
def test_429_long_retry_after_is_capped(repo, exhausted, source) -> None:
    from nightwatch.notify.telegram import _MAX_ATTEMPTS, _MAX_DELAY_SECONDS

    limited = (FakeResponse(429, {"parameters": {"retry_after": 600}})
               if source == "body" else FakeResponse(429, headers={"Retry-After": "600"}))
    responses = ([limited] * _MAX_ATTEMPTS if exhausted else
                 [limited, FakeResponse(200, {"result": {"message_id": 9}})])
    client = FakeClient(responses)
    sleeps = []
    notifier = TelegramNotifier(
        _configured_telegram(), repo, client=client, sleep=sleeps.append,
    )
    result = notifier.send("bounded rate limit", event="fill")
    assert result.status == ("failed" if exhausted else "sent")
    assert len(client.post_calls) == (_MAX_ATTEMPTS if exhausted else 2)
    assert sleeps == [_MAX_DELAY_SECONDS] * (_MAX_ATTEMPTS - 1 if exhausted else 1)


# -- permanent 400-class is never retried --------------------------------------


def test_permanent_400_is_not_retried(repo: Repo) -> None:
    client = FakeClient(responses=[FakeResponse(400, text="bad request: chat not found")])
    notifier = TelegramNotifier(
        _configured_telegram(), repo, client=client, sleep=_no_sleep
    )
    notification = notifier.send("bad chat id", event="fill")
    assert notification.status == "failed"
    assert len(client.post_calls) == 1  # not retried
    assert "400" in (notification.error or "")


def test_403_forbidden_is_not_retried(repo: Repo) -> None:
    client = FakeClient(responses=[FakeResponse(403, text="bot was blocked")])
    notifier = TelegramNotifier(
        _configured_telegram(), repo, client=client, sleep=_no_sleep
    )
    notifier.send("blocked", event="fill")
    assert len(client.post_calls) == 1


# -- long message is split across multiple sendMessage calls -------------------


def test_long_message_is_split_into_multiple_posts(repo: Repo) -> None:
    text = "z" * (MESSAGE_LIMIT + 100)
    client = FakeClient(
        responses=[
            FakeResponse(200, {"result": {"message_id": 1}}),
            FakeResponse(200, {"result": {"message_id": 2}}),
        ]
    )
    notifier = TelegramNotifier(
        _configured_telegram(), repo, client=client, sleep=_no_sleep
    )
    notification = notifier.send(text, event="decision")
    assert notification.status == "sent"
    assert len(client.post_calls) == 2
    for call in client.post_calls:
        assert len(call["json"]["text"]) <= MESSAGE_LIMIT


# -- typed helper methods tag environment unmistakably -------------------------


def test_typed_events_tag_demo_and_real_unmistakably(repo: Repo) -> None:
    client = FakeClient(
        responses=[FakeResponse(200, {"result": {"message_id": i}}) for i in range(1, 8)]
    )
    demo = TelegramNotifier(
        _configured_telegram(), repo, environment="demo", client=client, sleep=_no_sleep
    )
    demo.session_start(trading_date="2026-01-02", target_time="15:30 ET", mode="dry_run")
    demo.preflight_result(trading_date="2026-01-02", passed=True)
    demo.decision(
        trading_date="2026-01-02", cone_z=-2.5, spy_close=470.0, drawdown=-0.05,
        tier=0, budget=2000.0, cash=500.0, intents=[],
    )
    demo.fill(
        trading_date="2026-01-02", symbol="UPRO", side="buy", amount_usd=100.0,
        units=1.5, price=66.0, order_id="ord-1",
    )
    demo.session_end(
        trading_date="2026-01-02", positions_summary="1 lot", equity=1000.0,
        cash=500.0, pnl=10.0, next_run_at="2026-01-05T15:15:00+00:00",
    )
    demo.abort(trading_date="2026-01-02", reason="gate 3 failed: no network")
    demo.heartbeat(trading_date="2026-01-02", next_run_at="2026-01-05T15:15:00+00:00")

    for call in client.post_calls:
        assert call["json"]["text"].startswith("[DEMO]")

    real = TelegramNotifier(
        _configured_telegram(),
        repo,
        environment="real",
        client=FakeClient(responses=[FakeResponse(200, {"result": {"message_id": 1}})]),
        sleep=_no_sleep,
    )
    notification = real.heartbeat(trading_date="2026-01-02", next_run_at=None)
    assert notification.status == "sent"
    row = repo.conn.execute(
        "SELECT body FROM notifications WHERE id = (SELECT MAX(id) FROM notifications)"
    ).fetchone()
    assert row["body"].startswith("[REAL]")


# -- get_updates ----------------------------------------------------------------


def test_get_updates_returns_result_list(repo: Repo) -> None:
    client = FakeClient()
    client.set_get_response(FakeResponse(200, {"ok": True, "result": [{"update_id": 1}]}))
    notifier = TelegramNotifier(_configured_telegram(), repo, client=client)
    updates = notifier.get_updates()
    assert updates == [{"update_id": 1}]


def test_get_updates_network_error_returns_empty_list(repo: Repo) -> None:
    client = FakeClient()
    client.set_get_response(httpx.ConnectError("down"))
    notifier = TelegramNotifier(_configured_telegram(), repo, client=client)
    assert notifier.get_updates() == []


# -- secrets never leak into the log file (redaction) --------------------------


def test_fake_token_never_appears_in_todays_log_on_send_failure(
    host: DesktopHost, repo: Repo
) -> None:
    configure_logging(host=host)
    try:
        client = FakeClient(responses=[FakeResponse(400, text="bad request")])
        notifier = TelegramNotifier(
            _configured_telegram(), repo, client=client, sleep=_no_sleep
        )
        logger = logging.getLogger("nightwatch.notify.telegram")
        notification = notifier.send("will fail loudly", event="abort")
        assert notification.status == "failed"
        # Force a flush by explicitly emitting one more record via the same
        # logger path used inside TelegramNotifier._send_one on failure.
        logger.error("token was %s", FAKE_TOKEN)
    finally:
        logging.shutdown()

    log_path = log_path_for_today(host)
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in contents
    assert "REDACTED" in contents
