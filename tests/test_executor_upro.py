"""T035: symbol/book/bucket identity survives submission, fills and recovery."""

from dataclasses import replace

import pytest

from nightwatch.db.repo import Bucket, Decision, Intent
from nightwatch.engine.executor import execute_buys, recover_unfinished
from nightwatch.strategy.ladder import (
    LadderConfig, LadderInputs, LadderIntent, LadderState, ReentryBucket, decide,
)
from .test_executor import (
    FUTURE, NOW, SPY, TRADING_DATE, FakeExecClient, _filled, _poll, _position,
)

INSTRUMENT_IDS = {"SCHG": 1234, "UPRO": 5678}
BUCKET_ID = "B-2026-10-01-1"


@pytest.fixture
def session_id(repo):
    repo.upsert_bucket(Bucket(BUCKET_ID, "2026-10-01", 680.0, 300.0, 100.0, 0))
    return repo.create_session(TRADING_DATE.isoformat(), "live", "demo", True).id


def _run(repo, session_id, fake, intents, *, instrument_ids=INSTRUMENT_IDS):
    report = execute_buys(
        repo=repo, client=fake, session_id=session_id, trading_date=TRADING_DATE,
        intents=intents, instrument_ids=instrument_ids, spy=SPY,
        max_orders=len(intents), deadline=FUTURE, now=lambda: NOW,
        sleep=lambda _: None,
    )
    assert fake.violations == []
    return report


def _upro_fake(repo):
    fake = FakeExecClient(repo)
    fake.portfolio_queue = [
        [replace(_position(), instrument_id=5678)],
        [replace(_position(is_no_take_profit=True), instrument_id=5678)],
    ]
    return fake


def test_upro_catapult_uses_own_instrument_and_identity_and_clears_tp(repo, session_id):
    fake = _upro_fake(repo)
    report = _run(repo, session_id, fake, [
        LadderIntent("UPRO", "open", amount=633.33, book="catapult"),
    ])
    result = report.results[0]
    assert result.outcome == "filled"
    assert [call[1] for call in fake.buy_calls] == [5678]
    intent = repo.get_intents(session_id)[0]
    assert (intent.symbol, intent.instrument_id, intent.book) == ("UPRO", 5678, "catapult")
    order = repo.get_order(result.order_row_id)
    assert (order.symbol, order.instrument_id) == ("UPRO", 5678)
    annotation = repo.get_lot_annotation("P1")
    assert (annotation.symbol, annotation.book, annotation.bucket_id) == ("UPRO", "catapult", None)
    assert annotation.entry_reference_spy == SPY.close
    assert len(fake.patch_calls) == 1
    assert [row["status"] for row in repo.list_tp_clears("P1")] == ["cleared"]
    assert report.tp_failed == report.tp_uncleared == report.followups == ()


@pytest.mark.parametrize("reason", [
    f"deferred reentry {BUCKET_ID} 1/3", "not a parseable reentry reason",
])
def test_reentry_persists_explicit_bucket_without_parsing_reason(repo, session_id, reason):
    fake = _upro_fake(repo)
    report = _run(repo, session_id, fake, [
        LadderIntent("UPRO", "open", amount=633.33, reason=reason,
                     book="reentry", bucket_id=BUCKET_ID),
    ])
    intent = repo.get_intents(session_id)[0]
    assert (intent.symbol, intent.book, intent.bucket_id) == ("UPRO", "reentry", BUCKET_ID)
    annotation = repo.get_lot_annotation("P1")
    assert (annotation.symbol, annotation.book, annotation.bucket_id) == ("UPRO", "reentry", BUCKET_ID)
    assert report.followups == report.tp_failed == ()


def test_mixed_buys_submit_in_order_with_distinct_instruments(repo, session_id):
    fake = FakeExecClient(repo)
    fake.buy_queue = ["O1", "O2"]
    fake.poll_queue = [_poll(_filled()), _poll(_filled("P2", order_id="O2"))]
    fake.portfolio_queue = [
        [_position(is_no_take_profit=True)],
        [replace(_position("P2", is_no_take_profit=True), instrument_id=5678)],
    ]
    report = _run(repo, session_id, fake, [
        LadderIntent("SCHG", "open", amount=633.33),
        LadderIntent("UPRO", "open", amount=633.33, book="catapult"),
    ])
    assert [call[1] for call in fake.buy_calls] == [1234, 5678]
    assert [row.symbol for row in repo.get_intents(session_id)] == ["SCHG", "UPRO"]
    assert [repo.get_order(row.order_row_id).symbol for row in report.results] == ["SCHG", "UPRO"]
    assert repo.get_lot_annotation("P1").book == "catapult"
    assert repo.get_lot_annotation("P2").symbol == "UPRO"
    assert report.tp_failed == report.tp_uncleared == report.followups == ()


@pytest.mark.parametrize("kwargs,ids", [
    ({"symbol": "UPRO", "book": "catapult"}, {"SCHG": 1234}),
    ({"symbol": "SPY", "book": "catapult"}, {**INSTRUMENT_IDS, "SPY": 9999}),
    ({"side": "close"}, INSTRUMENT_IDS),
    ({"amount": None}, INSTRUMENT_IDS),
    ({"amount": 0.0}, INSTRUMENT_IDS),
    ({"amount": -1.0}, INSTRUMENT_IDS),
    ({"book": "unknown"}, INSTRUMENT_IDS),
    ({"book": ""}, INSTRUMENT_IDS),
    ({"symbol": "UPRO", "book": None}, INSTRUMENT_IDS),
    ({"symbol": "UPRO", "book": "reentry"}, INSTRUMENT_IDS),
])
def test_invalid_batch_refused_before_any_write_or_broker_call(repo, session_id, kwargs, ids):
    fake = FakeExecClient(repo)
    values = {"symbol": "SCHG", "side": "open", "amount": 100.0, "book": None}
    values.update(kwargs)
    with pytest.raises(ValueError):
        _run(repo, session_id, fake, [
            LadderIntent("SCHG", "open", amount=100.0), LadderIntent(**values),
        ], instrument_ids=ids)
    assert fake.calls == []
    assert repo.get_intents(session_id) == []
    assert repo.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


@pytest.mark.parametrize("recovery", [False, True])
def test_instrument_mismatch_skips_annotation_and_tp_and_reports_halt(repo, session_id, recovery):
    fake = FakeExecClient(repo)  # Broker returns SCHG for a submitted UPRO buy.
    if recovery:
        _seed_reentry(repo, session_id)
        fake.recover_queue = [_filled()]
        report = recover_unfinished(repo=repo, client=fake, abandon_after_sessions=3,
                                    sleep=lambda _: None)
        assert fake.buy_calls == []
    else:
        report = _run(repo, session_id, fake, [
            LadderIntent("UPRO", "open", amount=633.33, book="catapult"),
        ])
    assert repo.get_lot_annotation("P1") is None
    assert report.tp_failed == ("P1",)
    assert report.tp_uncleared == ()
    assert any("instrument mismatch" in line for line in report.followups)
    assert fake.patch_calls == []
    assert repo.list_tp_clears() == []


def _seed_reentry(repo, session_id):
    repo.insert_decision(Decision(
        session_id=session_id, trading_date=TRADING_DATE.isoformat(),
        spy_close=600.0, spy_ath=700.0, drawdown=-0.07, tier=1,
    ))
    intent_id = repo.insert_intent(Intent(
        session_id=session_id, request_id="recover-upro", sequence=1,
        symbol="UPRO", side="buy", instrument_id=5678, amount_usd=633.33,
        book="reentry", bucket_id=BUCKET_ID, reason="arbitrary persisted reason",
    ))
    repo.set_intent_status(intent_id, "sent")


def test_recovery_restores_upro_reentry_and_bucket_without_resubmitting(repo, session_id):
    _seed_reentry(repo, session_id)
    fake = _upro_fake(repo)
    fake.recover_queue = [_filled()]
    report = recover_unfinished(repo=repo, client=fake, abandon_after_sessions=3,
                                sleep=lambda _: None)
    annotation = repo.get_lot_annotation("P1")
    assert (annotation.symbol, annotation.book, annotation.bucket_id) == ("UPRO", "reentry", BUCKET_ID)
    assert annotation.entry_reference_spy == 600.0
    assert repo.get_intent_by_request_id("recover-upro").status == "recovered"
    order = repo.get_order_by_reference_id("recover-upro")
    assert (order.symbol, order.instrument_id) == ("UPRO", 5678)
    assert fake.buy_calls == fake.violations == []
    assert len(fake.patch_calls) == 1
    assert report.tp_failed == report.tp_uncleared == report.followups == ()
    assert len(report.resolved) == 1


def test_strategy_reentry_intent_carries_bucket_and_legacy_default_is_none():
    bucket = ReentryBucket(BUCKET_ID, "2026-10-01", 680.0, 300.0, 100.0,
                           created_bar=0, last_reference_spy=680.0)
    decision = decide(LadderInputs(
        trading_date=TRADING_DATE.isoformat(), year=2026, spy=SPY, cone_z=0.0,
        upro_price=100.0, schg_price=50.0, cash=1000.0, equity=10000.0,
        schg_position_value=6000.0, open_lots=(), buckets=(bucket,),
    ), LadderState(), LadderConfig())
    reentry = [intent for intent in decision.intents if intent.book == "reentry"]
    assert len(reentry) == 1
    assert reentry[0].bucket_id == BUCKET_ID
    assert reentry[0].amount == 100.0
    assert LadderIntent("SCHG", "open", amount=100.0).bucket_id is None
