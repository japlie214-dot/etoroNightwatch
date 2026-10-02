"""Tests for nightwatch.engine.order_budget (the latency-derived time gate
that replaces DESIGN.md §6.C's arbitrary `max_orders_per_session` integer
cap, per DESIGN_session.md §4.8/§4.9).

This module is deliberately SIMPLE SCAFFOLDING (see order_budget.py's own
module docstring) with no live production caller yet -- these tests exercise
it exactly as `tests/test_preflight.py` exercises gates 10/11 before
`run_preflight()` exists: pure functions, hand-built inputs, no broker, no
`engine/session.py` orchestration required.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from nightwatch.engine.order_budget import (
    INTENT_COUNT_BUG_CEILING_MULTIPLE,
    PLACEHOLDER_P95_LATENCY_SECONDS,
    ImplausibleIntentCountError,
    assert_plausible_intent_count,
    check_order_budget,
    compute_hard_deadline,
)
from nightwatch.market.calendar import EASTERN
from nightwatch.probes.order_latency import T30_BUDGET_SECONDS

UTC = timezone.utc


# ===========================================================================
# check_order_budget -- the core "now + p95 < hard_deadline" mechanism
# ===========================================================================


def test_order_fits_comfortably_inside_the_deadline():
    now = datetime(2026, 9, 11, 15, 0, 0, tzinfo=UTC)
    deadline = now + timedelta(minutes=10)
    decision = check_order_budget(now=now, hard_deadline=deadline, p95_latency_seconds=5.0)
    assert decision.allowed is True
    assert decision.projected_finish == now + timedelta(seconds=5.0)
    assert "may proceed" in decision.reason
    assert decision.used_placeholder_latency is False


def test_order_does_not_fit_when_projected_finish_is_past_the_deadline():
    now = datetime(2026, 9, 11, 15, 0, 0, tzinfo=UTC)
    deadline = now + timedelta(seconds=10)
    decision = check_order_budget(now=now, hard_deadline=deadline, p95_latency_seconds=30.0)
    assert decision.allowed is False
    assert "STOP placing further orders" in decision.reason
    assert "accept the remaining shortfall" in decision.reason


def test_boundary_is_strictly_less_than_never_equal():
    # The task's own mechanism, literally: "now + p95 < hard_deadline" -- an
    # exact tie is NOT a fit. A gate that allowed the boundary case would be
    # off by exactly the amount that matters most: the order that JUST
    # barely doesn't fit.
    now = datetime(2026, 9, 11, 15, 0, 0, tzinfo=UTC)
    deadline = now + timedelta(seconds=30.0)
    decision = check_order_budget(now=now, hard_deadline=deadline, p95_latency_seconds=30.0)
    assert decision.allowed is False


def test_zero_latency_still_fits_when_now_is_before_the_deadline():
    now = datetime(2026, 9, 11, 15, 0, 0, tzinfo=UTC)
    deadline = now + timedelta(seconds=1)
    decision = check_order_budget(now=now, hard_deadline=deadline, p95_latency_seconds=0.0)
    assert decision.allowed is True


def test_negative_latency_raises_rather_than_silently_treating_as_instant():
    now = datetime(2026, 9, 11, 15, 0, 0, tzinfo=UTC)
    deadline = now + timedelta(minutes=5)
    with pytest.raises(ValueError, match="p95_latency_seconds must be >= 0"):
        check_order_budget(now=now, hard_deadline=deadline, p95_latency_seconds=-1.0)


def test_already_past_the_deadline_never_allows_regardless_of_latency():
    now = datetime(2026, 9, 11, 16, 5, 0, tzinfo=UTC)
    deadline = datetime(2026, 9, 11, 16, 0, 0, tzinfo=UTC)
    decision = check_order_budget(now=now, hard_deadline=deadline, p95_latency_seconds=0.0)
    assert decision.allowed is False


def test_required_arguments_have_no_defaults_and_are_keyword_only():
    # board nightwatch/handoff-2026-09-06.md §1's own fix for the §3a ATH floor's "silently
    # inert" defect: a caller missing a required input must get a TypeError,
    # never a quietly-wrong default. Same discipline applied here.
    now = datetime(2026, 9, 11, 15, 0, 0, tzinfo=UTC)
    deadline = now + timedelta(minutes=5)
    with pytest.raises(TypeError):
        check_order_budget(now=now, hard_deadline=deadline)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        check_order_budget(now, deadline, 5.0)  # type: ignore[misc]


# ===========================================================================
# The placeholder latency -- loud, not silent
# ===========================================================================


def test_placeholder_constant_is_conservative_and_obviously_named():
    # "Obviously a placeholder and greppable": the name itself says so, and
    # its value is grossly larger than any real broker round-trip
    # DESIGN_session.md §4.8/RUNBOOK §8(b) contemplate (single-digit-to-low-
    # tens of seconds).
    assert PLACEHOLDER_P95_LATENCY_SECONDS >= 120.0
    assert "PLACEHOLDER" in "PLACEHOLDER_P95_LATENCY_SECONDS"


def test_placeholder_is_unambiguously_greater_than_the_t30_budget():
    # The whole point of the placeholder: it must fail the gate for the
    # ENTIRE T-30 window, not merely "almost immediately" (which a placeholder
    # smaller than T30_BUDGET_SECONDS would only do for the window's final
    # slice, permitting every order before that -- fail-open for most of the
    # session). This test fails loudly if anyone ever sets the placeholder
    # back down to, or below, T30_BUDGET_SECONDS.
    assert PLACEHOLDER_P95_LATENCY_SECONDS > T30_BUDGET_SECONDS


def test_placeholder_permits_zero_orders_across_the_entire_t30_window():
    # Even at the single most favorable instant inside the T-30 window --
    # right at its start, the earliest an order could ever be attempted,
    # which produces the smallest possible overrun -- the placeholder must
    # still refuse. If it refuses there, it refuses everywhere later in the
    # window too (the overrun only grows as `now` advances toward
    # `hard_deadline`), so this single check proves zero orders are ever
    # permitted for the whole session while the placeholder is in place.
    hard_deadline = datetime(2026, 9, 11, 15, 30, 0, tzinfo=UTC)
    window_start = hard_deadline - timedelta(seconds=T30_BUDGET_SECONDS)

    decision = check_order_budget(
        now=window_start,
        hard_deadline=hard_deadline,
        p95_latency_seconds=PLACEHOLDER_P95_LATENCY_SECONDS,
    )
    assert decision.allowed is False

    # Sanity-check across the rest of the window too, not just the boundary.
    for offset_seconds in (0, 300, 900, 1500, 1799):
        now = window_start + timedelta(seconds=offset_seconds)
        decision = check_order_budget(
            now=now,
            hard_deadline=hard_deadline,
            p95_latency_seconds=PLACEHOLDER_P95_LATENCY_SECONDS,
        )
        assert decision.allowed is False, (
            f"placeholder permitted an order at offset {offset_seconds}s "
            "into the T-30 window -- it must permit none"
        )


def test_using_the_placeholder_is_flagged_in_the_decision():
    now = datetime(2026, 9, 11, 15, 0, 0, tzinfo=UTC)
    deadline = now + timedelta(hours=1)
    decision = check_order_budget(
        now=now, hard_deadline=deadline, p95_latency_seconds=PLACEHOLDER_P95_LATENCY_SECONDS
    )
    assert decision.used_placeholder_latency is True


def test_using_the_placeholder_logs_an_error_not_silence(caplog: pytest.LogCaptureFixture):
    now = datetime(2026, 9, 11, 15, 0, 0, tzinfo=UTC)
    deadline = now + timedelta(hours=1)
    with caplog.at_level("ERROR", logger="nightwatch.engine.order_budget"):
        check_order_budget(
            now=now, hard_deadline=deadline, p95_latency_seconds=PLACEHOLDER_P95_LATENCY_SECONDS
        )
    assert any("PLACEHOLDER" in record.message for record in caplog.records)


def test_a_real_measured_latency_does_not_trip_the_placeholder_warning(
    caplog: pytest.LogCaptureFixture,
):
    now = datetime(2026, 9, 11, 15, 0, 0, tzinfo=UTC)
    deadline = now + timedelta(hours=1)
    with caplog.at_level("ERROR", logger="nightwatch.engine.order_budget"):
        decision = check_order_budget(
            now=now, hard_deadline=deadline, p95_latency_seconds=7.3
        )
    assert decision.used_placeholder_latency is False
    assert not any("PLACEHOLDER" in record.message for record in caplog.records)


# ===========================================================================
# compute_hard_deadline -- derived from the market calendar, never hardcoded
# ===========================================================================


def test_hard_deadline_is_close_minus_margin_on_a_regular_close_day():
    # 2026-09-11 (Friday) is an ordinary NYSE trading day: 16:00 ET close.
    trading_date = date(2026, 9, 11)
    deadline = compute_hard_deadline(trading_date, margin_minutes=5.0)
    expected_close = datetime(2026, 9, 11, 16, 0, 0, tzinfo=EASTERN)
    expected = expected_close - timedelta(minutes=5.0)
    assert deadline == expected
    assert deadline == datetime(2026, 9, 11, 15, 55, 0, tzinfo=EASTERN)


def test_hard_deadline_honors_an_early_close_day():
    # 2026-11-27 is the day after Thanksgiving -- a documented 13:00 ET
    # NYSE early close (market/calendar.py's nyse_early_closes).
    trading_date = date(2026, 11, 27)
    deadline = compute_hard_deadline(trading_date, margin_minutes=0.0)
    expected_close = datetime(2026, 11, 27, 13, 0, 0, tzinfo=EASTERN)
    assert deadline == expected_close


def test_zero_margin_is_exactly_close():
    trading_date = date(2026, 9, 11)
    deadline = compute_hard_deadline(trading_date, margin_minutes=0.0)
    expected_close = datetime(2026, 9, 11, 16, 0, 0, tzinfo=EASTERN)
    assert deadline == expected_close


def test_regular_close_order_window_is_non_empty():
    deadline = compute_hard_deadline(date(2026, 9, 11), margin_minutes=5.0)
    close = datetime(2026, 9, 11, 16, 0, 0, tzinfo=EASTERN)
    session_start = close - timedelta(seconds=T30_BUDGET_SECONDS)
    assert deadline - session_start == timedelta(minutes=25)


def test_early_close_deadline_is_strictly_after_session_start():
    deadline = compute_hard_deadline(date(2026, 11, 27), margin_minutes=5.0)
    assert deadline == datetime(2026, 11, 27, 12, 55, 0, tzinfo=EASTERN)
    assert deadline > datetime(2026, 11, 27, 12, 30, 0, tzinfo=EASTERN)


def test_margin_that_leaves_no_order_window_is_rejected():
    with pytest.raises(ValueError) as exc_info:
        compute_hard_deadline(date(2026, 9, 11), margin_minutes=30)
    assert str(exc_info.value) == (
        "margin_minutes must be < 30 so the order window is non-empty, got 30"
    )


def test_margin_just_below_the_whole_window_is_allowed():
    deadline = compute_hard_deadline(date(2026, 9, 11), margin_minutes=29.9)
    close = datetime(2026, 9, 11, 16, 0, 0, tzinfo=EASTERN)
    assert deadline == close - timedelta(minutes=29.9)
    assert deadline > close - timedelta(seconds=T30_BUDGET_SECONDS)


def test_negative_margin_is_rejected():
    with pytest.raises(ValueError, match="margin_minutes must be >= 0"):
        compute_hard_deadline(date(2026, 9, 11), margin_minutes=-1.0)


def test_margin_minutes_has_no_default_and_is_keyword_only():
    with pytest.raises(TypeError):
        compute_hard_deadline(date(2026, 9, 11))  # type: ignore[call-arg]


def test_compute_hard_deadline_feeds_check_order_budget_end_to_end():
    # An order whose projected finish lands comfortably before close-minus-
    # margin is allowed; one that would land after it is not -- exercised
    # together the way a real caller (once one exists) would use both.
    trading_date = date(2026, 9, 11)
    deadline = compute_hard_deadline(trading_date, margin_minutes=5.0)
    assert deadline == datetime(2026, 9, 11, 15, 55, 0, tzinfo=EASTERN)
    comfortably_early = deadline - timedelta(minutes=10)
    decision = check_order_budget(
        now=comfortably_early, hard_deadline=deadline, p95_latency_seconds=10.0
    )
    assert decision.allowed is True

    just_before_deadline = deadline - timedelta(seconds=5)
    decision2 = check_order_budget(
        now=just_before_deadline, hard_deadline=deadline, p95_latency_seconds=10.0
    )
    assert decision2.allowed is False


def test_order_is_allowed_one_minute_after_session_start_with_measured_latency():
    deadline = compute_hard_deadline(date(2026, 9, 11), margin_minutes=5.0)
    now = datetime(2026, 9, 11, 15, 31, 0, tzinfo=EASTERN)
    decision = check_order_budget(
        now=now, hard_deadline=deadline, p95_latency_seconds=10.0
    )
    assert decision.allowed is True


def test_placeholder_still_refuses_one_minute_after_session_start():
    deadline = compute_hard_deadline(date(2026, 9, 11), margin_minutes=5.0)
    now = datetime(2026, 9, 11, 15, 31, 0, tzinfo=EASTERN)
    decision = check_order_budget(
        now=now,
        hard_deadline=deadline,
        p95_latency_seconds=PLACEHOLDER_P95_LATENCY_SECONDS,
    )
    assert PLACEHOLDER_P95_LATENCY_SECONDS == 3600.0
    assert decision.allowed is False
    assert decision.used_placeholder_latency is True


# ===========================================================================
# assert_plausible_intent_count -- bug-catcher, not max_orders_per_session
# ===========================================================================


def test_a_plausible_intent_count_does_not_raise():
    # DESIGN_session.md §4.9's own worst-case projection: ~10 lots touched
    # per sell as the book matures. Comfortably under any reasonable ceiling.
    assert_plausible_intent_count(10, max_lots=60)
    assert_plausible_intent_count(0, max_lots=60)


def test_an_absurd_intent_count_raises_the_dedicated_exception():
    with pytest.raises(ImplausibleIntentCountError):
        assert_plausible_intent_count(1000, max_lots=60)


def test_ceiling_is_derived_from_max_lots_times_the_documented_multiple():
    max_lots = 60
    ceiling = max_lots * INTENT_COUNT_BUG_CEILING_MULTIPLE
    assert_plausible_intent_count(ceiling, max_lots=max_lots)  # exactly at ceiling: OK
    with pytest.raises(ImplausibleIntentCountError):
        assert_plausible_intent_count(ceiling + 1, max_lots=max_lots)


def test_ceiling_tracks_a_different_max_lots_value_rather_than_a_frozen_default():
    # max_lots is required, not defaulted -- the ceiling must visibly track
    # whatever the live strategy config actually says, not a number baked
    # into this module at import time.
    with pytest.raises(ImplausibleIntentCountError):
        assert_plausible_intent_count(50, max_lots=5)  # ceiling = 25
    assert_plausible_intent_count(50, max_lots=20)  # ceiling = 100 -- fine


def test_negative_intent_count_is_rejected():
    with pytest.raises(ValueError, match="intent_count must be >= 0"):
        assert_plausible_intent_count(-1, max_lots=60)


def test_non_positive_max_lots_is_rejected():
    with pytest.raises(ValueError, match="max_lots must be > 0"):
        assert_plausible_intent_count(5, max_lots=0)


def test_max_lots_has_no_default_and_is_keyword_only():
    with pytest.raises(TypeError):
        assert_plausible_intent_count(5)  # type: ignore[call-arg]
