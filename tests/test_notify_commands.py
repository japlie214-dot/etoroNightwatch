"""Tests for nightwatch.notify.commands (DESIGN §7.2, §8.2)."""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from nightwatch.config import Config, EtoroConfig, EtoroEnvironment, RiskConfig
from nightwatch.config import StrategyConfig, SymbolConfig, TelegramConfig, TradingMode
from nightwatch.config import WebappConfig
from nightwatch.db.repo import Repo
from nightwatch.host import DesktopHost
from nightwatch.engine.halt import read_halt, write_halt
from nightwatch.notify.commands import (
    SUPPORTED_COMMANDS,
    compute_hmac,
    handle,
    normalize_command,
    verify_hmac,
)

CHAT_ID = "111222333"
SECRET = "test-command-secret"


def _config(**telegram_overrides: object) -> Config:
    telegram = TelegramConfig(
        enabled=True,
        secrets={
            "TELEGRAM_BOT_TOKEN": "123456789:AAFakeTokenForTests1234567890",
            "TELEGRAM_CHAT_ID": CHAT_ID,
            "NIGHTWATCH_COMMAND_SECRET": SECRET,
            **telegram_overrides,  # type: ignore[arg-type]
        },
    )
    return Config(
        mode=TradingMode.DRY_RUN,
        etoro=EtoroConfig(environment=EtoroEnvironment.DEMO),
        strategy=StrategyConfig(),
        symbols=SymbolConfig(),
        telegram=telegram,
        webapp=WebappConfig(),
        risk=RiskConfig(),
    )


def _send(
    repo: Repo,
    host: DesktopHost,
    config: Config,
    command: str,
    *,
    counter: int,
    chat_id: str | None = CHAT_ID,
    sender_id: str = CHAT_ID,
    channel: str = "telegram",
    secret: str = SECRET,
    bad_hmac: bool = False,
):
    normalized = normalize_command(command)
    provided = compute_hmac(normalized, counter, secret)
    if bad_hmac:
        provided = provided[:-1] + ("0" if provided[-1] != "0" else "1")
    return handle(
        repo, host, config, channel=channel, sender_id=sender_id, raw_command=command,
        chat_id=chat_id, counter=counter, provided_hmac=provided,
    )


# -- normalize_command / hmac primitives ---------------------------------------


def test_normalize_command_strips_slash_and_lowercases() -> None:
    assert normalize_command("/Status") == "status"
    assert normalize_command("halt") == "halt"
    assert normalize_command("  /Resume  ") == "resume"
    assert normalize_command("") == ""


def test_hmac_roundtrip_and_tamper_detection() -> None:
    mac = compute_hmac("status", 1, SECRET)
    assert verify_hmac("status", 1, mac, SECRET) is True
    assert verify_hmac("status", 1, mac + "x", SECRET) is False
    assert verify_hmac("status", 2, mac, SECRET) is False
    assert verify_hmac("status", 1, mac, "wrong-secret") is False


# -- every supported command parses and is accepted -----------------------------


@pytest.mark.parametrize("command", sorted(SUPPORTED_COMMANDS))
def test_every_supported_command_is_accepted(
    repo: Repo, host: DesktopHost, command: str
) -> None:
    config = _config()
    result = _send(repo, host, config, f"/{command}", counter=1)
    assert result.accepted, result.command.reason
    assert result.command.verdict == "accepted"
    assert result.reply is not None
    # audited regardless of outcome
    rows = repo.get_commands(channel="telegram")
    assert len(rows) == 1
    assert rows[0].command == f"/{command}"


def test_unknown_command_is_rejected(repo: Repo, host: DesktopHost) -> None:
    config = _config()
    result = _send(repo, host, config, "/nuke_account", counter=1)
    assert not result.accepted
    assert result.command.reason == "unknown_command"
    assert result.reply is None


def test_explicitly_dangerous_commands_are_not_supported() -> None:
    """halt/resume are the only state-changing verbs; nothing else may be."""
    dangerous = {"arm", "trade", "buy", "sell", "config", "budget", "override"}
    assert dangerous.isdisjoint(SUPPORTED_COMMANDS)


# -- chat-id allowlist ------------------------------------------------------------


def test_wrong_chat_id_is_ignored_and_logged(repo: Repo, host: DesktopHost) -> None:
    config = _config()
    result = _send(repo, host, config, "/status", counter=1, chat_id="999999999")
    assert not result.accepted
    assert result.command.reason == "unauthorized"
    assert result.reply is None
    rows = repo.get_commands()
    assert len(rows) == 1
    assert rows[0].verdict == "rejected"


def test_fcm_channel_is_not_subject_to_the_telegram_chat_id_check(
    repo: Repo, host: DesktopHost
) -> None:
    config = _config()
    result = _send(
        repo, host, config, "/status", counter=1, channel="fcm",
        sender_id="fcm-principal-1", chat_id=None,
    )
    assert result.accepted


# -- HMAC authentication ------------------------------------------------------------


def test_valid_hmac_is_accepted(repo: Repo, host: DesktopHost) -> None:
    config = _config()
    result = _send(repo, host, config, "/status", counter=1, channel="fcm")
    assert result.accepted


def test_tampered_hmac_is_rejected(repo: Repo, host: DesktopHost) -> None:
    config = _config()
    result = _send(repo, host, config, "/status", counter=1, bad_hmac=True, channel="fcm")
    assert not result.accepted
    assert result.command.reason == "bad_hmac"


def test_missing_hmac_is_rejected(repo: Repo, host: DesktopHost) -> None:
    config = _config()
    result = handle(
        repo, host, config, channel="fcm", sender_id=CHAT_ID,
        raw_command="/status", chat_id=CHAT_ID, counter=1, provided_hmac=None,
    )
    assert not result.accepted
    assert result.command.reason == "missing_hmac"


def test_missing_command_secret_is_rejected(repo: Repo, host: DesktopHost) -> None:
    telegram = TelegramConfig(
        enabled=True,
        secrets={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": CHAT_ID},
    )
    config = Config(
        mode=TradingMode.DRY_RUN, etoro=EtoroConfig(environment=EtoroEnvironment.DEMO),
        strategy=StrategyConfig(), symbols=SymbolConfig(), telegram=telegram,
        webapp=WebappConfig(), risk=RiskConfig(),
    )
    result = handle(
        repo, host, config, channel="fcm", sender_id=CHAT_ID,
        raw_command="/status", chat_id=CHAT_ID, counter=1, provided_hmac="whatever",
    )
    assert not result.accepted
    assert result.command.reason == "command_secret_not_configured"


# -- replay rejection by counter ------------------------------------------------------


def test_replayed_counter_is_rejected(repo: Repo, host: DesktopHost) -> None:
    config = _config()
    first = _send(repo, host, config, "/status", counter=5, channel="fcm")
    assert first.accepted

    replay = _send(repo, host, config, "/status", counter=5, channel="fcm")
    assert not replay.accepted
    assert replay.command.reason == "replay"

    older = _send(repo, host, config, "/status", counter=3, channel="fcm")
    assert not older.accepted
    assert older.command.reason == "replay"

    newer = _send(repo, host, config, "/status", counter=6, channel="fcm")
    assert newer.accepted


def test_replay_counter_namespace_is_per_channel_and_sender(
    repo: Repo, host: DesktopHost
) -> None:
    config = _config()
    a = _send(repo, host, config, "/status", counter=5, sender_id="sender-a", channel="fcm")
    assert a.accepted
    b = _send(
        repo, host, config, "/status", counter=1, channel="fcm",
        sender_id="sender-b", chat_id=None,
    )
    assert b.accepted  # independent counter space, not a replay of sender-a's 5


# -- halt / resume dispatch, reusing cli.py's logic -------------------------------


def test_halt_command_sets_the_halt_flag(repo: Repo, host: DesktopHost) -> None:
    config = _config()
    assert not host.halt_flag_path().exists()
    result = _send(repo, host, config, "/halt", counter=1)
    assert result.accepted
    assert host.halt_flag_path().exists()


def test_resume_command_clears_the_halt_flag(repo: Repo, host: DesktopHost) -> None:
    config = _config()
    host.halt_flag_path().parent.mkdir(parents=True, exist_ok=True)
    host.halt_flag_path().write_text("halted\n", encoding="utf-8")
    result = _send(repo, host, config, "/resume operator checked", counter=1)
    assert result.accepted
    assert not host.halt_flag_path().exists()
    assert result.reply == "HALT cleared (reason: operator checked). Previous HALT: halted (source unknown)."
    assert result.command.command == "/resume operator checked"
    assert repo.get_commands()[0].reason == "operator checked"


def test_telegram_needs_no_hmac_secret_or_counter(repo, host):
    config = _config(NIGHTWATCH_COMMAND_SECRET="")
    for _ in range(2):
        result = handle(repo, host, config, channel="telegram", sender_id=CHAT_ID,
                        chat_id=CHAT_ID, raw_command="/status")
        assert result.accepted


def test_bare_resume_keeps_halt(repo, host):
    write_halt(host, reason="original", source="session")
    before = read_halt(host)
    result = _send(repo, host, _config(), "/resume  ", counter=1)
    assert result.accepted
    assert result.reply == "Usage: /resume <reason>. HALT unchanged."
    assert read_halt(host) == before


@pytest.mark.parametrize("seconds", [-1, 0, 1])
def test_resume_message_time_guard(repo, host, seconds):
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    write_halt(host, reason="original", source="session", now=now)
    before = read_halt(host)
    result = handle(repo, host, _config(), channel="telegram", sender_id=CHAT_ID,
                    chat_id=CHAT_ID, raw_command="/resume checked",
                    message_ts=now + timedelta(seconds=seconds))
    assert result.accepted
    if seconds <= 0:
        assert result.reply == (f"Ignored: this /resume was sent before the current HALT was set ({before['set_at']}). "
                                "Send /resume again.")
        assert read_halt(host) == before
    else:
        assert read_halt(host) is None
        assert "HALT cleared (reason: checked)" in result.reply


def test_resume_without_halt(repo, host):
    result = _send(repo, host, _config(), "/resume checked", counter=1)
    assert result.reply == "HALT was not set."


def test_halt_reason_is_saved_and_existing_flag_kept(repo, host):
    result = _send(repo, host, _config(), "/halt why", counter=1)
    flag = read_halt(host)
    assert flag["reason"] == "why" and flag["source"] == "telegram"
    assert result.command.reason == "why"
    _send(repo, host, _config(), "/halt another reason", counter=1)
    assert read_halt(host) == flag


# -- commands table gets a row for both accepted and rejected --------------------


def test_commands_table_records_both_accepted_and_rejected(
    repo: Repo, host: DesktopHost
) -> None:
    config = _config()
    _send(repo, host, config, "/status", counter=1, channel="fcm")
    _send(repo, host, config, "/status", counter=1, channel="fcm")  # replay -> rejected
    rows = repo.get_commands()
    assert len(rows) == 2
    verdicts = {r.verdict for r in rows}
    assert verdicts == {"accepted", "rejected"}


# -- read-only replies carry real data --------------------------------------------


def test_positions_reply_reflects_open_annotations(repo: Repo, host: DesktopHost) -> None:
    from nightwatch.db.repo import Bucket, LotAnnotation

    for bucket in ("bucket-a", "bucket-b"):
        repo.upsert_bucket(Bucket(bucket_id=bucket, sale_date="2026-01-02", sale_spy_close=600.0,
                                  proceeds=120.0, tranche_amount=40.0, created_bar=1))
    for position_id, symbol, book, bucket, is_open in [
        ("private-position-a", "UPRO", "catapult", None, True),
        ("private-position-b", "UPRO", "catapult", None, True),
        ("private-position-c", "SCHG", "catapult", None, True),
        ("private-position-d", "UPRO", "reentry", "bucket-a", True),
        ("private-position-e", "UPRO", "reentry", "bucket-b", True),
        ("private-position-closed", "UPRO", "catapult", None, False),
    ]:
        repo.insert_lot_annotation(LotAnnotation(
            position_id=position_id, symbol=symbol, book=book, bucket_id=bucket,
            entry_date="2026-01-02", entry_amount=120.0, origin="app",
            is_open=is_open,
        ))
    assert repo.open_lots() == []
    result = _send(repo, host, _config(), "/positions", counter=1)
    assert result.accepted
    assert result.reply == (
        "SCHG [catapult] 1 lot(s)\nUPRO [catapult] 2 lot(s)\n"
        "UPRO [reentry bucket=bucket-a] 1 lot(s)\n"
        "UPRO [reentry bucket=bucket-b] 1 lot(s)"
    )
    assert "private-position" not in result.reply


def test_positions_ignores_legacy_lots_and_closed_annotations(repo, host):
    from nightwatch.db.repo import Lot, LotAnnotation

    repo.insert_lot(Lot(book="catapult", symbol="UPRO", units=2.0, entry_price=60.0,
                        cost_basis=120.0, entry_date="2026-01-02"))
    repo.insert_lot_annotation(LotAnnotation(
        position_id="closed-position", symbol="UPRO", book="catapult",
        entry_date="2026-01-02", entry_amount=120.0, origin="app", is_open=False,
    ))
    result = _send(repo, host, _config(), "/positions", counter=1)
    assert result.accepted and result.reply == "no open positions"


def test_help_reply_lists_every_supported_command(repo: Repo, host: DesktopHost) -> None:
    config = _config()
    result = _send(repo, host, config, "/help", counter=1)
    assert result.accepted
    for command in SUPPORTED_COMMANDS:
        assert f"/{command}" in (result.reply or "")
