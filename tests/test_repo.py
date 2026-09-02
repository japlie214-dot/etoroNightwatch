"""Round-trip tests for every db.repo.Repo helper (DESIGN §3)."""

from __future__ import annotations

import sqlite3

import pytest

from nightwatch.db.repo import (
    Bar,
    Bucket,
    BudgetRecord,
    Command,
    Decision,
    Instrument,
    Intent,
    Lot,
    Notification,
    Order,
    PortfolioSnapshot,
    PreflightCheck,
    Repo,
    SnapshotPosition,
    StrategyState,
)


# -- bars --------------------------------------------------------------------


def test_bars_upsert_and_get(repo: Repo) -> None:
    repo.upsert_bar(Bar(symbol="SPY", date="2026-01-02", close=470.0, source="seed"))
    got = repo.get_bar("SPY", "2026-01-02")
    assert got is not None
    assert got.close == 470.0
    assert got.is_provisional is True


def test_bars_upsert_many_and_ordering(repo: Repo) -> None:
    bars = [
        Bar(symbol="SPY", date=f"2026-01-{d:02d}", close=100.0 + d, source="seed")
        for d in range(2, 10)
    ]
    count = repo.upsert_bars(bars)
    assert count == len(bars)
    got = repo.get_bars("SPY")
    assert [b.date for b in got] == sorted(b.date for b in bars)


def test_bars_settle_flips_provisional_flag(repo: Repo) -> None:
    repo.upsert_bar(Bar(symbol="SPY", date="2026-01-02", close=470.0, source="live_rate"))
    repo.settle_bar("SPY", "2026-01-02", close=471.5, source="candles")
    got = repo.get_bar("SPY", "2026-01-02")
    assert got is not None
    assert got.is_provisional is False
    assert got.close == 471.5
    assert got.source == "candles"


def test_count_bars_settled_only(repo: Repo) -> None:
    repo.upsert_bar(Bar(symbol="SPY", date="2026-01-02", close=1.0, is_provisional=False, source="candles"))
    repo.upsert_bar(Bar(symbol="SPY", date="2026-01-03", close=2.0, is_provisional=True, source="live_rate"))
    assert repo.count_bars("SPY", settled_only=True) == 1
    assert repo.count_bars("SPY", settled_only=False) == 2


def test_provisional_bars_lists_only_unsettled(repo: Repo) -> None:
    repo.upsert_bar(Bar(symbol="SPY", date="2026-01-02", close=1.0, is_provisional=False, source="candles"))
    repo.upsert_bar(Bar(symbol="SPY", date="2026-01-03", close=2.0, is_provisional=True, source="live_rate"))
    provisional = repo.provisional_bars()
    assert [b.date for b in provisional] == ["2026-01-03"]


# -- instruments ---------------------------------------------------------------


def test_instrument_round_trip(repo: Repo) -> None:
    repo.upsert_instrument(
        Instrument(
            symbol="UPRO",
            instrument_id=12345,
            allow_open_position=True,
            min_position_exposure=50.0,
            metadata={"note": "leveraged etf"},
        )
    )
    got = repo.get_instrument("UPRO")
    assert got is not None
    assert got.instrument_id == 12345
    assert got.allow_open_position is True
    assert got.metadata == {"note": "leveraged etf"}


# -- sessions ------------------------------------------------------------------


def test_session_create_get_and_finish(repo: Repo) -> None:
    session = repo.create_session("2026-01-02", mode="dry_run", environment="demo", armed=False)
    assert session.trading_date == "2026-01-02"
    assert session.state == "started"

    fetched = repo.get_session_by_date("2026-01-02")
    assert fetched is not None
    assert fetched.id == session.id

    repo.finish_session(session.id, state="finished", outcome="no-op")
    finished = repo.get_session(session.id)
    assert finished is not None
    assert finished.state == "finished"
    assert finished.finished_at is not None


def test_session_unique_per_trading_date_rejects_duplicate(repo: Repo) -> None:
    """The UNIQUE(trading_date) constraint the safety-net alarm relies on
    (DESIGN §5, §6.2) must actually reject a second session for the same date."""
    repo.create_session("2026-01-02", mode="dry_run", environment="demo", armed=False)
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_session("2026-01-02", mode="dry_run", environment="demo", armed=False)


def test_latest_session_and_unfinished_sessions(repo: Repo) -> None:
    repo.create_session("2026-01-02", mode="dry_run", environment="demo", armed=False)
    s2 = repo.create_session("2026-01-03", mode="dry_run", environment="demo", armed=False)
    latest = repo.latest_session()
    assert latest is not None
    assert latest.trading_date == "2026-01-03"

    repo.finish_session(s2.id, state="finished")
    unfinished = repo.unfinished_sessions()
    assert [s.trading_date for s in unfinished] == ["2026-01-02"]


def test_update_session_rejects_unknown_column(repo: Repo) -> None:
    session = repo.create_session("2026-01-02", mode="dry_run", environment="demo", armed=False)
    with pytest.raises(ValueError):
        repo.update_session(session.id, trading_date="2099-01-01")


# -- preflight -------------------------------------------------------------


def test_preflight_checks_round_trip_in_gate_order(repo: Repo) -> None:
    session = repo.create_session("2026-01-02", mode="dry_run", environment="demo", armed=False)
    repo.record_preflight_check(PreflightCheck(session_id=session.id, gate=2, name="power", passed=True))
    repo.record_preflight_check(PreflightCheck(session_id=session.id, gate=1, name="halt", passed=True))
    checks = repo.get_preflight_checks(session.id)
    assert [c.gate for c in checks] == [1, 2]
    assert all(c.passed for c in checks)


# -- decisions ---------------------------------------------------------------


def test_decision_round_trip(repo: Repo) -> None:
    session = repo.create_session("2026-01-02", mode="dry_run", environment="demo", armed=False)
    intents = [{"symbol": "UPRO", "side": "buy", "amount_usd": 100.0}]
    repo.insert_decision(
        Decision(session_id=session.id, trading_date="2026-01-02", cone_z=-2.5, tier=1, intents=intents)
    )
    decision = repo.get_decision(session.id)
    assert decision is not None
    assert decision.cone_z == -2.5
    assert decision.intents == intents


# -- intents / orders (crash-recovery path) -----------------------------------


def test_intent_lookup_by_request_id(repo: Repo) -> None:
    """The crash-recovery path: a lost HTTP response is recovered by looking
    the intent up by its x-request-id, never by re-sending (DESIGN §5)."""
    session = repo.create_session("2026-01-02", mode="live", environment="demo", armed=True)
    request_id = "11111111-1111-1111-1111-111111111111"
    repo.insert_intent(
        Intent(
            session_id=session.id, request_id=request_id, sequence=1,
            symbol="UPRO", side="buy", amount_usd=100.0,
        )
    )
    found = repo.get_intent_by_request_id(request_id)
    assert found is not None
    assert found.session_id == session.id
    assert found.status == "pending"

    assert repo.get_intent_by_request_id("does-not-exist") is None


def test_intent_status_lifecycle_and_pending_listing(repo: Repo) -> None:
    session = repo.create_session("2026-01-02", mode="live", environment="demo", armed=True)
    intent_id = repo.insert_intent(
        Intent(session_id=session.id, request_id="rid-1", sequence=1, symbol="UPRO", side="buy", amount_usd=50.0)
    )
    assert len(repo.pending_intents()) == 1
    repo.set_intent_status(intent_id, "sent")
    repo.set_intent_status(intent_id, "filled", resolved=True)
    intents = repo.get_intents(session.id)
    assert intents[0].status == "filled"
    assert intents[0].resolved_at is not None
    assert repo.pending_intents() == []


def test_order_round_trip_and_reference_lookup(repo: Repo) -> None:
    session = repo.create_session("2026-01-02", mode="live", environment="demo", armed=True)
    intent_id = repo.insert_intent(
        Intent(session_id=session.id, request_id="rid-2", sequence=1, symbol="UPRO", side="buy", amount_usd=50.0)
    )
    order_id = repo.insert_order(
        Order(intent_id=intent_id, session_id=session.id, request_id="rid-2", symbol="UPRO", side="buy")
    )
    repo.update_order_status(order_id, last_status=11)
    repo.update_order_status(order_id, last_status=3, terminal_status=3, fill_price=45.2, fill_units=1.1)

    by_ref = repo.get_order_by_reference_id("rid-2")
    assert by_ref is not None
    assert by_ref.terminal_status == 3
    assert by_ref.fill_price == 45.2
    # a later poll with only a status must not erase the fill price already recorded
    repo.update_order_status(order_id, last_status=3)
    assert repo.get_order(order_id).fill_price == 45.2


# -- lots ----------------------------------------------------------------------


def test_lot_round_trip_and_close(repo: Repo) -> None:
    lot_id = repo.insert_lot(
        Lot(book="catapult", symbol="UPRO", units=1.0, entry_price=45.0, cost_basis=45.0, entry_date="2026-01-02")
    )
    repo.set_lot_position_id(lot_id, "pos-123")
    lot = repo.get_lot(lot_id)
    assert lot is not None
    assert lot.position_id == "pos-123"
    assert lot.is_open is True

    by_position = repo.get_lot_by_position_id("pos-123")
    assert by_position is not None
    assert by_position.id == lot_id

    repo.close_lot(lot_id, exit_date="2026-02-01", exit_price=50.0, exit_proceeds=50.0, realized_pnl=5.0)
    closed = repo.get_lot(lot_id)
    assert closed is not None
    assert closed.is_open is False
    assert closed.realized_pnl == 5.0


def test_open_lots_filters_by_book_and_symbol(repo: Repo) -> None:
    repo.insert_lot(Lot(book="catapult", symbol="UPRO", units=1.0, entry_price=1.0, cost_basis=1.0, entry_date="2026-01-02"))
    repo.insert_lot(Lot(book="reentry", symbol="UPRO", units=1.0, entry_price=1.0, cost_basis=1.0, entry_date="2026-01-02"))
    assert len(repo.open_lots()) == 2
    assert len(repo.open_lots(book="catapult")) == 1
    assert len(repo.open_lots(symbol="UPRO", book="reentry")) == 1


# -- buckets ---------------------------------------------------------------


def test_bucket_round_trip_and_expiry(repo: Repo) -> None:
    bucket = Bucket(
        bucket_id="bucket-1", sale_date="2026-01-02", sale_spy_close=470.0,
        proceeds=300.0, tranche_amount=100.0, created_bar=42,
    )
    repo.upsert_bucket(bucket)
    got = repo.get_bucket("bucket-1")
    assert got is not None
    assert got.is_active is True

    assert repo.active_buckets() == [got]
    repo.expire_bucket("bucket-1")
    expired = repo.get_bucket("bucket-1")
    assert expired is not None
    assert expired.is_active is False
    assert expired.expired_at is not None
    assert repo.active_buckets() == []


# -- strategy state (singleton) ---------------------------------------------


def test_strategy_state_defaults_and_round_trip(repo: Repo) -> None:
    default_state = repo.get_strategy_state()
    assert default_state.bars == 0

    repo.save_strategy_state(StrategyState(bars=300, ath=480.0, recyclable_cash=123.45, budget_year=2026))
    got = repo.get_strategy_state()
    assert got.bars == 300
    assert got.ath == 480.0
    assert got.recyclable_cash == 123.45
    assert got.budget_year == 2026


# -- schg_daily_values / budget_history --------------------------------------


def test_schg_values_and_mean(repo: Repo) -> None:
    repo.upsert_schg_value("2026-01-02", 100.0)
    repo.upsert_schg_value("2026-06-15", 120.0)
    mean = repo.mean_schg_value(2026)
    assert mean == pytest.approx(110.0)
    assert repo.mean_schg_value(2099) is None


def test_budget_history_round_trip(repo: Repo) -> None:
    repo.insert_budget_record(BudgetRecord(year=2026, budget=2000.0, order_size=33.3, cash_target=266.4, source="auto"))
    latest = repo.latest_budget()
    assert latest is not None
    assert latest.year == 2026
    assert repo.latest_budget(year=2026) is not None
    assert repo.latest_budget(year=1999) is None


# -- portfolio snapshots ------------------------------------------------------


def test_portfolio_snapshot_round_trip_with_positions(repo: Repo) -> None:
    session = repo.create_session("2026-01-02", mode="dry_run", environment="demo", armed=False)
    snapshot_id = repo.insert_snapshot(
        PortfolioSnapshot(
            session_id=session.id, taken_at="2026-01-02T15:50:00+00:00",
            equity=10000.0, cash=500.0, available_cash=450.0,
            positions=[SnapshotPosition(position_id="pos-1", symbol="UPRO", units=2.0)],
        )
    )
    got = repo.get_snapshot(snapshot_id)
    assert got is not None
    assert got.equity == 10000.0
    assert len(got.positions) == 1
    assert got.positions[0].symbol == "UPRO"

    assert repo.latest_snapshot().id == snapshot_id


# -- commands ------------------------------------------------------------------


def test_command_insert_and_replay_rejection(repo: Repo) -> None:
    repo.insert_command(
        Command(channel="telegram", sender_id="chat-1", command="/halt", counter=5, verdict="accepted")
    )
    assert repo.last_command_counter("telegram", "chat-1") == 5
    assert repo.is_replay("telegram", "chat-1", 5) is True
    assert repo.is_replay("telegram", "chat-1", 4) is True
    assert repo.is_replay("telegram", "chat-1", 6) is False
    # a different sender has an independent counter space
    assert repo.is_replay("telegram", "chat-2", 1) is False

    repo.insert_command(
        Command(channel="telegram", sender_id="chat-2", command="/status", verdict="rejected", reason="unauthorized")
    )
    rows = repo.get_commands(channel="telegram")
    assert len(rows) == 2
    assert {r.reason for r in rows if r.verdict == "rejected"} == {"unauthorized"}


# -- notifications -------------------------------------------------------------


def test_notification_round_trip_and_delivery_status(repo: Repo) -> None:
    notification_id = repo.insert_notification(Notification(event="heartbeat", body="ran, nothing to do"))
    assert repo.get_notification(notification_id).status == "pending"
    assert len(repo.pending_notifications()) == 1

    repo.set_notification_status(notification_id, "failed", error="timeout")
    failed = repo.get_notification(notification_id)
    assert failed.status == "failed"
    assert failed.error == "timeout"
    assert repo.pending_notifications() == []
