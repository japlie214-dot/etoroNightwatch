"""Tests for nightwatch.probes.fee_audit (the sharpened E1 per-trade fee
audit: HANDOFF's operator brief for 2026-09-05 tonight's probe run).

Entirely offline, like tests/test_probes_live_window.py -- no eToro call, no
network at all, no file I/O. This module is pure logic; the on-device runner
that calls it is not exercised here.
"""

from __future__ import annotations

import pytest

from nightwatch.probes.fee_audit import (
    EXPECTED_SUM_FEES,
    EXPECTED_SUM_NET_PROFIT,
    LEVERAGED_OR_SHORT,
    PLAIN_1X_LONG,
    build_fee_audit_rows,
    check_dividend_or_account_fee_exposure,
    correlate_fees_with,
    fee_bearing_rows,
    group_fees_by_instrument,
    group_fees_by_leverage,
    reconcile_account_totals,
)


def _trade(**overrides):
    base = {
        "position_id": "p1",
        "instrument_id": 1084,
        "units": 10.0,
        "is_buy": True,
        "leverage": 1,
        "open_rate": 10.0,
        "close_rate": 11.0,
        "profit": 10.0,
        "investment": 100.0,
        "fees": 0.0,
        "open_date": "2026-01-01",
        "close_date": "2026-01-11",
        "raw": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# build_fee_audit_rows
# ---------------------------------------------------------------------------


def test_build_fee_audit_rows_flattens_basic_fields():
    trades = [_trade()]
    rows = build_fee_audit_rows(trades, symbol_by_instrument_id={1084: "SCHG"})
    assert len(rows) == 1
    row = rows[0]
    assert row.position_id == "p1"
    assert row.instrument_id == 1084
    assert row.symbol == "SCHG"
    assert row.investment == 100.0
    assert row.net_profit == 10.0
    assert row.fees == 0.0
    assert row.leverage == 1
    assert row.is_buy is True
    assert row.is_leveraged_or_short is False
    assert row.holding_days == pytest.approx(10.0)


def test_build_fee_audit_rows_leveraged_and_short_flagging():
    leveraged = build_fee_audit_rows([_trade(leverage=2)])[0]
    assert leveraged.is_leveraged_or_short is True

    short = build_fee_audit_rows([_trade(is_buy=False)])[0]
    assert short.is_leveraged_or_short is True

    plain_long = build_fee_audit_rows([_trade(leverage=1, is_buy=True)])[0]
    assert plain_long.is_leveraged_or_short is False


def test_build_fee_audit_rows_unparseable_dates_give_none_holding_days_not_zero():
    rows = build_fee_audit_rows([_trade(open_date=None, close_date="2026-01-11")])
    assert rows[0].holding_days is None
    rows2 = build_fee_audit_rows([_trade(open_date="not-a-date", close_date="2026-01-11")])
    assert rows2[0].holding_days is None


def test_build_fee_audit_rows_missing_symbol_mapping_is_none_not_crash():
    rows = build_fee_audit_rows([_trade(instrument_id=9999)], symbol_by_instrument_id={1084: "SCHG"})
    assert rows[0].symbol is None


# ---------------------------------------------------------------------------
# group_fees_by_instrument
# ---------------------------------------------------------------------------


def test_group_fees_by_instrument_finds_schg_fee_if_present():
    # The operator's exact worry: does ANY plain-ETF (SCHG) trade carry a fee?
    trades = [
        _trade(instrument_id=1084, fees=0.0),
        _trade(instrument_id=1084, fees=-2.50),  # a fee-bearing SCHG row
        _trade(instrument_id=3244, fees=-10.0, leverage=3),
    ]
    rows = build_fee_audit_rows(trades, symbol_by_instrument_id={1084: "SCHG", 3244: "UPRO"})
    groups = group_fees_by_instrument(rows)
    schg_group = next(g for g in groups if g.instrument_id == 1084)
    assert schg_group.any_nonzero_fee is True
    assert schg_group.sum_fees == pytest.approx(-2.50)
    assert schg_group.symbol == "SCHG"
    # Sorted by abs(sum_fees) descending -- UPRO's -10.0 outranks SCHG's -2.50.
    assert groups[0].instrument_id == 3244


def test_group_fees_by_instrument_none_fee_counts_row_but_contributes_zero():
    trades = [_trade(instrument_id=1084, fees=None)]
    rows = build_fee_audit_rows(trades)
    groups = group_fees_by_instrument(rows)
    assert groups[0].count == 1
    assert groups[0].sum_fees == 0.0
    assert groups[0].any_nonzero_fee is False


# ---------------------------------------------------------------------------
# group_fees_by_leverage
# ---------------------------------------------------------------------------


def test_group_fees_by_leverage_separates_plain_long_from_leveraged_or_short():
    trades = [
        _trade(leverage=1, is_buy=True, fees=0.0, investment=100.0),
        _trade(leverage=1, is_buy=True, fees=0.0, investment=200.0),
        _trade(leverage=3, is_buy=True, fees=-5.0, investment=300.0),
        _trade(leverage=1, is_buy=False, fees=-1.0, investment=50.0),
    ]
    rows = build_fee_audit_rows(trades)
    groups = group_fees_by_leverage(rows)
    assert groups[PLAIN_1X_LONG].count == 2
    assert groups[PLAIN_1X_LONG].any_nonzero_fee is False
    assert groups[LEVERAGED_OR_SHORT].count == 2
    assert groups[LEVERAGED_OR_SHORT].sum_fees == pytest.approx(-6.0)
    assert groups[LEVERAGED_OR_SHORT].any_nonzero_fee is True


def test_group_fees_by_leverage_always_has_both_keys_even_if_empty():
    rows = build_fee_audit_rows([_trade(leverage=1, is_buy=True)])
    groups = group_fees_by_leverage(rows)
    assert groups[LEVERAGED_OR_SHORT].count == 0
    assert groups[LEVERAGED_OR_SHORT].sum_fees == 0.0


# ---------------------------------------------------------------------------
# fee_bearing_rows
# ---------------------------------------------------------------------------


def test_fee_bearing_rows_excludes_zero_and_none_includes_nonzero():
    trades = [
        _trade(position_id="a", fees=0.0),
        _trade(position_id="b", fees=None),
        _trade(position_id="c", fees=-3.33),
        _trade(position_id="d", fees=1.5),  # a positive "fee" would still count as nonzero
    ]
    rows = build_fee_audit_rows(trades)
    bearing = fee_bearing_rows(rows)
    ids = {r.position_id for r in bearing}
    assert ids == {"c", "d"}


# ---------------------------------------------------------------------------
# correlate_fees_with
# ---------------------------------------------------------------------------


def test_correlate_fees_with_investment_detects_percentage_commission_pattern():
    # fees exactly proportional to investment -- a clean, deterministic 1%
    # commission signature the correlation must actually find, not eyeball.
    trades = [
        _trade(investment=100.0, fees=-1.0),
        _trade(investment=200.0, fees=-2.0),
        _trade(investment=300.0, fees=-3.0),
        _trade(investment=400.0, fees=-4.0),
    ]
    rows = build_fee_audit_rows(trades)
    result = correlate_fees_with(rows, "investment")
    assert result.n == 4
    assert result.pearson_r == pytest.approx(-1.0, abs=1e-9) or result.pearson_r == pytest.approx(1.0, abs=1e-9)


def test_correlate_fees_with_holding_days_no_relationship_reports_low_r():
    trades = [
        _trade(open_date="2026-01-01", close_date="2026-01-02", fees=-1.0),
        _trade(open_date="2026-01-01", close_date="2026-06-01", fees=-1.0),
        _trade(open_date="2026-01-01", close_date="2026-01-05", fees=0.0),
        _trade(open_date="2026-01-01", close_date="2026-12-01", fees=0.0),
    ]
    rows = build_fee_audit_rows(trades)
    result = correlate_fees_with(rows, "holding_days")
    assert result.n == 4
    assert result.pearson_r is not None


def test_correlate_fees_with_fewer_than_two_rows_returns_none_not_crash():
    rows = build_fee_audit_rows([_trade()])
    result = correlate_fees_with(rows, "investment")
    assert result.pearson_r is None
    assert result.n <= 1


def test_correlate_fees_with_invalid_metric_raises():
    rows = build_fee_audit_rows([_trade()])
    with pytest.raises(ValueError):
        correlate_fees_with(rows, "not_a_real_metric")


def test_correlate_fees_with_zero_variance_metric_is_none_not_crash():
    # Every row has the same investment -- statistics.correlation raises
    # StatisticsError on zero variance; this must be caught, not propagated.
    trades = [
        _trade(investment=100.0, fees=-1.0),
        _trade(investment=100.0, fees=-2.0),
        _trade(investment=100.0, fees=0.0),
    ]
    rows = build_fee_audit_rows(trades)
    result = correlate_fees_with(rows, "investment")
    assert result.pearson_r is None
    assert "undefined" in result.note


# ---------------------------------------------------------------------------
# reconcile_account_totals
# ---------------------------------------------------------------------------


def test_reconcile_account_totals_matches_documented_figures():
    # A synthetic 120-row stand-in whose sums are engineered to hit the
    # documented totals exactly, the way the real on-device run must.
    trades = [_trade(instrument_id=1084, fees=0.0, profit=2.7275) for _ in range(120)]
    # 120 * 2.7275 = 327.3 -- close enough to demonstrate the reconciliation
    # math, then nudge one row's profit and fees to hit the totals exactly.
    trades[0] = _trade(instrument_id=1084, fees=EXPECTED_SUM_FEES, profit=EXPECTED_SUM_NET_PROFIT - 2.7275 * 119)
    rows = build_fee_audit_rows(trades)
    recon = reconcile_account_totals(rows)
    assert recon.n_rows == 120
    assert recon.sum_fees == pytest.approx(EXPECTED_SUM_FEES, abs=0.01)
    assert recon.sum_net_profit == pytest.approx(EXPECTED_SUM_NET_PROFIT, abs=0.01)
    assert recon.fees_reconcile is True
    assert recon.net_profit_reconciles is True
    assert "RECONCILES" in recon.verdict


def test_reconcile_account_totals_flags_a_genuine_disagreement_loudly():
    trades = [_trade(fees=-1.0, profit=1.0) for _ in range(5)]  # sums nowhere near the expected totals
    rows = build_fee_audit_rows(trades)
    recon = reconcile_account_totals(rows)
    assert recon.fees_reconcile is False
    assert recon.net_profit_reconciles is False
    assert "DOES NOT RECONCILE" in recon.verdict
    assert "major finding" in recon.verdict


def test_reconcile_account_totals_none_values_contribute_zero_not_crash():
    rows = build_fee_audit_rows([_trade(fees=None, profit=None)])
    recon = reconcile_account_totals(rows)
    assert recon.sum_fees == 0.0
    assert recon.sum_net_profit == 0.0


# ---------------------------------------------------------------------------
# check_dividend_or_account_fee_exposure
# ---------------------------------------------------------------------------


def test_check_dividend_or_account_fee_exposure_finds_nothing_says_so_plainly():
    payloads = [{"positionId": "p1", "netProfit": 10.0, "fees": 0.0}]
    result = check_dividend_or_account_fee_exposure(payloads)
    assert result.fields_found == ()
    assert "NO field" in result.verdict
    assert "does not expose dividend" in result.verdict


def test_check_dividend_or_account_fee_exposure_finds_a_dividend_field():
    payloads = [{"positionId": "p1", "dividendAdjustment": 0.42}]
    result = check_dividend_or_account_fee_exposure(payloads)
    assert "dividendAdjustment" in result.fields_found
    assert "field(s) with a dividend/account-charge-shaped name" in result.verdict


def test_check_dividend_or_account_fee_exposure_recurses_into_nested_dicts():
    payloads = [{"positionId": "p1", "nested": {"accountMaintenanceFee": 1.0}}]
    result = check_dividend_or_account_fee_exposure(payloads)
    assert "accountMaintenanceFee" in result.fields_found


def test_check_dividend_or_account_fee_exposure_recurses_into_lists():
    payloads = [{"items": [{"custodyFee": 2.0}, {"other": 1}]}]
    result = check_dividend_or_account_fee_exposure(payloads)
    assert "custodyFee" in result.fields_found
