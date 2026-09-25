"""Tests for nightwatch.probes.live_window (the 2026-09-11 live-probe's
off-device-testable logic: board nightwatch/handoff-2026-09-05.md's ask for unit tests on the
arithmetic, key-set diff, and trade partitioning).

Entirely offline, like tests/test_market_adopt.py -- no eToro call, no
network at all. This module is pure logic; the probe script that calls it
(tools/probe_live_2026-09-11.py) is not imported here and is not exercised by
this suite (it requires Chaquopy/live credentials -- see the probe's own
docstring and doc/RUNBOOK_LIVE_WINDOW_2026-09-11.md).
"""

from __future__ import annotations

import pytest

from nightwatch.probes.live_window import (
    FeeReconciliation,
    KeyDiff,
    PARSER_READ_KEYS,
    PARSER_REQUIRED_KEY_GROUPS,
    RequiredKeyDiff,
    diff_keys,
    diff_required_keys,
    partition_trades_by_instrument,
    reconcile_fee_hypothesis,
    verify_sleeve_arithmetic,
)


# ---------------------------------------------------------------------------
# diff_keys
# ---------------------------------------------------------------------------


def test_diff_keys_clean_payload_has_no_missing_keys():
    # PARSER_READ_KEYS lists every casing alternative a fallback chain
    # tries (get_rates reads instrumentID *or* instrumentId *or*
    # InstrumentID, only one of which any real payload actually has) --
    # so a payload is "clean" only relative to a parser with no such
    # alternates. _parse_order_result's registry entry is one flat set with
    # no casing forks, so handing diff_keys every key in it is a genuine
    # "this payload has everything the parser reads" case.
    result = diff_keys(PARSER_READ_KEYS["_parse_order_result"], "_parse_order_result")
    assert result.is_clean
    assert result.missing_from_payload == frozenset()


def test_diff_keys_get_rates_registry_lists_casing_alternatives_not_all_required():
    # Documents the registry's actual shape: get_rates's real live payload
    # only ever has ONE casing of instrumentID/bid/ask, so a real clean
    # payload will still show the other alternatives as "missing" -- that is
    # expected, informational noise from the fallback chain, not a defect.
    # This test exists so a future edit that "fixes" is_clean to tolerate
    # alternates does not silently change behaviour without a test noticing.
    live_shaped_payload = {"rates", "instrumentID", "bid", "ask", "lastExecution", "date"}
    result = diff_keys(live_shaped_payload, "get_rates")
    assert not result.is_clean
    # The untried casing alternatives, not a genuinely absent field.
    assert result.missing_from_payload == {"instrumentId", "InstrumentID", "Bid", "Ask", "timestamp", "dateTime"}


def test_diff_keys_flags_missing_field_the_defect_class():
    # The exact failure mode HANDOFF_2026-09-05 §4/§8 describes: a parser
    # reads a plausible key that a real payload simply does not have.
    payload_keys = {"instrumentID", "bid", "ask"}  # no lastExecution, no date
    result = diff_keys(payload_keys, "get_rates")
    assert "lastExecution" in result.missing_from_payload
    assert "date" in result.missing_from_payload
    assert not result.is_clean


def test_diff_keys_reports_unused_payload_keys():
    payload_keys = {"instrumentID", "bid", "ask", "lastExecution", "date", "spread", "exoticFieldNoOneReads"}
    result = diff_keys(payload_keys, "get_rates")
    assert "spread" in result.unused_in_payload
    assert "exoticFieldNoOneReads" in result.unused_in_payload
    # A key the parser DOES read must never show up as unused.
    assert "bid" not in result.unused_in_payload


def test_diff_keys_unknown_parser_raises():
    with pytest.raises(KeyError):
        diff_keys({"a", "b"}, "not_a_real_parser")


def test_diff_keys_get_instruments_and_get_costs_have_no_field_level_keys():
    # get_instruments does no field extraction at all (client.py: returns the
    # raw rows verbatim under the "instrumentDisplayDatas" envelope key) --
    # every row-level key should show as "unused", which is the honest,
    # correct report, not a bug in the registry. The envelope key itself is
    # the only thing the parser reads, so include it to get a clean diff.
    result = diff_keys(
        {"instrumentDisplayDatas", "instrumentId", "symbolFull", "exchangeId"},
        "get_instruments",
    )
    assert result.parser_keys == frozenset({"instrumentDisplayDatas"})
    assert result.missing_from_payload == frozenset()
    assert result.unused_in_payload == {"instrumentId", "symbolFull", "exchangeId"}

    # get_costs parses NOTHING -- not even an envelope key -- so every key in
    # a real payload is "unused" by definition, and nothing can ever be
    # "missing".
    result2 = diff_keys({"totalFeeAmount", "totalFeePercent"}, "get_costs")
    assert result2.parser_keys == frozenset()
    assert result2.missing_from_payload == frozenset()
    assert result2.unused_in_payload == {"totalFeeAmount", "totalFeePercent"}


# ---------------------------------------------------------------------------
# diff_required_keys (section F: fixes diff_keys's false-positive rate)
# ---------------------------------------------------------------------------


def test_diff_required_keys_fromdate_only_candle_row_is_clean():
    # The exact false-positive diff_keys produced against a live payload:
    # _parse_candle's raw_date fallback chain is `fromDate or date or
    # timestamp` -- a real row satisfying only `fromDate` (eToro's actual
    # live shape) must NOT be reported as missing `date`/`timestamp`.
    row_keys = {"fromDate", "open", "high", "low", "close", "volume", "instrumentID"}
    result = diff_required_keys(row_keys, "_parse_candle")
    assert isinstance(result, RequiredKeyDiff)
    assert result.is_clean
    assert result.missing_groups == ()
    # instrumentID is not part of _parse_candle's own required groups or its
    # optional set -- it is genuinely unused by this parser.
    assert "instrumentID" in result.unused_in_payload
    # volume is optional, not "unused" noise to flag.
    assert "volume" not in result.unused_in_payload


def test_diff_required_keys_flags_a_genuinely_missing_field():
    # None of {fromDate, date, timestamp} present at all -- a real gap.
    row_keys = {"open", "high", "low", "close"}
    result = diff_required_keys(row_keys, "_parse_candle")
    assert not result.is_clean
    assert frozenset({"fromDate", "date", "timestamp"}) in result.missing_groups


def test_diff_required_keys_get_rates_single_casing_is_clean():
    # A real get_rates row only ever has ONE casing of each field
    # (instrumentID/bid/ask observed live) -- diff_required_keys must credit
    # that as satisfied, unlike diff_keys which reports the other casings as
    # "missing" noise.
    row_keys = {"instrumentID", "bid", "ask", "lastExecution", "date"}
    result = diff_required_keys(row_keys, "get_rates")
    assert result.is_clean
    satisfied_keys = {k for _, k in result.satisfied_groups}
    assert satisfied_keys == {"instrumentID", "bid", "ask"}


def test_diff_required_keys_unknown_parser_raises():
    with pytest.raises(KeyError):
        diff_required_keys({"a", "b"}, "not_a_real_parser")


def test_diff_required_keys_get_eod_history_and_iex_no_fallback_chain():
    # get_eod_history/get_iex_quotes read plain, single-cased keys -- no
    # or-chain -- so each field is its own one-member group.
    result = diff_required_keys(
        {"date", "close", "adjClose", "splitFactor", "divCash"}, "get_eod_history"
    )
    assert result.is_clean
    assert result.unused_in_payload == frozenset()

    result2 = diff_required_keys({"ticker", "tngoLast"}, "get_iex_quotes")
    assert result2.is_clean

    result3 = diff_required_keys({"ticker"}, "get_iex_quotes")
    assert not result3.is_clean
    assert frozenset({"tngoLast"}) in result3.missing_groups


def test_parser_required_key_groups_registry_shape():
    # Every group is a non-empty frozenset of alternative key names.
    for parser_name, groups in PARSER_REQUIRED_KEY_GROUPS.items():
        assert groups, f"{parser_name} has no required groups registered"
        for group in groups:
            assert isinstance(group, frozenset) and group


def test_parser_read_keys_registry_covers_all_six_unverified_parsers():
    # board nightwatch/handoff-2026-09-05.md §6/§8: the six parsers this probe must audit.
    expected = {
        "_parse_candle",
        "get_rates",
        "get_eligibility",
        "_parse_order_result",
        "get_instruments",
        "get_costs",
    }
    assert expected == set(PARSER_READ_KEYS.keys())


# ---------------------------------------------------------------------------
# partition_trades_by_instrument
# ---------------------------------------------------------------------------


def test_partition_trades_by_instrument_sums_fees_per_instrument():
    trades = [
        {"instrument_id": 1084, "fees": 0.0, "investment": 100.0},  # SCHG
        {"instrument_id": 1084, "fees": 0.0, "investment": 250.0},  # SCHG
        {"instrument_id": 3244, "fees": -3.20, "investment": 500.0},  # UPRO
        {"instrument_id": 3244, "fees": -1.10, "investment": 200.0},  # UPRO
        {"instrument_id": 9999, "fees": -5.00, "investment": 50.0},  # crypto/other
    ]
    result = partition_trades_by_instrument(trades)
    assert result[1084].count == 2
    assert result[1084].sum_fees == 0.0
    assert result[1084].any_nonzero_fee is False
    assert result[3244].count == 2
    assert result[3244].sum_fees == pytest.approx(-4.30)
    assert result[3244].any_nonzero_fee is True
    assert result[9999].sum_fees == pytest.approx(-5.00)


def test_partition_trades_by_instrument_handles_none_fees_without_dropping_row():
    trades = [{"instrument_id": 1084, "fees": None, "investment": None}]
    result = partition_trades_by_instrument(trades)
    assert result[1084].count == 1
    assert result[1084].sum_fees == 0.0
    assert result[1084].sum_investment == 0.0
    assert result[1084].any_nonzero_fee is False


def test_partition_trades_by_instrument_all_120_real_rows_shape():
    # Mirrors board nightwatch/handoff-2026-09-05.md §5's real finding: sigma fees = -$380.40
    # across 120 rows. A synthetic stand-in of the same shape (not the real
    # values, which are not reproduced here) -- one instrument carrying all
    # the fee burden, another carrying none, checks the partition actually
    # discriminates rather than pooling everything.
    trades = [{"instrument_id": 1084, "fees": 0.0, "investment": 100.0} for _ in range(100)]
    trades += [{"instrument_id": 3244, "fees": -19.02, "investment": 300.0} for _ in range(20)]
    result = partition_trades_by_instrument(trades)
    assert result[1084].sum_fees == 0.0
    assert result[3244].sum_fees == pytest.approx(-380.40)
    assert result[1084].count + result[3244].count == 120


# ---------------------------------------------------------------------------
# verify_sleeve_arithmetic
# ---------------------------------------------------------------------------


def test_verify_sleeve_arithmetic_no_mismatch_within_tolerance():
    documented = {"schg_value": 44867.0, "upro_value": 4047.0, "sleeve_equity": 50179.0}
    measured = {"schg_value": 44900.0, "upro_value": 4050.0, "sleeve_equity": 50210.0}
    mismatches = verify_sleeve_arithmetic(measured, documented, tolerance_pct=0.02)
    assert mismatches == []


def test_verify_sleeve_arithmetic_flags_a_real_disagreement():
    # DESIGN_market_data.md §10.11/§10.12's own failure mode: a wildly
    # different measured figure must not be swallowed.
    documented = {"schg_value": 44867.0}
    measured = {"schg_value": 2020.0}  # the retracted "~$10 each x 202" estimate
    mismatches = verify_sleeve_arithmetic(measured, documented)
    assert len(mismatches) == 1
    assert mismatches[0].quantity == "schg_value"
    assert mismatches[0].pct_diff > 0.9


def test_verify_sleeve_arithmetic_zero_documented_value_uses_epsilon():
    documented = {"cash_reserve_usd": 0.0}
    measured_ok = {"cash_reserve_usd": 0.0}
    measured_bad = {"cash_reserve_usd": 500.0}
    assert verify_sleeve_arithmetic(measured_ok, documented) == []
    mismatches = verify_sleeve_arithmetic(measured_bad, documented)
    assert len(mismatches) == 1
    assert mismatches[0].pct_diff is None


def test_verify_sleeve_arithmetic_ignores_keys_not_in_both():
    documented = {"schg_value": 44867.0, "only_documented": 1.0}
    measured = {"schg_value": 44870.0, "only_measured": 2.0}
    assert verify_sleeve_arithmetic(measured, documented) == []


# ---------------------------------------------------------------------------
# reconcile_fee_hypothesis
# ---------------------------------------------------------------------------


def test_reconcile_fee_hypothesis_operator_scenario_fee_hidden_in_amount():
    # The operator's exact report: $100 order, fee disclosed, but the
    # resulting position amount looks like it already netted the fee out,
    # and cash moved by exactly the order amount.
    result = reconcile_fee_hypothesis(
        cash_before=1000.0,
        cash_after=900.0,
        order_amount_usd=100.0,
        disclosed_fee=0.35,
        position_amount=99.65,
    )
    assert result.cash_delta == pytest.approx(100.0)
    assert result.cash_delta_matches_order_amount is True
    assert result.position_amount_matches_order_amount is False
    assert "carved out of the position" in result.verdict


def test_reconcile_fee_hypothesis_fee_charged_separately_against_cash():
    result = reconcile_fee_hypothesis(
        cash_before=1000.0,
        cash_after=899.65,  # 100 order + 0.35 fee both left cash
        order_amount_usd=100.0,
        disclosed_fee=0.35,
        position_amount=100.0,
    )
    assert result.cash_delta == pytest.approx(100.35)
    assert result.cash_delta_matches_order_amount is False
    assert "charged separately against cash" in result.verdict


def test_reconcile_fee_hypothesis_no_fee_clean_etf_trade():
    result = reconcile_fee_hypothesis(
        cash_before=1000.0,
        cash_after=900.0,
        order_amount_usd=100.0,
        disclosed_fee=0.0,
        position_amount=100.0,
    )
    assert result.cash_delta_matches_order_amount is True
    assert result.position_amount_matches_order_amount is True
    assert "genuinely fee-free" in result.verdict


def test_reconcile_fee_hypothesis_no_fee_but_unexplained_cash_movement():
    result = reconcile_fee_hypothesis(
        cash_before=1000.0,
        cash_after=850.0,  # far more than the order amount left cash
        order_amount_usd=100.0,
        disclosed_fee=None,
        position_amount=100.0,
    )
    assert result.cash_delta_matches_order_amount is False
    assert "does not explain" in result.verdict


def test_reconcile_fee_hypothesis_unknown_position_amount_is_none_not_crash():
    result = reconcile_fee_hypothesis(
        cash_before=1000.0,
        cash_after=900.0,
        order_amount_usd=100.0,
        disclosed_fee=None,
        position_amount=None,
    )
    assert result.position_amount_matches_order_amount is None
    assert isinstance(result, FeeReconciliation)
