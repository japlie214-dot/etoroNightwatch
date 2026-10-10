"""Round-trip tests for every db.repo.Repo helper (DESIGN §3)."""

from __future__ import annotations

import dataclasses
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
    LotAnnotation,
    Notification,
    Order,
    PortfolioSnapshot,
    PreflightCheck,
    PriceWindowFingerprint,
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


def test_reserved_buy_usd_counts_date_and_non_rejected_buys(repo: Repo) -> None:
    assert repo.reserved_buy_usd("2026-01-02") == 0.0
    for date, multiplier in [("2026-01-02", 1), ("2026-01-03", 10)]:
        session = repo.create_session(date, mode="live", environment="demo", armed=True)
        for sequence, status in enumerate(
            ("pending", "sent", "filled", "recovered", "abandoned", "rejected"), 1,
        ):
            repo.insert_intent(Intent(
                session_id=session.id, request_id=f"{date}-{status}", sequence=sequence,
                symbol="SCHG", side="buy", amount_usd=sequence * 100.0 * multiplier,
                status=status,
            ))
        repo.insert_intent(Intent(
            session_id=session.id, request_id=f"{date}-close", sequence=7,
            symbol="UPRO", side="close", amount_usd=9999.0, status="filled",
        ))
    changes = repo.conn.total_changes
    assert repo.reserved_buy_usd("2026-01-02") == 1500.0
    assert repo.reserved_buy_usd("2026-01-03") == 15000.0
    assert Repo(repo.conn).reserved_buy_usd("2026-01-02") == 1500.0
    assert repo.reserved_buy_usd("2026-01-04") == 0.0
    assert repo.conn.total_changes == changes


def test_abandoned_intents_listing(repo: Repo) -> None:
    """The state-based resolution path's candidate set
    (engine.reconcile.resolve_abandoned_intent_adoptions, DESIGN_session.md
    §3.4): only intents actually marked 'abandoned' come back, regardless
    of status or how many sessions they span."""
    session = repo.create_session("2026-01-02", mode="live", environment="demo", armed=True)
    abandoned_id = repo.insert_intent(
        Intent(session_id=session.id, request_id="rid-abandoned", sequence=1, symbol="SCHG", side="buy", amount_usd=50.0)
    )
    repo.insert_intent(
        Intent(session_id=session.id, request_id="rid-pending", sequence=2, symbol="SCHG", side="buy", amount_usd=50.0)
    )
    filled_id = repo.insert_intent(
        Intent(session_id=session.id, request_id="rid-filled", sequence=3, symbol="SCHG", side="buy", amount_usd=50.0)
    )
    repo.set_intent_status(abandoned_id, "abandoned", resolved=True)
    repo.set_intent_status(filled_id, "filled", resolved=True)

    result = repo.abandoned_intents()
    assert [i.request_id for i in result] == ["rid-abandoned"]
    assert result[0].status == "abandoned"


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


# -- lot annotations (DESIGN_market_data.md §4.4) -------------------------


def _annotation(position_id: str, **overrides: object) -> LotAnnotation:
    fields: dict[str, object] = dict(
        position_id=position_id,
        symbol="SCHG",
        book="catapult",
        entry_date="2024-09-03T10:15:00+00:00",
        entry_amount=10.0,
        origin="adopted",
        entry_spy_ath=110.0,
        entry_spy_close=104.5,
        entry_spy_drawdown=-0.05,
        entry_tier=0,
    )
    fields.update(overrides)
    return LotAnnotation(**fields)  # type: ignore[arg-type]


def test_lot_annotation_round_trip(repo: Repo) -> None:
    repo.insert_lot_annotation(_annotation("pos-1"))
    row = repo.get_lot_annotation("pos-1")
    assert row is not None
    assert row.symbol == "SCHG"
    assert row.book == "catapult"
    assert row.origin == "adopted"
    assert row.entry_amount == 10.0
    assert row.entry_tier == 0
    assert row.is_open is True
    assert row.bucket_id is None
    assert row.created_at is not None
    # migration 005, board nightwatch/handoff-2026-09-05-evening.md §3a: defaults to
    # 'derived' -- every row this codebase writes today has measured, never
    # guessed, SPY context.
    assert row.entry_context_source == "derived"


def test_lot_annotation_entry_context_source_round_trips_explicitly(repo: Repo) -> None:
    repo.insert_lot_annotation(
        _annotation("pos-assumed", entry_context_source="assumed_out_of_window")
    )
    row = repo.get_lot_annotation("pos-assumed")
    assert row is not None
    assert row.entry_context_source == "assumed_out_of_window"


def test_lot_annotation_adopted_from_abandoned_intent_round_trips(repo: Repo) -> None:
    """Migration 006 (DESIGN_session.md §3.4, the operator's 2026-09-06 "no
    forced human-in-the-loop" decision): a lot built by
    ``engine.reconcile.resolve_abandoned_intent_adoptions`` is stamped with
    this distinct entry_context_source and stays greppable/auditable
    forever after, never silently indistinguishable from a normally-recorded
    row."""
    repo.insert_lot_annotation(
        _annotation("pos-adopted", entry_context_source="adopted_from_abandoned_intent")
    )
    row = repo.get_lot_annotation("pos-adopted")
    assert row is not None
    assert row.entry_context_source == "adopted_from_abandoned_intent"


def test_insert_lot_annotation_rejects_duplicate_position_id(repo: Repo) -> None:
    repo.insert_lot_annotation(_annotation("pos-1"))
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_lot_annotation(_annotation("pos-1"))


def test_insert_lot_annotation_rejects_bad_book_or_origin(repo: Repo) -> None:
    with pytest.raises(ValueError, match="book"):
        repo.insert_lot_annotation(_annotation("pos-bad-book", book="not-a-book"))
    with pytest.raises(ValueError, match="origin"):
        repo.insert_lot_annotation(_annotation("pos-bad-origin", origin="legacy"))
    assert repo.list_lot_annotations() == []


def test_insert_lot_annotation_rejects_bad_entry_context_source(repo: Repo) -> None:
    with pytest.raises(ValueError, match="entry_context_source"):
        repo.insert_lot_annotation(
            _annotation("pos-bad-context", entry_context_source="guessed")
        )
    assert repo.list_lot_annotations() == []


def test_list_lot_annotations_filters(repo: Repo) -> None:
    repo.insert_lot_annotation(_annotation("pos-schg-catapult", symbol="SCHG", book="catapult"))
    repo.insert_lot_annotation(_annotation("pos-upro-reentry", symbol="UPRO", book="reentry"))
    repo.insert_lot_annotation(_annotation("pos-upro-catapult", symbol="UPRO", book="catapult", origin="app"))

    assert len(repo.list_lot_annotations()) == 3
    assert {a.position_id for a in repo.list_lot_annotations(symbol="UPRO")} == {
        "pos-upro-reentry", "pos-upro-catapult",
    }
    assert [a.position_id for a in repo.list_lot_annotations(book="reentry")] == ["pos-upro-reentry"]
    assert [a.position_id for a in repo.list_lot_annotations(origin="app")] == ["pos-upro-catapult"]


def test_open_lot_annotation_position_ids(repo: Repo) -> None:
    repo.insert_lot_annotation(_annotation("pos-open", is_open=True))
    repo.insert_lot_annotation(_annotation("pos-closed", is_open=False))
    assert repo.open_lot_annotation_position_ids() == {"pos-open"}


def test_insert_lot_annotations_bulk_is_idempotent(repo: Repo) -> None:
    rows = [_annotation("pos-1"), _annotation("pos-2", symbol="UPRO")]
    inserted_first = repo.insert_lot_annotations(rows)
    assert inserted_first == 2
    assert len(repo.list_lot_annotations()) == 2

    # Re-running with the same rows (plus a genuinely new one) must insert
    # only the new one -- INSERT OR IGNORE keyed on position_id, never an
    # overwrite of the two already present (§4.3: stamp once, never update).
    inserted_second = repo.insert_lot_annotations([*rows, _annotation("pos-3", symbol="UPRO")])
    assert inserted_second == 1
    assert len(repo.list_lot_annotations()) == 3


def test_insert_lot_annotations_bulk_rejects_bad_book_before_writing_anything(repo: Repo) -> None:
    """A bad ``book``/``origin`` must raise -- and must raise BEFORE any
    row is written, not merely leave the row silently unwritten.

    This is a real correctness gap that would otherwise exist:
    ``INSERT OR IGNORE`` (the mechanism idempotency needs) resolves a
    ``CHECK`` violation by silently skipping the row, with no exception --
    confirmed directly against this SQLite build. Left uncaught, a bad
    ``book`` would vanish into ``rowcount == 0`` rather than surface as an
    error, which is exactly the "didn't error, not a pass" failure mode this
    project's history keeps flagging. ``Repo._validate_lot_annotation``
    exists specifically to catch this in Python first.
    """
    rows = [
        _annotation("pos-good-1"),
        _annotation("pos-good-2", symbol="UPRO"),
        _annotation("pos-bad", book="not-a-real-book"),
    ]
    with pytest.raises(ValueError, match="not-a-real-book"):
        repo.insert_lot_annotations(rows)

    # Nothing from the batch landed -- validated before the first INSERT.
    assert repo.list_lot_annotations() == []


def test_insert_lot_annotations_bulk_is_atomic_on_a_foreign_key_violation(repo: Repo) -> None:
    """A genuine SQL-level failure partway through a batch (here: a
    ``bucket_id`` that does not exist -- unlike a bad ``book``/``origin``,
    a FOREIGN KEY violation is NOT swallowed by ``OR IGNORE``, confirmed
    directly against this SQLite build) must roll back the ENTIRE batch,
    including the rows before it that were individually valid -- 'prefer
    one transaction that either completes or rolls back' (§4.7 migration
    task brief)."""
    rows = [
        _annotation("pos-good-1"),
        _annotation("pos-good-2", symbol="UPRO"),
        _annotation("pos-bad", bucket_id="no-such-bucket"),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_lot_annotations(rows)

    # Nothing from the batch landed -- not even the two good rows that were
    # inserted before the bad one blew up mid-loop.
    assert repo.list_lot_annotations() == []
    assert repo.get_lot_annotation("pos-good-1") is None
    assert repo.get_lot_annotation("pos-good-2") is None

    # The connection must still be usable afterwards (rollback did not
    # poison it) -- a fresh, valid write succeeds.
    repo.insert_lot_annotation(_annotation("pos-after-rollback"))
    assert repo.get_lot_annotation("pos-after-rollback") is not None


def test_update_lot_annotation_updates_mutable_fields_in_place(repo: Repo) -> None:
    """PARTIAL CLOSE write path (board nightwatch/handoff-2026-09-06.md §7): a position that
    stays OPEN with reduced units needs its sidecar row updatable, not just
    insert-or-skip."""
    repo.insert_lot_annotation(_annotation("pos-1"))
    row = repo.get_lot_annotation("pos-1")
    assert row is not None

    updated = dataclasses.replace(row, entry_tier=2)
    repo.update_lot_annotation(updated)

    got = repo.get_lot_annotation("pos-1")
    assert got is not None
    assert got.entry_tier == 2
    # A partial close leaves the position OPEN -- is_open is untouched here
    # because `updated` was built from the still-open row.
    assert got.is_open is True


def test_update_lot_annotation_leaves_the_relink_key_and_identity_untouched(
    repo: Repo,
) -> None:
    """`entry_date`/`entry_amount` are the re-link key (§4.4) and must never
    change after the first stamp; `position_id`/`symbol`/`book`/`origin` are
    identity/provenance, also immutable here. The UPDATE statement simply
    does not include those columns, so even a caller that (incorrectly)
    supplies different values for them has no effect."""
    repo.insert_lot_annotation(_annotation("pos-1", entry_amount=10.0, symbol="SCHG"))
    original = repo.get_lot_annotation("pos-1")
    assert original is not None

    tampered = dataclasses.replace(
        original, entry_amount=999.0, symbol="UPRO", entry_tier=1
    )
    repo.update_lot_annotation(tampered)

    got = repo.get_lot_annotation("pos-1")
    assert got is not None
    assert got.entry_amount == 10.0
    assert got.symbol == "SCHG"
    assert got.entry_tier == 1  # the one field that WAS meant to change


def test_update_lot_annotation_raises_when_no_row_exists(repo: Repo) -> None:
    with pytest.raises(ValueError, match="pos-missing"):
        repo.update_lot_annotation(_annotation("pos-missing"))


def test_update_lot_annotation_rejects_bad_book_or_origin(repo: Repo) -> None:
    repo.insert_lot_annotation(_annotation("pos-1"))
    with pytest.raises(ValueError, match="book"):
        repo.update_lot_annotation(_annotation("pos-1", book="not-a-book"))
    # Rejected before any write -- the row is unchanged.
    assert repo.get_lot_annotation("pos-1").book == "catapult"  # type: ignore[union-attr]


def test_lot_annotation_exit_context_source_defaults_to_none(repo: Repo) -> None:
    """Migration 007: every row inserted today (no auto-resolve path has
    run yet) has no special close-time provenance to record."""
    repo.insert_lot_annotation(_annotation("pos-1"))
    row = repo.get_lot_annotation("pos-1")
    assert row is not None
    assert row.exit_context_source is None


def test_update_lot_annotation_can_stamp_exit_context_source(repo: Repo) -> None:
    """Migration 007 (DESIGN_session.md §3.4, closing the mirror-image gap):
    engine.reconcile.resolve_abandoned_close_intents marks an
    auto-resolved close's annotation via exactly this write path -- is_open
    flips to False and exit_context_source is stamped, greppable/auditable
    forever after, unlike entry_context_source which update_lot_annotation
    never touches."""
    repo.insert_lot_annotation(_annotation("pos-1"))
    row = repo.get_lot_annotation("pos-1")
    assert row is not None

    resolved = dataclasses.replace(
        row, is_open=False, exit_context_source="resolved_from_abandoned_intent"
    )
    repo.update_lot_annotation(resolved)

    got = repo.get_lot_annotation("pos-1")
    assert got is not None
    assert got.is_open is False
    assert got.exit_context_source == "resolved_from_abandoned_intent"
    # entry_context_source is untouched, exactly as before this migration.
    assert got.entry_context_source == "derived"


def test_update_lot_annotation_rejects_bad_exit_context_source(repo: Repo) -> None:
    repo.insert_lot_annotation(_annotation("pos-1"))
    row = repo.get_lot_annotation("pos-1")
    assert row is not None
    with pytest.raises(ValueError, match="exit_context_source"):
        repo.update_lot_annotation(
            dataclasses.replace(row, exit_context_source="guessed")
        )
    # Rejected before any write -- the row is unchanged.
    assert repo.get_lot_annotation("pos-1").exit_context_source is None  # type: ignore[union-attr]


def test_insert_lot_annotation_rejects_bad_exit_context_source(repo: Repo) -> None:
    with pytest.raises(ValueError, match="exit_context_source"):
        repo.insert_lot_annotation(
            _annotation("pos-bad-exit-context", exit_context_source="guessed")
        )
    assert repo.list_lot_annotations() == []


def test_insert_lot_annotations_bulk_round_trips_exit_context_source(repo: Repo) -> None:
    """Bulk insert (the §4.7 adoption backfill path) also carries this
    column through -- it defaults to None like everywhere else, and is not
    silently dropped by the batch INSERT."""
    rows = [_annotation("pos-1"), _annotation("pos-2", symbol="UPRO")]
    repo.insert_lot_annotations(rows)
    assert repo.get_lot_annotation("pos-1").exit_context_source is None  # type: ignore[union-attr]


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


def test_insert_snapshot_mid_failure_leaves_no_rows(repo: Repo) -> None:
    session = repo.create_session("2026-01-02", mode="dry_run", environment="demo", armed=False)
    snapshot = PortfolioSnapshot(
        session_id=session.id, taken_at="2026-01-02T15:50:00+00:00",
        positions=[SnapshotPosition(position_id=pid) for pid in ("1", "2", "1")],
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        repo.insert_snapshot(snapshot)

    repo.finish_session(session.id, state="finished", outcome="snapshot failed")
    finished = repo.get_session(session.id)
    assert finished is not None
    assert finished.state == "finished"
    assert finished.finished_at is not None
    assert repo.conn.execute("SELECT COUNT(*) FROM portfolio_snapshots").fetchone()[0] == 0
    assert repo.conn.execute("SELECT COUNT(*) FROM portfolio_snapshot_positions").fetchone()[0] == 0


def test_portfolio_snapshot_round_trip_with_positions(repo: Repo) -> None:
    # symbol/current_price/unrealized_pnl are caller-supplied here (resolved
    # via Repo.get_instrument / computed via etoro.models.mark_position),
    # never parsed off the broker (DESIGN_market_data.md §4.2, §10.7) -- this
    # test exercises the snapshot round-trip, not the resolution/computation
    # itself, so the values are just hand-supplied.
    session = repo.create_session("2026-01-02", mode="dry_run", environment="demo", armed=False)
    snapshot_id = repo.insert_snapshot(
        PortfolioSnapshot(
            session_id=session.id, taken_at="2026-01-02T15:50:00+00:00",
            equity=10000.0, cash=500.0, available_cash=450.0,
            positions=[
                SnapshotPosition(
                    position_id="pos-1", symbol="UPRO", units=2.0,
                    entry_price=75.0, current_price=80.0, amount=150.0,
                    leverage=1, unrealized_pnl=10.0, open_date="2026-01-02T00:00:00Z",
                )
            ],
        )
    )
    got = repo.get_snapshot(snapshot_id)
    assert got is not None
    assert got.equity == 10000.0
    assert len(got.positions) == 1
    assert got.positions[0].symbol == "UPRO"
    assert got.positions[0].amount == 150.0
    assert got.positions[0].current_price == 80.0
    assert got.positions[0].unrealized_pnl == 10.0

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


# -- price window fingerprints (DESIGN_market_data.md §3.4, §7, migration 004) --


def _fingerprint(trading_date: str = "2026-09-05", symbol: str = "SPY", **overrides) -> PriceWindowFingerprint:
    fields = dict(
        trading_date=trading_date,
        symbol=symbol,
        window_start="2025-01-02",
        window_end="2026-09-04",
        bar_count=420,
        series_sha256="a" * 64,
        source="tiingo",
        source_fetched_at="2026-09-05T12:00:00+00:00",
    )
    fields.update(overrides)
    return PriceWindowFingerprint(**fields)  # type: ignore[arg-type]


def test_price_window_fingerprint_round_trip(repo: Repo) -> None:
    assert repo.get_price_window_fingerprint("2026-09-05", "SPY") is None
    repo.record_price_window_fingerprint(_fingerprint())
    row = repo.get_price_window_fingerprint("2026-09-05", "SPY")
    assert row is not None
    assert row.symbol == "SPY"
    assert row.bar_count == 420
    assert row.series_sha256 == "a" * 64
    assert row.source == "tiingo"


def test_price_window_fingerprint_upsert_overwrites_same_day_same_symbol(repo: Repo) -> None:
    repo.record_price_window_fingerprint(_fingerprint(series_sha256="a" * 64, bar_count=420))
    repo.record_price_window_fingerprint(_fingerprint(series_sha256="b" * 64, bar_count=421))
    row = repo.get_price_window_fingerprint("2026-09-05", "SPY")
    assert row.series_sha256 == "b" * 64
    assert row.bar_count == 421
    # Still exactly one row -- PRIMARY KEY (trading_date, symbol), not a
    # growing history of every session's fingerprint under one key.
    assert len(repo.list_price_window_fingerprints("SPY")) == 1


def test_price_window_fingerprint_distinct_symbols_and_dates_do_not_collide(repo: Repo) -> None:
    repo.record_price_window_fingerprint(_fingerprint(trading_date="2026-09-04", symbol="SPY"))
    repo.record_price_window_fingerprint(_fingerprint(trading_date="2026-09-05", symbol="SPY"))
    repo.record_price_window_fingerprint(_fingerprint(trading_date="2026-09-05", symbol="UPRO"))
    assert len(repo.list_price_window_fingerprints("SPY")) == 2
    assert len(repo.list_price_window_fingerprints("UPRO")) == 1
    # Most recent session first.
    dates = [r.trading_date for r in repo.list_price_window_fingerprints("SPY")]
    assert dates == ["2026-09-05", "2026-09-04"]
