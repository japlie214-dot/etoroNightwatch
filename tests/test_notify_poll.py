"""Pending Telegram commands, using only scripted notifier responses."""

from datetime import datetime, timezone

from nightwatch.engine.halt import read_halt, write_halt
from nightwatch.notify.poll import process_pending
from .test_notify_commands import CHAT_ID, _config


class FakeNotifier:
    def __init__(self, *batches):
        self.batches = list(batches)
        self.calls = []
        self.sent = []
        self.closed = False

    def get_updates(self, offset=None):
        self.calls.append(("get_updates", offset))
        return self.batches.pop(0)

    def send(self, text, **kwargs):
        self.calls.append(("send", text))
        self.sent.append((text, kwargs))

    def close(self):
        self.closed = True


def update(update_id, text, *, chat_id=CHAT_ID, ts=1791583200):
    return {"update_id": update_id, "message": {
        "text": text, "date": ts, "chat": {"id": int(chat_id)}, "from": {"id": 42},
    }}


def test_commands_processed_in_update_order_then_acknowledged(repo, host):
    write_halt(host, reason="original", source="session",
               now=datetime(2026, 10, 8, 13, tzinfo=timezone.utc))
    notifier = FakeNotifier([update(8, "/halt why"), update(5, "/resume checked")], [])
    results = process_pending(notifier, repo, host, _config())
    assert [r.command.command for r in results] == ["/resume checked", "/halt why"]
    assert all(r.accepted for r in results)
    assert "HALT cleared (reason: checked)" in notifier.sent[0][0]
    assert notifier.sent[1][0].startswith("HALT set.")
    assert [kwargs for _, kwargs in notifier.sent] == [{"event": "command_reply"}] * 2
    assert [call[0] for call in notifier.calls] == ["get_updates", "send", "send", "get_updates"]
    assert notifier.calls[-1] == ("get_updates", 9)
    assert read_halt(host)["reason"] == "why"
    assert [r.sender_id for r in repo.get_commands()] == ["42", "42"]


def test_non_commands_skipped_but_acknowledged(repo, host):
    notifier = FakeNotifier([update(10, "hello"), {"update_id": 12}], [])
    assert process_pending(notifier, repo, host, _config()) == []
    assert notifier.calls == [("get_updates", None), ("get_updates", 13)]
    assert notifier.sent == [] and repo.get_commands() == []


def test_raising_update_does_not_block_others_or_ack(repo, host, caplog):
    broken = update(20, "/resume checked")
    del broken["message"]["date"]
    notifier = FakeNotifier([broken, update(19, "/status"), update(21, "/help")], [])
    results = process_pending(notifier, repo, host, _config())
    assert [r.command.command for r in results] == ["/status", "/help"]
    assert len(notifier.sent) == 2
    assert "Telegram update 20 processing failed" in caplog.text
    assert notifier.calls[-1] == ("get_updates", 22)


def test_raising_last_update_is_acknowledged(repo, host, caplog):
    broken = update(30, "/status")
    del broken["message"]["chat"]
    notifier = FakeNotifier([update(2, "/help"), broken], [])
    assert len(process_pending(notifier, repo, host, _config())) == 1
    assert "Telegram update 30 processing failed" in caplog.text
    assert notifier.calls[-1] == ("get_updates", 31)


def test_empty_updates_do_not_ack(repo, host):
    notifier = FakeNotifier([])
    assert process_pending(notifier, repo, host, _config()) == []
    assert notifier.calls == [("get_updates", None)]


def test_foreign_chat_is_logged_and_never_answered(repo, host, caplog):
    notifier = FakeNotifier([update(4, "/halt why", chat_id="999")], [])
    results = process_pending(notifier, repo, host, _config())
    assert len(results) == 1 and not results[0].accepted
    assert results[0].reply is None and notifier.sent == []
    row = repo.get_commands()[0]
    assert row.verdict == "rejected" and row.reason == "unauthorized"
    assert "reason=unauthorized" in caplog.text
    assert notifier.calls[-1] == ("get_updates", 5)
    assert read_halt(host) is None


def test_message_without_sender_and_utc_timestamp(repo, host):
    now = datetime(2026, 10, 8, 14, tzinfo=timezone.utc)
    write_halt(host, reason="original", source="session", now=now)
    message = update(1, "/resume checked", ts=int(now.timestamp()))
    del message["message"]["from"]
    notifier = FakeNotifier([message], [])
    result = process_pending(notifier, repo, host, _config())[0]
    assert result.command.sender_id == ""
    assert result.reply.startswith("Ignored:")
    assert read_halt(host)["reason"] == "original"
