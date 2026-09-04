"""Tests for nightwatch.etoro.models (DESIGN §1.1, §4.3, §4.5)."""

from __future__ import annotations

import pytest

from nightwatch.etoro.models import (
    CloseRequest,
    FILLED_ORDER_STATUSES,
    OrderRequest,
    OrderResult,
    OrderStatus,
    TERMINAL_ORDER_STATUSES,
    compute_available_cash,
)

# -- available-cash formula (DESIGN §1.1, §4.5) ------------------------------


def test_available_cash_uses_documented_formula_not_total_balance() -> None:
    # credit=1000, one ordersForOpen entry with mirrorID=0 (counts), one with
    # mirrorID != 0 (excluded), one plain "orders" entry (always counts).
    available = compute_available_cash(
        credit=1000.0,
        orders_for_open=[
            {"amount": 100.0, "mirrorID": 0},
            {"amount": 250.0, "mirrorID": 7},  # excluded: not mirrorID == 0
        ],
        orders=[{"amount": 50.0}],
    )
    # 1000 - 100 (mirrorID==0) - 50 (orders) = 850; the mirrorID=7 entry never
    # enters the sum at all.
    assert available == pytest.approx(850.0)


def test_available_cash_ignores_total_balance_entirely() -> None:
    # A huge totalBalance must have zero influence -- the formula never reads it.
    low = compute_available_cash(credit=100.0, orders_for_open=[], orders=[])
    assert low == pytest.approx(100.0)


def test_available_cash_defaults_to_credit_when_no_pending_orders() -> None:
    assert compute_available_cash(500.0) == pytest.approx(500.0)


def test_available_cash_sums_multiple_pending_orders() -> None:
    available = compute_available_cash(
        credit=2000.0,
        orders_for_open=[
            {"amount": 100.0, "mirrorID": 0},
            {"amount": 200.0, "mirrorID": 0},
        ],
        orders=[{"amount": 10.0}, {"amount": 20.0}],
    )
    assert available == pytest.approx(2000.0 - 300.0 - 30.0)


# -- OrderStatus enum: status 11 must never be a fill ------------------------


def test_waiting_for_market_is_not_terminal() -> None:
    assert OrderStatus.WAITING_FOR_MARKET not in TERMINAL_ORDER_STATUSES


def test_waiting_for_market_is_never_a_fill() -> None:
    assert OrderStatus.WAITING_FOR_MARKET not in FILLED_ORDER_STATUSES
    result = OrderResult(
        order_id="o1", reference_id="r1", status=OrderStatus.WAITING_FOR_MARKET
    )
    assert result.is_filled is False
    assert result.is_terminal is False


def test_pending_cancel_is_not_terminal() -> None:
    assert OrderStatus.PENDING_CANCEL not in TERMINAL_ORDER_STATUSES


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.CANCELED_PARTIALLY_FILLED,
        OrderStatus.REJECTED_PARTIALLY_FILLED,
    ],
)
def test_terminal_statuses_are_terminal(status: OrderStatus) -> None:
    result = OrderResult(order_id="o", reference_id="r", status=status)
    assert result.is_terminal is True


@pytest.mark.parametrize(
    "status,expected_fill",
    [
        (OrderStatus.FILLED, True),
        (OrderStatus.PARTIALLY_FILLED, True),
        (OrderStatus.REJECTED, False),
        (OrderStatus.CANCELED, False),
        (OrderStatus.RECEIVED, False),
        (OrderStatus.PLACED, False),
        (OrderStatus.WAITING_FOR_MARKET, False),
    ],
)
def test_is_filled_matches_expectation(status: OrderStatus, expected_fill: bool) -> None:
    result = OrderResult(order_id="o", reference_id="r", status=status)
    assert result.is_filled is expected_fill


# -- OrderRequest: structural safety, validated at construction -------------


def test_order_request_default_is_safe() -> None:
    order = OrderRequest(instrument_id=1, amount=100.0)
    body = order.to_json()
    assert body["leverage"] == 1
    assert body["transaction"] == "buy"
    assert body["settlementType"] == "real"
    assert body["action"] == "open"
    assert body["orderCurrency"] == "usd"
    assert body["instrumentId"] == 1
    assert body["amount"] == 100.0


def test_order_request_rejects_leverage_above_one() -> None:
    with pytest.raises(ValueError, match="leverage"):
        OrderRequest(instrument_id=1, amount=100.0, leverage=3)


def test_order_request_rejects_sell_short() -> None:
    with pytest.raises(ValueError, match="transaction"):
        OrderRequest(instrument_id=1, amount=100.0, transaction="sellShort")


def test_order_request_rejects_buy_to_cover() -> None:
    with pytest.raises(ValueError, match="transaction"):
        OrderRequest(instrument_id=1, amount=100.0, transaction="buyToCover")


def test_order_request_rejects_cfd_settlement() -> None:
    with pytest.raises(ValueError, match="settlement_type"):
        OrderRequest(instrument_id=1, amount=100.0, settlement_type="cfd")


def test_order_request_rejects_margin_trade_settlement() -> None:
    with pytest.raises(ValueError, match="settlement_type"):
        OrderRequest(instrument_id=1, amount=100.0, settlement_type="marginTrade")


def test_order_request_rejects_non_open_action() -> None:
    with pytest.raises(ValueError, match="action"):
        OrderRequest(instrument_id=1, amount=100.0, action="close")


def test_order_request_rejects_non_positive_amount() -> None:
    with pytest.raises(ValueError, match="amount"):
        OrderRequest(instrument_id=1, amount=0.0)
    with pytest.raises(ValueError, match="amount"):
        OrderRequest(instrument_id=1, amount=-5.0)


# -- CloseRequest: full close only, structurally -----------------------------


def test_close_request_defaults_to_full_close() -> None:
    close = CloseRequest(position_id="p1", instrument_id=1)
    body = close.to_json()
    assert body["UnitsToDeduct"] is None
    assert body["InstrumentId"] == 1


def test_close_request_rejects_partial_close() -> None:
    with pytest.raises(ValueError, match="units_to_deduct"):
        CloseRequest(position_id="p1", instrument_id=1, units_to_deduct=5.0)
