"""Tests for nightwatch.engine.reconcile (DESIGN §1.4 fix #3, §5 gate 11;
DESIGN_market_data.md §4.5's "inverted gate 11").

Covers both required directions -- a broker position with no matching
sidecar row, and a sidecar row marked open with no matching broker position
-- plus the non-managed-symbol carve-out (display-only, never a
discrepancy) and the closed-annotation-but-broker-still-open case folded
into the same "unmatched_broker_position" kind.
"""

from __future__ import annotations

import pytest

from nightwatch.db.repo import Intent, LotAnnotation
from nightwatch.engine.reconcile import (
    ADOPTED_FROM_ABANDONED_INTENT,
    RESOLVED_FROM_ABANDONED_INTENT,
    Adoption,
    Discrepancy,
    Resolution,
    reconcile,
    resolve_abandoned_close_intents,
    resolve_abandoned_intent_adoptions,
)
from nightwatch.etoro.models import Position

UPRO_ID = 3244
SCHG_ID = 1084
SPY_ID = 3000
OTHER_ID = 6365  # a real non-managed instrument id per board nightwatch/handoff-2026-09-06.md §5

MANAGED_SYMBOLS = ("SCHG", "UPRO")
INSTRUMENT_SYMBOLS = {UPRO_ID: "UPRO", SCHG_ID: "SCHG", SPY_ID: "SPY"}


def _position(
    position_id: str,
    instrument_id: int,
    *,
    units: float = 1.0,
    entry_price: float = 50.0,
    amount: float = 50.0,
    is_buy: bool = True,
    leverage: int = 1,
) -> Position:
    return Position(
        position_id=position_id,
        instrument_id=instrument_id,
        units=units,
        entry_price=entry_price,
        amount=amount,
        leverage=leverage,
        is_buy=is_buy,
        open_date="2026-08-21T10:00:00+00:00",
        raw={},
    )


def _annotation(
    position_id: str,
    symbol: str,
    *,
    book: str = "catapult",
    origin: str = "app",
    is_open: bool = True,
) -> LotAnnotation:
    return LotAnnotation(
        position_id=position_id,
        symbol=symbol,
        book=book,
        entry_date="2026-08-21",
        entry_amount=50.0,
        origin=origin,
        is_open=is_open,
    )


# ---------------------------------------------------------------------------
# The happy path -- nothing should ever fail a clean, fully-matched book
# ---------------------------------------------------------------------------


def test_reconcile_passes_clean_when_broker_and_sidecar_fully_agree() -> None:
    positions = [
        _position("pos-upro-1", UPRO_ID),
        _position("pos-schg-1", SCHG_ID),
    ]
    annotations = [
        _annotation("pos-upro-1", "UPRO"),
        _annotation("pos-schg-1", "SCHG"),
    ]
    result = reconcile(
        positions,
        annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert result == []


# ---------------------------------------------------------------------------
# Direction 1: a broker position with no matching lot -- NEVER fabricate one
# ---------------------------------------------------------------------------


def test_unmatched_broker_position_with_no_annotation_at_all() -> None:
    positions = [_position("pos-mystery", UPRO_ID)]
    result = reconcile(
        positions,
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert len(result) == 1
    d = result[0]
    assert isinstance(d, Discrepancy)
    assert d.kind == "unmatched_broker_position"
    assert d.position_id == "pos-mystery"
    assert d.symbol == "UPRO"
    assert "no lot_annotations row" in d.reason
    assert "fabricate" in d.reason


def test_unmatched_broker_position_when_annotation_exists_but_closed() -> None:
    """An 'assumed-complete close that wasn't' (DESIGN_session.md §3.4): the
    sidecar thinks this lot is closed, but the broker still shows it open.
    Folded into the same discrepancy kind -- the failure mode (no *open*
    sidecar row for an open broker position) is identical."""
    positions = [_position("pos-schg-9", SCHG_ID)]
    annotations = [_annotation("pos-schg-9", "SCHG", is_open=False)]
    result = reconcile(
        positions,
        annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert len(result) == 1
    d = result[0]
    assert d.kind == "unmatched_broker_position"
    assert d.position_id == "pos-schg-9"
    assert "marked closed" in d.reason


# ---------------------------------------------------------------------------
# Direction 2: a lot marked open with no matching broker position
# ---------------------------------------------------------------------------


def test_unmatched_open_lot_with_no_broker_position() -> None:
    annotations = [_annotation("pos-vanished", "UPRO")]
    result = reconcile(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert len(result) == 1
    d = result[0]
    assert d.kind == "unmatched_open_lot"
    assert d.position_id == "pos-vanished"
    assert d.symbol == "UPRO"
    assert "no matching broker position" in d.reason


def test_closed_lot_annotation_with_no_broker_position_is_not_a_discrepancy() -> None:
    """A closed sidecar row naturally has no live broker position -- that is
    expected, not a discrepancy."""
    annotations = [_annotation("pos-long-closed", "UPRO", is_open=False)]
    result = reconcile(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert result == []


# ---------------------------------------------------------------------------
# Non-managed symbols: display-only, never a failure (DESIGN.md §1.4, §6.D)
# ---------------------------------------------------------------------------


def test_non_managed_symbol_position_is_never_a_discrepancy() -> None:
    positions = [_position("pos-other", OTHER_ID)]
    result = reconcile(
        positions,
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols={**INSTRUMENT_SYMBOLS, OTHER_ID: "IAU"},
    )
    assert result == []


def test_unresolvable_instrument_id_is_treated_as_non_managed_not_a_failure() -> None:
    """gate 8 guarantees UPRO/SCHG/SPY are always resolved before gate 11
    runs -- an instrument_id this app has never cached is therefore always
    genuinely non-managed, not a managed-symbol miss slipping through."""
    positions = [_position("pos-unknown-instrument", 999999)]
    result = reconcile(
        positions,
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert result == []


# ---------------------------------------------------------------------------
# Both directions can fire in the same reconcile() call, independently
# ---------------------------------------------------------------------------


def test_both_directions_fire_independently_in_one_call() -> None:
    positions = [
        _position("pos-good", UPRO_ID),
        _position("pos-broker-only", SCHG_ID),
    ]
    annotations = [
        _annotation("pos-good", "UPRO"),
        _annotation("pos-sidecar-only", "UPRO"),
    ]
    result = reconcile(
        positions,
        annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    kinds_by_position = {d.position_id: d.kind for d in result}
    assert kinds_by_position == {
        "pos-broker-only": "unmatched_broker_position",
        "pos-sidecar-only": "unmatched_open_lot",
    }


# ---------------------------------------------------------------------------
# Discrepancy structural safety
# ---------------------------------------------------------------------------


def test_discrepancy_refuses_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        Discrepancy(
            kind="something_else",
            position_id="pos-1",
            symbol="UPRO",
            reason="bogus",
        )


# ---------------------------------------------------------------------------
# resolve_abandoned_intent_adoptions -- the narrow auto-adopt path
# (DESIGN_session.md §3.4, the operator's 2026-09-06 "no forced
# human-in-the-loop" decision). THE CRITICAL DISTINCTION: this must never be
# confused with, or slide into, the fabricate-a-lot bug (DESIGN.md §1.4 fix
# #3) -- a bare unmatched broker position with no matching abandoned intent
# must ALWAYS still fail the gate, exactly as before.
# ---------------------------------------------------------------------------


def _abandoned_intent(
    *,
    request_id: str = "req-1",
    session_id: int = 1,
    sequence: int = 1,
    symbol: str = "SCHG",
    side: str = "buy",
    instrument_id: int = SCHG_ID,
    book: str | None = "catapult",
    amount_usd: float | None = 50.0,
    position_id: str | None = None,
    bucket_id: str | None = None,
    status: str = "abandoned",
) -> Intent:
    return Intent(
        session_id=session_id,
        request_id=request_id,
        sequence=sequence,
        symbol=symbol,
        side=side,
        instrument_id=instrument_id,
        book=book,
        amount_usd=amount_usd,
        position_id=position_id,
        bucket_id=bucket_id,
        status=status,
    )


def test_adopts_an_unmatched_position_matching_one_abandoned_intent() -> None:
    """The one case the whole feature exists for: an abandoned buy order
    later fills, and the resulting broker position matches our own
    persisted, abandoned intent record by instrument and size."""
    position = _position("pos-late-fill", SCHG_ID, amount=50.0)
    discrepancies = reconcile(
        [position],
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert len(discrepancies) == 1  # sanity: it is a discrepancy before adoption

    intent = _abandoned_intent(amount_usd=50.0)
    remaining, adoptions = resolve_abandoned_intent_adoptions(
        [position],
        annotations=[],
        discrepancies=discrepancies,
        abandoned_intents=[intent],
        size_tolerance_pct=0.02,
    )

    assert remaining == []
    assert len(adoptions) == 1
    adoption = adoptions[0]
    assert isinstance(adoption, Adoption)
    assert adoption.position_id == "pos-late-fill"
    assert adoption.intent is intent
    assert adoption.annotation.position_id == "pos-late-fill"
    assert adoption.annotation.symbol == "SCHG"
    assert adoption.annotation.book == "catapult"
    assert adoption.annotation.origin == "app"
    assert adoption.annotation.entry_context_source == ADOPTED_FROM_ABANDONED_INTENT
    assert adoption.annotation.entry_context_source == "adopted_from_abandoned_intent"
    assert adoption.annotation.entry_amount == position.amount
    assert adoption.annotation.entry_date == position.open_date


def test_bare_unmatched_position_with_no_abandoned_intents_still_fails_closed() -> None:
    """THE load-bearing test: with an EMPTY abandoned_intents (the default
    everywhere this is called from), a bare unmatched broker position is
    NEVER adopted -- it fails exactly as it did before this feature existed.
    This must fail loudly if adoption ever starts happening with no
    provenance in hand."""
    position = _position("pos-mystery", SCHG_ID)
    discrepancies = reconcile(
        [position],
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    remaining, adoptions = resolve_abandoned_intent_adoptions(
        [position],
        annotations=[],
        discrepancies=discrepancies,
        abandoned_intents=[],
        size_tolerance_pct=0.02,
    )
    assert adoptions == []
    assert remaining == discrepancies
    assert len(remaining) == 1
    assert remaining[0].kind == "unmatched_broker_position"


def test_bare_unmatched_position_with_unrelated_abandoned_intents_still_fails_closed() -> None:
    """Same as above, but with abandoned intents present -- just none of
    them describe THIS position. Presence of the feature must never lower
    the bar for positions with no genuine provenance."""
    position = _position("pos-mystery", SCHG_ID, amount=50.0)
    discrepancies = reconcile(
        [position],
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    unrelated_instrument = _abandoned_intent(
        request_id="req-other-instrument", instrument_id=UPRO_ID, symbol="UPRO", amount_usd=50.0
    )
    wrong_size = _abandoned_intent(request_id="req-wrong-size", amount_usd=500.0)
    not_abandoned = _abandoned_intent(request_id="req-still-pending", status="pending", amount_usd=50.0)
    not_a_buy = _abandoned_intent(request_id="req-a-close", side="close", amount_usd=50.0)
    remaining, adoptions = resolve_abandoned_intent_adoptions(
        [position],
        annotations=[],
        discrepancies=discrepancies,
        abandoned_intents=[unrelated_instrument, wrong_size, not_abandoned, not_a_buy],
        size_tolerance_pct=0.02,
    )
    assert adoptions == []
    assert remaining == discrepancies


def test_ambiguous_match_two_candidate_intents_fails_closed_not_arbitrarily_resolved() -> None:
    """Governing principle: halts are for money-losing ambiguity, never
    bookkeeping ambiguity. Two abandoned intents that both plausibly
    describe the same position is genuine ambiguity about which of our own
    orders this is -- refused, not arbitrarily resolved by picking one."""
    position = _position("pos-ambiguous", SCHG_ID, amount=50.0)
    discrepancies = reconcile(
        [position],
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    intent_a = _abandoned_intent(request_id="req-a", amount_usd=50.0)
    intent_b = _abandoned_intent(request_id="req-b", amount_usd=50.5)
    remaining, adoptions = resolve_abandoned_intent_adoptions(
        [position],
        annotations=[],
        discrepancies=discrepancies,
        abandoned_intents=[intent_a, intent_b],
        size_tolerance_pct=0.02,
    )
    assert adoptions == []
    assert remaining == discrepancies


def test_closed_annotation_case_is_never_eligible_for_adoption() -> None:
    """The 'assumed-complete close that wasn't' shape (a lot_annotations
    row exists but is marked closed while the broker position is still
    open) is a DIFFERENT anomaly from 'no provenance at all' -- it must
    never be resolved by this path even when a plausible abandoned intent
    exists, since a sidecar row already claims a definite, contradicted
    answer."""
    position = _position("pos-schg-9", SCHG_ID, amount=50.0)
    annotations = [_annotation("pos-schg-9", "SCHG", is_open=False)]
    discrepancies = reconcile(
        [position],
        annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    intent = _abandoned_intent(amount_usd=50.0)
    remaining, adoptions = resolve_abandoned_intent_adoptions(
        [position],
        annotations,
        discrepancies=discrepancies,
        abandoned_intents=[intent],
        size_tolerance_pct=0.02,
    )
    assert adoptions == []
    assert remaining == discrepancies


def test_unmatched_open_lot_direction_is_never_touched_by_adoption() -> None:
    """This path only ever addresses direction 1 (unmatched_broker_position)
    -- an unmatched_open_lot discrepancy (a lot marked open with no broker
    position) must pass through completely unchanged, no matter what
    abandoned intents are supplied."""
    annotations = [_annotation("pos-vanished", "UPRO")]
    discrepancies = reconcile(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    intent = _abandoned_intent(instrument_id=UPRO_ID, symbol="UPRO", amount_usd=50.0)
    remaining, adoptions = resolve_abandoned_intent_adoptions(
        positions=[],
        annotations=annotations,
        discrepancies=discrepancies,
        abandoned_intents=[intent],
        size_tolerance_pct=0.02,
    )
    assert adoptions == []
    assert remaining == discrepancies


def test_size_tolerance_boundary_just_inside_adopts() -> None:
    position = _position("pos-slippage", SCHG_ID, amount=51.0)  # 2% over $50
    discrepancies = reconcile(
        [position],
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    intent = _abandoned_intent(amount_usd=50.0)
    remaining, adoptions = resolve_abandoned_intent_adoptions(
        [position],
        annotations=[],
        discrepancies=discrepancies,
        abandoned_intents=[intent],
        size_tolerance_pct=0.02,
    )
    assert len(adoptions) == 1
    assert remaining == []


def test_size_tolerance_boundary_just_outside_fails_closed() -> None:
    position = _position("pos-too-different", SCHG_ID, amount=52.0)  # 4% over $50
    discrepancies = reconcile(
        [position],
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    intent = _abandoned_intent(amount_usd=50.0)
    remaining, adoptions = resolve_abandoned_intent_adoptions(
        [position],
        annotations=[],
        discrepancies=discrepancies,
        abandoned_intents=[intent],
        size_tolerance_pct=0.02,
    )
    assert adoptions == []
    assert remaining == discrepancies


def test_short_position_is_refused_even_with_a_matching_abandoned_intent() -> None:
    """DESIGN §6.A: never short, never leveraged above 1. An auto-adopted
    lot is exactly the place to hold that line most tightly, since nothing
    else double-checks it before this annotation is persisted."""
    position = _position("pos-short", SCHG_ID, amount=50.0, is_buy=False)
    discrepancies = reconcile(
        [position],
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    intent = _abandoned_intent(amount_usd=50.0)
    remaining, adoptions = resolve_abandoned_intent_adoptions(
        [position],
        annotations=[],
        discrepancies=discrepancies,
        abandoned_intents=[intent],
        size_tolerance_pct=0.02,
    )
    assert adoptions == []
    assert remaining == discrepancies


def test_leveraged_position_is_refused_even_with_a_matching_abandoned_intent() -> None:
    position = _position("pos-leveraged", SCHG_ID, amount=50.0, leverage=2)
    discrepancies = reconcile(
        [position],
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    intent = _abandoned_intent(amount_usd=50.0)
    remaining, adoptions = resolve_abandoned_intent_adoptions(
        [position],
        annotations=[],
        discrepancies=discrepancies,
        abandoned_intents=[intent],
        size_tolerance_pct=0.02,
    )
    assert adoptions == []
    assert remaining == discrepancies


def test_intent_already_linked_to_a_different_position_is_not_a_candidate() -> None:
    position = _position("pos-x", SCHG_ID, amount=50.0)
    discrepancies = reconcile(
        [position],
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    intent = _abandoned_intent(amount_usd=50.0, position_id="pos-someone-else")
    remaining, adoptions = resolve_abandoned_intent_adoptions(
        [position],
        annotations=[],
        discrepancies=discrepancies,
        abandoned_intents=[intent],
        size_tolerance_pct=0.02,
    )
    assert adoptions == []
    assert remaining == discrepancies


def test_intent_with_invalid_book_is_not_adopted() -> None:
    """intents.book permits 'core' (schema.sql); lot_annotations.book does
    not. An abandoned intent recorded with book='core' has no valid lot
    book to adopt into and must fail closed, not be silently coerced."""
    position = _position("pos-core-book", SCHG_ID, amount=50.0)
    discrepancies = reconcile(
        [position],
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    intent = _abandoned_intent(amount_usd=50.0, book="core")
    remaining, adoptions = resolve_abandoned_intent_adoptions(
        [position],
        annotations=[],
        discrepancies=discrepancies,
        abandoned_intents=[intent],
        size_tolerance_pct=0.02,
    )
    assert adoptions == []
    assert remaining == discrepancies


def test_empty_abandoned_intents_is_a_complete_no_op() -> None:
    """The default everywhere this is called from: passing no abandoned
    intents at all must return discrepancies verbatim and never adopt."""
    positions = [_position("pos-mystery", UPRO_ID)]
    discrepancies = reconcile(
        positions,
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    remaining, adoptions = resolve_abandoned_intent_adoptions(
        positions,
        annotations=[],
        discrepancies=discrepancies,
        abandoned_intents=[],
        size_tolerance_pct=0.02,
    )
    assert remaining == discrepancies
    assert adoptions == []


# ---------------------------------------------------------------------------
# resolve_abandoned_close_intents -- the mirror-image auto-resolve path
# (DESIGN_session.md §3.4, closing the gap resolve_abandoned_intent_adoptions's
# own docstring named but never built: a stuck CLOSE that later fills must
# resolve via THIS direction, never the buy-side adopt path. THE CRITICAL
# PROPERTY: a position that vanished for any OTHER reason (broker-side
# liquidation, or the operator hand-closing it in the eToro app) has no
# matching abandoned close intent of ours and must ALWAYS still fail the
# gate -- that is the entire safety argument for this feature.
# ---------------------------------------------------------------------------


def _abandoned_close_intent(
    *,
    request_id: str = "req-close-1",
    session_id: int = 1,
    sequence: int = 1,
    symbol: str = "SCHG",
    instrument_id: int = SCHG_ID,
    book: str | None = "catapult",
    units: float | None = 1.0,
    position_id: str | None = "pos-vanished",
    status: str = "abandoned",
) -> Intent:
    return Intent(
        session_id=session_id,
        request_id=request_id,
        sequence=sequence,
        symbol=symbol,
        side="close",
        instrument_id=instrument_id,
        book=book,
        units=units,
        position_id=position_id,
        status=status,
    )


def test_resolves_a_vanished_position_matching_one_abandoned_close_intent() -> None:
    """The one case the whole feature exists for: an abandoned close order
    later fills, the broker position disappears, and the sidecar row that
    still says open matches our own persisted, abandoned close intent by
    position, symbol, and book."""
    annotations = [_annotation("pos-vanished", "SCHG")]
    discrepancies = reconcile(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert len(discrepancies) == 1  # sanity: it is a discrepancy before resolution
    assert discrepancies[0].kind == "unmatched_open_lot"

    intent = _abandoned_close_intent()
    remaining, resolutions = resolve_abandoned_close_intents(
        annotations, discrepancies, abandoned_intents=[intent]
    )

    assert remaining == []
    assert len(resolutions) == 1
    resolution = resolutions[0]
    assert isinstance(resolution, Resolution)
    assert resolution.position_id == "pos-vanished"
    assert resolution.intent is intent
    assert resolution.annotation.position_id == "pos-vanished"
    assert resolution.annotation.symbol == "SCHG"
    assert resolution.annotation.book == "catapult"
    assert resolution.annotation.is_open is False
    assert resolution.annotation.exit_context_source == RESOLVED_FROM_ABANDONED_INTENT
    assert resolution.annotation.exit_context_source == "resolved_from_abandoned_intent"
    # Everything else (the re-link key, origin, entry context) is untouched.
    assert resolution.annotation.entry_date == "2026-08-21"
    assert resolution.annotation.entry_amount == 50.0
    assert resolution.annotation.origin == "app"


def test_vanished_position_with_no_abandoned_close_intents_still_fails_closed() -> None:
    """THE load-bearing test: with an EMPTY abandoned_intents (the default
    everywhere this is called from), a vanished position is NEVER resolved
    -- it fails exactly as it did before this feature existed. This is the
    property that must never stop holding: it is what preserves a halt for
    a broker-side liquidation or a hand-close with no record of ours."""
    annotations = [_annotation("pos-vanished", "SCHG")]
    discrepancies = reconcile(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    remaining, resolutions = resolve_abandoned_close_intents(
        annotations, discrepancies, abandoned_intents=[]
    )
    assert resolutions == []
    assert remaining == discrepancies
    assert len(remaining) == 1
    assert remaining[0].kind == "unmatched_open_lot"


def test_broker_side_liquidation_or_hand_close_still_fails_closed() -> None:
    """THE property this whole feature must never weaken: a position that
    vanished for a reason that is NOT one of our own abandoned close orders
    (broker-side liquidation, or the operator closing it by hand in the
    eToro app) has no matching intent, however many unrelated abandoned
    intents happen to be present, and must still fail the gate."""
    annotations = [_annotation("pos-vanished", "SCHG")]
    discrepancies = reconcile(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    unrelated_position = _abandoned_close_intent(
        request_id="req-other-position", position_id="pos-someone-else"
    )
    unrelated_symbol = _abandoned_close_intent(
        request_id="req-other-symbol", symbol="UPRO", instrument_id=UPRO_ID
    )
    unrelated_book = _abandoned_close_intent(request_id="req-other-book", book="reentry")
    not_abandoned = _abandoned_close_intent(request_id="req-still-pending", status="pending")
    not_a_close = Intent(
        session_id=1,
        request_id="req-a-buy",
        sequence=1,
        symbol="SCHG",
        side="buy",
        instrument_id=SCHG_ID,
        book="catapult",
        position_id="pos-vanished",
        status="abandoned",
    )
    remaining, resolutions = resolve_abandoned_close_intents(
        annotations,
        discrepancies,
        abandoned_intents=[
            unrelated_position,
            unrelated_symbol,
            unrelated_book,
            not_abandoned,
            not_a_close,
        ],
    )
    assert resolutions == []
    assert remaining == discrepancies


def test_ambiguous_close_match_two_candidate_intents_fails_closed() -> None:
    """Governing principle: halts are for money-losing ambiguity, never
    bookkeeping ambiguity. Two abandoned close intents that both plausibly
    describe the same vanished position is genuine ambiguity about which of
    our own close orders this was -- refused, not arbitrarily resolved."""
    annotations = [_annotation("pos-vanished", "SCHG")]
    discrepancies = reconcile(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    intent_a = _abandoned_close_intent(request_id="req-close-a")
    intent_b = _abandoned_close_intent(request_id="req-close-b")
    remaining, resolutions = resolve_abandoned_close_intents(
        annotations, discrepancies, abandoned_intents=[intent_a, intent_b]
    )
    assert resolutions == []
    assert remaining == discrepancies


def test_unmatched_broker_position_direction_is_never_touched_by_close_resolution() -> None:
    """This path only ever addresses direction 2 (unmatched_open_lot) -- an
    unmatched_broker_position discrepancy must pass through completely
    unchanged, no matter what abandoned close intents are supplied."""
    positions = [_position("pos-mystery", UPRO_ID)]
    discrepancies = reconcile(
        positions,
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    intent = _abandoned_close_intent(
        symbol="UPRO", instrument_id=UPRO_ID, position_id="pos-mystery"
    )
    remaining, resolutions = resolve_abandoned_close_intents(
        [], discrepancies, abandoned_intents=[intent]
    )
    assert resolutions == []
    assert remaining == discrepancies


def test_closed_annotation_has_no_matching_discrepancy_so_is_never_resolved() -> None:
    """A closed sidecar row never produces an unmatched_open_lot discrepancy
    in the first place (see test_closed_lot_annotation_with_no_broker_
    position_is_not_a_discrepancy above) -- confirm the close-resolution
    path has nothing to do here either, defensively."""
    annotations = [_annotation("pos-long-closed", "UPRO", is_open=False)]
    discrepancies = reconcile(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert discrepancies == []
    intent = _abandoned_close_intent(
        symbol="UPRO", instrument_id=UPRO_ID, position_id="pos-long-closed", book="catapult"
    )
    remaining, resolutions = resolve_abandoned_close_intents(
        annotations, discrepancies, abandoned_intents=[intent]
    )
    assert resolutions == []
    assert remaining == []


def test_close_intent_with_mismatched_book_is_not_a_candidate() -> None:
    annotations = [_annotation("pos-vanished", "SCHG", book="catapult")]
    discrepancies = reconcile(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    intent = _abandoned_close_intent(book="reentry")
    remaining, resolutions = resolve_abandoned_close_intents(
        annotations, discrepancies, abandoned_intents=[intent]
    )
    assert resolutions == []
    assert remaining == discrepancies


def test_close_intent_with_unset_position_id_is_not_a_candidate() -> None:
    """Unlike a buy intent (whose position_id starts unset until a fill
    confirms it), a close intent's position_id is set at creation time
    (schema.sql: "set for side='close'") -- an intent with none recorded is
    malformed data, not a candidate, never treated as 'unset, so matches
    anything' the way the buy side tolerates for its own position_id."""
    annotations = [_annotation("pos-vanished", "SCHG")]
    discrepancies = reconcile(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    intent = _abandoned_close_intent(position_id=None)
    remaining, resolutions = resolve_abandoned_close_intents(
        annotations, discrepancies, abandoned_intents=[intent]
    )
    assert resolutions == []
    assert remaining == discrepancies


def test_empty_abandoned_close_intents_is_a_complete_no_op() -> None:
    """The default parameter: passing no abandoned close intents at all
    must return discrepancies verbatim and never resolve anything."""
    annotations = [_annotation("pos-vanished", "UPRO")]
    discrepancies = reconcile(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    remaining, resolutions = resolve_abandoned_close_intents(annotations, discrepancies)
    assert remaining == discrepancies
    assert resolutions == []
