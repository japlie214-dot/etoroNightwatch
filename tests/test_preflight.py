"""Tests for nightwatch.engine.preflight's gate 10 freshness check
(DESIGN_market_data.md §6, §2.9, §10.4).

The acceptance bar (task brief, "Build that as a unit-test fixture"):
replaying the 2026-09-04 evidence -- UPRO rates at lastExecution=153.67,
date=2026-09-03T19:59:54.8409896Z (~19.0h stale versus the probe instant),
concurrent true price ~151.665, and a current, agreeing candles price --
must make gate 10 fail and name UPRO. A second fixture, all-fresh, must
pass: a gate that fails everything is not a working gate.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nightwatch.config import MarketDataConfig, ReconciliationConfig
from nightwatch.db.repo import Intent, LotAnnotation
from nightwatch.engine.preflight import (
    GateResult,
    find_broker_take_profits,
    ProvisionalClose,
    gate_10_price_data,
    gate_11_reconciliation,
    notify_gate_10_failure,
    notify_gate_11_failure,
)
from nightwatch.etoro.client import EtoroClient
from nightwatch.etoro.models import Candle, Position, Rate

SYMBOLS = ("SPY", "SCHG", "UPRO")

#: The exact instant the live probe in DESIGN_market_data.md §2.9 was taken
#: (one of the three polls: 11:15 ET == 15:15 UTC on 2026-09-04).
PROBE_NOW = datetime(2026, 9, 4, 15, 15, 0, tzinfo=timezone.utc)


def _config(**overrides: object) -> MarketDataConfig:
    return MarketDataConfig(**overrides)  # type: ignore[arg-type]


def _fresh_rate(instrument_id: int, price: float, *, when: datetime) -> Rate:
    return Rate(
        instrument_id=instrument_id,
        bid=price - 0.02,
        ask=price + 0.02,
        last_execution=price,
        timestamp=when.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
    )


def _candle(
    date: str, close: float, *, when: datetime = PROBE_NOW, interval: str = "OneMinute"
) -> Candle:
    """A candle fresh as of ``when`` (default: the same instant as the live
    probe) unless the caller overrides it. Candles were not timestamped at
    all before 2026-09-05 (``Candle.timestamp`` did not exist); every
    existing fixture is fresh by construction here so the freshness check
    added that day does not spuriously fail tests that predate it.

    ``interval`` defaults to ``"OneMinute"`` -- these fixtures represent the
    self-timestamped-at-bar-granularity candle DESIGN_market_data.md §2.11
    describes as the reliable current-price source, not a ``OneDay`` bar;
    the ``OneDay``-specific behaviour (no age bound, relies on value
    agreement) gets its own dedicated fixtures below."""
    return Candle(
        date=date,
        open=close,
        high=close,
        low=close,
        close=close,
        timestamp=when.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
        interval=interval,
    )


# ---------------------------------------------------------------------------
# The acceptance fixture -- replays DESIGN_market_data.md §2.9's live evidence
# ---------------------------------------------------------------------------


def _upro_stale_fixture() -> tuple[dict[str, Rate], dict[str, Candle], dict[str, ProvisionalClose]]:
    rates = {
        # SPY and SCHG ticked forward to the second in the same batch request
        # the UPRO poll came from (§2.9's table) -- both fresh.
        "SPY": _fresh_rate(3000, 769.62, when=PROBE_NOW),
        "SCHG": _fresh_rate(1084, 35.5, when=PROBE_NOW),
        # UPRO: byte-identical across 5+ polls, frozen at the prior session's
        # 16:00 ET close -- DESIGN_market_data.md §2.9's exact numbers.
        "UPRO": Rate(
            instrument_id=3244,
            bid=153.67,
            ask=153.71,
            last_execution=153.67,
            timestamp="2026-09-03T19:59:54.8409896Z",
        ),
    }
    candles = {
        # eToro's own candles are current and agree with the true price for
        # every symbol, UPRO included (§2.11: UPRO OneMinute close 151.19,
        # agreeing with eToro's own app (151.43) and Tiingo (151.295/151.665)
        # -- "a current, agreeing candles price").
        "SPY": _candle("2026-09-04", 769.6),
        "SCHG": _candle("2026-09-04", 35.5),
        "UPRO": _candle("2026-09-04", 151.19),
    }
    provisional = {
        "SPY": ProvisionalClose(symbol="SPY", source="tiingo_iex", value=769.5),
        "SCHG": ProvisionalClose(symbol="SCHG", source="tiingo_iex", value=35.49),
        "UPRO": ProvisionalClose(symbol="UPRO", source="tiingo_iex", value=151.665),
    }
    return rates, candles, provisional


def test_gate_10_fails_and_names_upro_on_the_2026_09_04_evidence() -> None:
    rates, candles, provisional = _upro_stale_fixture()
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert isinstance(result, GateResult)
    assert result.passed is False
    assert result.gate == 10
    assert "UPRO" in result.reason
    assert "UPRO" in result.details["stale_symbols"]
    # SPY and SCHG must NOT be implicated -- only the genuinely stale symbol.
    assert "SPY" not in result.details["stale_symbols"]
    assert "SCHG" not in result.details["stale_symbols"]
    # The age is named, not just the fact of staleness.
    assert any("h old" in msg for msg in result.details["rate_vs_candle_failures"])


def test_gate_10_failure_reason_reports_roughly_19_hours() -> None:
    rates, candles, provisional = _upro_stale_fixture()
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    upro_failure = next(
        msg for msg in result.details["rate_vs_candle_failures"] if "UPRO" in msg
    )
    assert "19." in upro_failure  # ~19.0h -- do not overfit to one decimal digit


def test_gate_10_would_also_fail_on_deviation_alone_if_timestamp_were_missing() -> None:
    """Confirms the value-comparison fallback works even without a
    timestamp -- DESIGN_market_data.md §5.1: Tiingo's own IEX endpoint has no
    usable timestamp at all, so this path must not depend on one being
    present everywhere."""
    rates, candles, provisional = _upro_stale_fixture()
    stale = rates["UPRO"]
    rates["UPRO"] = Rate(
        instrument_id=stale.instrument_id,
        bid=stale.bid,
        ask=stale.ask,
        last_execution=stale.last_execution,
        timestamp=None,
    )
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert result.passed is False
    assert "UPRO" in result.details["stale_symbols"]


# ---------------------------------------------------------------------------
# The all-fresh fixture -- a gate that fails everything is not a working gate
# ---------------------------------------------------------------------------


def _all_fresh_fixture() -> tuple[dict[str, Rate], dict[str, Candle], dict[str, ProvisionalClose]]:
    rates = {
        "SPY": _fresh_rate(3000, 769.62, when=PROBE_NOW),
        "SCHG": _fresh_rate(1084, 35.5, when=PROBE_NOW),
        "UPRO": _fresh_rate(3244, 151.66, when=PROBE_NOW),
    }
    candles = {
        "SPY": _candle("2026-09-04", 769.6),
        "SCHG": _candle("2026-09-04", 35.5),
        "UPRO": _candle("2026-09-04", 151.6),
    }
    provisional = {
        "SPY": ProvisionalClose(symbol="SPY", source="tiingo_iex", value=769.55),
        "SCHG": ProvisionalClose(symbol="SCHG", source="tiingo_iex", value=35.51),
        "UPRO": ProvisionalClose(symbol="UPRO", source="tiingo_iex", value=151.63),
    }
    return rates, candles, provisional


def test_gate_10_passes_when_everything_is_fresh_and_consistent() -> None:
    rates, candles, provisional = _all_fresh_fixture()
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert result.passed is True
    assert result.gate == 10
    assert result.details["stale_symbols"] == []
    assert result.details["sanity_failures"] == []
    assert result.details["rate_vs_candle_failures"] == []
    assert result.details["provisional_close_failures"] == []


def test_gate_10_fallback_provisional_close_from_etoro_candle_is_not_cross_checked_against_itself() -> None:
    """When Tiingo is unreachable and the session fell back to eToro's own
    candle as the provisional close, there is nothing independent left to
    validate it against inside this gate -- confirm this does not spuriously
    fail (comparing a value to itself would trivially "pass" for the wrong
    reason, or worse, be mistaken for meaningful validation)."""
    rates, candles, _ = _all_fresh_fixture()
    provisional = {
        "SPY": ProvisionalClose(symbol="SPY", source="etoro_candle", value=candles["SPY"].close),
        "SCHG": ProvisionalClose(symbol="SCHG", source="etoro_candle", value=candles["SCHG"].close),
        "UPRO": ProvisionalClose(symbol="UPRO", source="etoro_candle", value=candles["UPRO"].close),
    }
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert result.passed is True
    assert result.details["provisional_close_failures"] == []


# ---------------------------------------------------------------------------
# The hole this build closes: the candle's own freshness was never checked
# -- it was the trusted reference by construction. Three cases:
#   (a) a fresh Tiingo IEX close agreeing with a *stale* candle must now fail
#       (agreement with a stale number is not a freshness check, §10.2)
#   (b) an etoro_candle-sourced provisional close backed by a stale candle
#       must now fail (previously `continue`d straight through, ungated)
#   (c) a candle with a missing/unparseable timestamp must fail closed, not
#       be silently skipped (§3.3: no usable freshness signal is not fresh)
# ---------------------------------------------------------------------------


def test_gate_10_fails_when_tiingo_close_agrees_with_a_stale_candle() -> None:
    """The primary path's only freshness signal is the candle it is compared
    against (Tiingo IEX has none of its own, §10.2) -- a stale candle must
    fail even though the Tiingo value agrees with it."""
    rates, candles, provisional = _all_fresh_fixture()
    stale_when = PROBE_NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    candles["UPRO"] = _candle("2026-09-03", 151.6, when=stale_when)
    provisional["UPRO"] = ProvisionalClose(symbol="UPRO", source="tiingo_iex", value=151.6)
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert result.passed is False
    assert any(
        "UPRO" in msg and "candle" in msg and "minutes old" in msg
        for msg in result.details["provisional_close_failures"]
    )


def test_gate_10_fails_when_etoro_candle_sourced_close_is_backed_by_a_stale_candle() -> None:
    """The fallback path (Tiingo unreachable, provisional close taken
    straight from the candle) must not sail through ungated just because
    there is no independent value left to check -- freshness still applies."""
    rates, candles, _ = _all_fresh_fixture()
    stale_when = PROBE_NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    candles["UPRO"] = _candle("2026-09-03", 151.6, when=stale_when)
    provisional = {
        "SPY": ProvisionalClose(symbol="SPY", source="etoro_candle", value=candles["SPY"].close),
        "SCHG": ProvisionalClose(symbol="SCHG", source="etoro_candle", value=candles["SCHG"].close),
        "UPRO": ProvisionalClose(symbol="UPRO", source="etoro_candle", value=candles["UPRO"].close),
    }
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert result.passed is False
    assert any(
        "UPRO" in msg and "minutes old" in msg
        for msg in result.details["provisional_close_failures"]
    )


def test_gate_10_fails_closed_when_candle_timestamp_is_missing() -> None:
    """A candle with no usable timestamp must fail the gate, not be
    silently skipped -- DESIGN_market_data.md §3.3: no usable freshness
    signal is treated the same as no data at all."""
    rates, candles, provisional = _all_fresh_fixture()
    stale_candle = candles["UPRO"]
    candles["UPRO"] = Candle(
        date=stale_candle.date,
        open=stale_candle.open,
        high=stale_candle.high,
        low=stale_candle.low,
        close=stale_candle.close,
        timestamp=None,
        interval=stale_candle.interval,
    )
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert result.passed is False
    assert any(
        "UPRO" in msg and ("missing" in msg or "unparseable" in msg)
        for msg in result.details["provisional_close_failures"]
    )


# ---------------------------------------------------------------------------
# Candle.interval (added 2026-09-05): one flat bound cannot honestly serve
# both a OneMinute bar and a OneDay bar. These confirm the per-interval fix:
#   - a OneDay candle hours old by timestamp, but value-agreeing, now passes
#     (previously would have spuriously failed under the old flat 20-minute
#     bound)
#   - a genuinely stale OneMinute candle still fails
#   - an unrecognised/missing interval fails closed
# ---------------------------------------------------------------------------


def test_gate_10_oneday_candle_hours_old_does_not_spuriously_fail() -> None:
    """A OneDay candle's timestamp can legitimately be hours old (DESIGN_
    market_data.md §2.11: the daily bar's close tracks the live price
    intraday even though its date field may carry no intraday time
    component). Under the old flat 20-minute bound this would have failed
    spuriously; now OneDay relies on value agreement instead of age, and a
    value-agreeing candle must pass."""
    rates, candles, provisional = _all_fresh_fixture()
    hours_old = PROBE_NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    candles["UPRO"] = _candle("2026-09-04", 151.6, when=hours_old, interval="OneDay")
    # Tiingo IEX value agrees closely with the (functionally current) daily
    # candle close -- well within provisional_close_deviation_pct.
    provisional["UPRO"] = ProvisionalClose(symbol="UPRO", source="tiingo_iex", value=151.62)
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert result.passed is True
    assert result.details["provisional_close_failures"] == []


def test_gate_10_still_fails_for_a_genuinely_stale_oneminute_candle() -> None:
    """The per-interval fix must not weaken the intraday case: a OneMinute
    candle well past the intraday bound still fails."""
    rates, candles, provisional = _all_fresh_fixture()
    stale_when = PROBE_NOW.replace(hour=PROBE_NOW.hour - 1)  # 60 min > 20-min bound
    candles["UPRO"] = _candle(
        "2026-09-04", 151.6, when=stale_when, interval="OneMinute"
    )
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert result.passed is False
    assert any(
        "UPRO" in msg and "minutes old" in msg
        for msg in result.details["provisional_close_failures"]
    )


def test_gate_10_fails_closed_for_an_unrecognised_candle_interval() -> None:
    """An interval this gate does not recognise (typo, or a future
    granularity not yet accounted for) must fail closed rather than guess a
    bound -- consistent with the rest of gate 10's fail-closed posture."""
    rates, candles, provisional = _all_fresh_fixture()
    candles["UPRO"] = _candle(
        "2026-09-04", 151.6, when=PROBE_NOW, interval="TwoWeeks"
    )
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert result.passed is False
    assert any(
        "UPRO" in msg and "unrecognised" in msg
        for msg in result.details["provisional_close_failures"]
    )


def test_gate_10_fails_closed_for_a_missing_candle_interval() -> None:
    """A Candle built without an interval at all (``None``, the dataclass
    default) is exactly as unknown to this gate as a typo'd string -- must
    also fail closed."""
    rates, candles, provisional = _all_fresh_fixture()
    fresh = candles["UPRO"]
    candles["UPRO"] = Candle(
        date=fresh.date,
        open=fresh.open,
        high=fresh.high,
        low=fresh.low,
        close=fresh.close,
        timestamp=fresh.timestamp,
        interval=None,
    )
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert result.passed is False
    assert any(
        "UPRO" in msg and "unrecognised" in msg
        for msg in result.details["provisional_close_failures"]
    )


# ---------------------------------------------------------------------------
# Item 3: live rate present and non-zero for every managed symbol
# ---------------------------------------------------------------------------


def test_gate_10_fails_when_a_managed_symbol_has_no_live_rate() -> None:
    rates, candles, provisional = _all_fresh_fixture()
    del rates["SCHG"]
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert result.passed is False
    assert any("SCHG" in msg and "no live rate" in msg for msg in result.details["sanity_failures"])


def test_gate_10_fails_when_a_live_rate_is_zero() -> None:
    rates, candles, provisional = _all_fresh_fixture()
    rates["SPY"] = Rate(instrument_id=3000, bid=0.0, ask=0.0, last_execution=0.0, timestamp=None)
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert result.passed is False
    assert any("SPY" in msg for msg in result.details["sanity_failures"])


def test_gate_10_includes_spy_in_every_check_even_though_it_is_never_bought() -> None:
    """DESIGN_market_data.md §6: excluding SPY "because we don't trade it"
    would leave cone_5_z itself unguarded -- confirm a SPY-only staleness
    still fails the gate."""
    rates, candles, provisional = _all_fresh_fixture()
    rates["SPY"] = Rate(
        instrument_id=3000,
        bid=769.62,
        ask=769.66,
        last_execution=769.62,
        timestamp="2026-09-03T14:00:00.000000Z",  # >24h before PROBE_NOW
    )
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    assert result.passed is False
    assert "SPY" in result.details["stale_symbols"]


# ---------------------------------------------------------------------------
# ProvisionalClose structural safety
# ---------------------------------------------------------------------------


def test_provisional_close_refuses_etoro_rate_as_a_source() -> None:
    with pytest.raises(ValueError, match="etoro_rate"):
        ProvisionalClose(symbol="UPRO", source="etoro_rate", value=153.67)


# ---------------------------------------------------------------------------
# Telegram wiring
# ---------------------------------------------------------------------------


class _RecordingNotifier:
    """A minimal double matching TelegramNotifier.preflight_result's
    signature, so this test does not need a real Repo/httpx stack."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def preflight_result(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def test_notify_gate_10_failure_calls_preflight_result_naming_the_gate() -> None:
    rates, candles, provisional = _upro_stale_fixture()
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    notifier = _RecordingNotifier()
    notify_gate_10_failure(notifier, result, trading_date="2026-09-04", session_id=42)  # type: ignore[arg-type]
    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert call["gate"] == 10
    assert call["passed"] is False
    assert "UPRO" in call["reason"]
    assert call["session_id"] == 42


def test_notify_gate_10_failure_refuses_a_passing_result() -> None:
    rates, candles, provisional = _all_fresh_fixture()
    result = gate_10_price_data(
        now=PROBE_NOW,
        managed_symbols=SYMBOLS,
        provisional_closes=provisional,
        rates=rates,
        candles=candles,
        config=_config(),
    )
    notifier = _RecordingNotifier()
    with pytest.raises(ValueError):
        notify_gate_10_failure(notifier, result, trading_date="2026-09-04")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Gate 11 -- reconciliation (DESIGN §1.4 fix #3, §5; DESIGN_market_data.md
# §4.5's "inverted gate 11"). engine/reconcile.py carries the actual
# detection logic and its own dedicated tests (tests/test_reconcile.py);
# these confirm gate 11 wraps it correctly, fails closed, and is wired the
# same way as gate 10 (a pure function over already-fetched data, plus a
# matching notify_* helper).
# ---------------------------------------------------------------------------

MANAGED_SYMBOLS = ("SCHG", "UPRO")
INSTRUMENT_SYMBOLS = {3244: "UPRO", 1084: "SCHG", 3000: "SPY"}


def _gate11_position(position_id: str, instrument_id: int) -> Position:
    return Position(
        position_id=position_id,
        instrument_id=instrument_id,
        units=1.0,
        entry_price=50.0,
        amount=50.0,
        leverage=1,
        is_buy=True,
        open_date="2026-08-21T10:00:00+00:00",
        raw={},
    )


def _gate11_annotation(position_id: str, symbol: str, *, is_open: bool = True) -> LotAnnotation:
    return LotAnnotation(
        position_id=position_id,
        symbol=symbol,
        book="catapult",
        entry_date="2026-08-21",
        entry_amount=50.0,
        origin="app",
        is_open=is_open,
    )


def test_gate_11_passes_when_broker_and_sidecar_fully_agree() -> None:
    positions = [_gate11_position("pos-upro-1", 3244)]
    annotations = [_gate11_annotation("pos-upro-1", "UPRO")]
    result = gate_11_reconciliation(
        positions,
        annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert isinstance(result, GateResult)
    assert result.gate == 11
    assert result.passed is True
    assert result.details["discrepancies"] == []


def test_gate_11_fails_closed_on_an_unmatched_broker_position() -> None:
    """The one non-negotiable behaviour required by the task: any
    non-empty discrepancy list fails this gate loudly."""
    positions = [_gate11_position("pos-mystery", 3244)]
    result = gate_11_reconciliation(
        positions,
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert result.passed is False
    assert result.gate == 11
    assert "pos-mystery" in result.reason
    assert len(result.details["discrepancies"]) == 1
    assert result.details["discrepancies"][0]["kind"] == "unmatched_broker_position"


def test_gate_11_fails_closed_on_an_unmatched_open_lot() -> None:
    annotations = [_gate11_annotation("pos-vanished", "UPRO")]
    result = gate_11_reconciliation(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert result.passed is False
    assert "pos-vanished" in result.reason
    assert result.details["discrepancies"][0]["kind"] == "unmatched_open_lot"


def test_gate_11_ignores_non_managed_symbol_positions() -> None:
    """A position outside the managed set is display-only and must never
    fail this gate (DESIGN.md §1.4, §6.D) -- even with zero sidecar rows."""
    positions = [_gate11_position("pos-other", 6365)]
    result = gate_11_reconciliation(
        positions,
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols={**INSTRUMENT_SYMBOLS, 6365: "IAU"},
    )
    assert result.passed is True


def test_notify_gate_11_failure_calls_preflight_result_naming_the_gate() -> None:
    positions = [_gate11_position("pos-mystery", 3244)]
    result = gate_11_reconciliation(
        positions,
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    notifier = _RecordingNotifier()
    notify_gate_11_failure(notifier, result, trading_date="2026-09-06", session_id=7)  # type: ignore[arg-type]
    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert call["gate"] == 11
    assert call["passed"] is False
    assert "pos-mystery" in call["reason"]
    assert call["session_id"] == 7


def test_notify_gate_11_failure_refuses_a_passing_result() -> None:
    positions = [_gate11_position("pos-upro-1", 3244)]
    annotations = [_gate11_annotation("pos-upro-1", "UPRO")]
    result = gate_11_reconciliation(
        positions,
        annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    notifier = _RecordingNotifier()
    with pytest.raises(ValueError):
        notify_gate_11_failure(notifier, result, trading_date="2026-09-06")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Gate 11's auto-adopt path (DESIGN_session.md §3.4, the operator's
# 2026-09-06 "no forced human-in-the-loop" decision). The matching/adoption
# logic itself is engine/reconcile.py's job and is exhaustively tested in
# tests/test_reconcile.py -- these confirm gate 11 wires it in correctly:
# opt-in via abandoned_intents, byte-identical behaviour when it is omitted,
# and adoptions surfaced in details without ever flipping `passed` to False.
# ---------------------------------------------------------------------------


def _gate11_abandoned_intent(
    *,
    request_id: str = "req-1",
    instrument_id: int = 1084,  # SCHG
    symbol: str = "SCHG",
    amount_usd: float = 50.0,
    book: str = "catapult",
) -> Intent:
    return Intent(
        session_id=1,
        request_id=request_id,
        sequence=1,
        symbol=symbol,
        side="buy",
        instrument_id=instrument_id,
        book=book,
        amount_usd=amount_usd,
        status="abandoned",
    )


def test_gate_11_default_omits_abandoned_intents_and_behaves_exactly_as_before() -> None:
    """The load-bearing backward-compatibility test: every call site that
    predates this feature (including every other test in this file) does
    not pass ``abandoned_intents`` at all -- confirm that path is completely
    unaffected, still failing closed on a bare unmatched position."""
    positions = [_gate11_position("pos-mystery", 3244)]
    result = gate_11_reconciliation(
        positions,
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert result.passed is False
    assert result.details["adoptions"] == []


def test_gate_11_adopts_a_position_matching_an_abandoned_intent() -> None:
    position = _gate11_position("pos-late-fill", 1084)  # SCHG
    intent = _gate11_abandoned_intent()
    result = gate_11_reconciliation(
        [position],
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
        abandoned_intents=[intent],
    )
    assert result.passed is True
    assert result.details["discrepancies"] == []
    assert len(result.details["adoptions"]) == 1
    adoption = result.details["adoptions"][0]
    assert adoption["position_id"] == "pos-late-fill"
    assert adoption["entry_context_source"] == "adopted_from_abandoned_intent"
    assert adoption["intent_request_id"] == "req-1"
    assert "auto-adopted" in result.reason


def test_gate_11_bare_unmatched_position_still_fails_with_unrelated_abandoned_intents() -> None:
    """THE non-negotiable behaviour: an unmatched broker position with no
    genuine provenance must still fail this gate, even when the
    auto-adopt path is engaged for other positions in the same call."""
    positions = [
        _gate11_position("pos-late-fill", 1084),
        _gate11_position("pos-mystery", 3244),  # UPRO, no matching intent
    ]
    intent = _gate11_abandoned_intent()
    result = gate_11_reconciliation(
        positions,
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
        abandoned_intents=[intent],
    )
    assert result.passed is False
    assert len(result.details["adoptions"]) == 1
    assert result.details["adoptions"][0]["position_id"] == "pos-late-fill"
    assert len(result.details["discrepancies"]) == 1
    assert result.details["discrepancies"][0]["position_id"] == "pos-mystery"


def test_gate_11_respects_reconciliation_config_size_tolerance() -> None:
    """A tighter configured tolerance than the position's actual deviation
    must still fail this gate closed -- the config value is not decorative."""
    position = _gate11_position("pos-slippage", 1084)
    object.__setattr__(position, "amount", 52.0)  # 4% over the intent's $50
    intent = _gate11_abandoned_intent(amount_usd=50.0)
    result = gate_11_reconciliation(
        [position],
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
        abandoned_intents=[intent],
        reconciliation_config=ReconciliationConfig(abandoned_intent_size_tolerance_pct=0.02),
    )
    assert result.passed is False
    assert result.details["adoptions"] == []

    # The same mismatch, but with a wide-enough configured tolerance, adopts.
    result_wide = gate_11_reconciliation(
        [position],
        annotations=[],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
        abandoned_intents=[intent],
        reconciliation_config=ReconciliationConfig(abandoned_intent_size_tolerance_pct=0.10),
    )
    assert result_wide.passed is True
    assert len(result_wide.details["adoptions"]) == 1


# ---------------------------------------------------------------------------
# Gate 11's auto-RESOLVE path -- the mirror-image gap (DESIGN_session.md
# §3.4). Same wiring contract as the adopt path above: opt-in via the same
# ``abandoned_intents``, byte-identical behaviour when it is omitted, and
# resolutions surfaced in details without ever flipping ``passed`` to False.
# The matching logic is engine/reconcile.py's job and is exhaustively
# tested in tests/test_reconcile.py.
# ---------------------------------------------------------------------------


def _gate11_abandoned_close_intent(
    *,
    request_id: str = "req-close-1",
    symbol: str = "SCHG",
    instrument_id: int = 1084,  # SCHG
    book: str = "catapult",
    position_id: str = "pos-vanished",
) -> Intent:
    return Intent(
        session_id=1,
        request_id=request_id,
        sequence=1,
        symbol=symbol,
        side="close",
        instrument_id=instrument_id,
        book=book,
        position_id=position_id,
        status="abandoned",
    )


def test_gate_11_resolves_a_vanished_position_matching_an_abandoned_close_intent() -> None:
    annotations = [_gate11_annotation("pos-vanished", "SCHG")]
    intent = _gate11_abandoned_close_intent()
    result = gate_11_reconciliation(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
        abandoned_intents=[intent],
    )
    assert result.passed is True
    assert result.details["discrepancies"] == []
    assert len(result.details["resolutions"]) == 1
    resolution = result.details["resolutions"][0]
    assert resolution["position_id"] == "pos-vanished"
    assert resolution["exit_context_source"] == "resolved_from_abandoned_intent"
    assert resolution["intent_request_id"] == "req-close-1"
    assert "auto-resolved" in result.reason


def test_gate_11_vanished_position_still_fails_with_no_matching_abandoned_close_intent() -> None:
    """THE non-negotiable behaviour: a position that vanished for any
    reason OTHER than one of our own abandoned close orders (broker-side
    liquidation, or a hand-close in the eToro app) has no matching intent
    and must still fail this gate -- this is the entire safety argument for
    the auto-resolve path."""
    annotations = [_gate11_annotation("pos-vanished", "SCHG")]
    result = gate_11_reconciliation(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
        abandoned_intents=[],
    )
    assert result.passed is False
    assert result.details["resolutions"] == []
    assert result.details["discrepancies"][0]["kind"] == "unmatched_open_lot"


def test_gate_11_default_omits_abandoned_intents_and_close_resolution_is_also_a_no_op() -> None:
    """Same backward-compatibility guarantee as the adopt path: every call
    site that predates this feature does not pass ``abandoned_intents`` at
    all -- confirm the close-side path is completely unaffected too."""
    annotations = [_gate11_annotation("pos-vanished", "SCHG")]
    result = gate_11_reconciliation(
        positions=[],
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert result.passed is False
    assert result.details["resolutions"] == []


def test_gate_11_both_adopt_and_resolve_paths_can_fire_in_one_call() -> None:
    """The buy-side adopt and close-side resolve paths are independent and
    both draw from the same ``abandoned_intents`` -- confirm they can both
    fire together without interfering with each other."""
    positions = [_gate11_position("pos-late-fill", 1084)]  # SCHG buy fill
    annotations = [_gate11_annotation("pos-vanished", "SCHG")]  # SCHG vanished close
    buy_intent = _gate11_abandoned_intent(request_id="req-buy-1")
    close_intent = _gate11_abandoned_close_intent(request_id="req-close-1")
    result = gate_11_reconciliation(
        positions,
        annotations=annotations,
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
        abandoned_intents=[buy_intent, close_intent],
    )
    assert result.passed is True
    assert len(result.details["adoptions"]) == 1
    assert result.details["adoptions"][0]["position_id"] == "pos-late-fill"
    assert len(result.details["resolutions"]) == 1
    assert result.details["resolutions"][0]["position_id"] == "pos-vanished"


# ===========================================================================
# Broker-side take-profit detection (DESIGN.md §6.A, 2026-09-11) --
# `find_broker_take_profits`, the detection half only. Deliberately not a
# gate: it is gate 12's input once gate 12 exists (see the TODO above the
# function), and must never be folded into fail-closed gate 11, where the
# live book's 229-of-230 TP-carrying positions would halt every session.
#
# Fixtures are REAL rows from
# `data/probe_2026-09-11/adopt_fetch/adopt_portfolio_raw.json`, parsed
# through the real parser rather than hand-built Position objects, so the
# test exercises the parse and the detection together.
# ===========================================================================

_LIVE_TP_ROW: dict[str, object] = {
    "positionID": 2867253750, "openDateTime": "2024-11-27T14:30:41.173Z",
    "openRate": 27.65, "instrumentID": 1084, "isBuy": True,
    "takeProfitRate": 304.15, "stopLossRate": 0.0001, "amount": 200.0,
    "leverage": 1, "units": 7.233273, "isNoTakeProfit": False,
    "isNoStopLoss": True,
}

_LIVE_NO_TP_ROW: dict[str, object] = {
    "positionID": 3550406905, "openDateTime": "2026-08-19T13:30:09.647Z",
    "openRate": 35.37, "instrumentID": 1084, "isBuy": True,
    "takeProfitRate": 0.0, "stopLossRate": 0.0001, "amount": 500.0,
    "leverage": 1, "units": 14.136273, "isNoTakeProfit": True,
    "isNoStopLoss": True,
}


def test_find_broker_take_profits_detects_the_live_11x_take_profit() -> None:
    position = EtoroClient._parse_position(_LIVE_TP_ROW)
    found = find_broker_take_profits([position])
    assert len(found) == 1
    assert found[0].position_id == "2867253750"
    assert found[0].instrument_id == 1084
    assert found[0].take_profit_rate == pytest.approx(304.15)
    assert found[0].multiple == pytest.approx(11.0, abs=1e-9)


def test_find_broker_take_profits_does_not_flag_the_suppressed_position() -> None:
    """positionID 3550406905 is the one live position with
    isNoTakeProfit: true. Its takeProfitRate is 0.0 -- present and falsy --
    so a rate-only check would be ambiguous; the flag is authoritative."""
    position = EtoroClient._parse_position(_LIVE_NO_TP_ROW)
    assert find_broker_take_profits([position]) == []


def test_find_broker_take_profits_ignores_a_payload_missing_the_fields() -> None:
    """"Not stated" is neither "suppressed" nor "detected" -- a payload with
    no take-profit keys must not crash and must not produce a finding."""
    position = _gate11_position("pos-no-tp-keys", 1084)
    assert position.take_profit_rate is None
    assert find_broker_take_profits([position]) == []


def test_find_broker_take_profits_reports_mixed_books_in_input_order() -> None:
    positions = [
        EtoroClient._parse_position(_LIVE_NO_TP_ROW),
        EtoroClient._parse_position(_LIVE_TP_ROW),
        _gate11_position("pos-no-tp-keys", 1084),
    ]
    found = find_broker_take_profits(positions)
    assert [f.position_id for f in found] == ["2867253750"]


def test_find_broker_take_profits_is_detection_only_and_never_fails_gate_11() -> None:
    """Explicit guard on the scoping decision: a TP-carrying position that
    reconciles fine must still pass gate 11. If this ever flips, the live
    book (229 of 230 positions carrying a TP) would halt every session."""
    position = EtoroClient._parse_position(_LIVE_TP_ROW)
    annotation = _gate11_annotation("2867253750", "SCHG")
    result = gate_11_reconciliation(
        [position],
        annotations=[annotation],
        managed_symbols=MANAGED_SYMBOLS,
        instrument_symbols=INSTRUMENT_SYMBOLS,
    )
    assert result.passed is True
    assert find_broker_take_profits([position]) != []
