"""Tests for nightwatch.etoro.models (DESIGN §1.1, §4.3, §4.5)."""

from __future__ import annotations

import pytest

from nightwatch.etoro.models import (
    CloseRequest,
    FILLED_ORDER_STATUSES,
    MIN_POSITION_VALUE_USD,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
    TERMINAL_ORDER_STATUSES,
    compute_available_cash,
    partial_close_leaves_legal_remainder,
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
    assert compute_available_cash(500.0, orders_for_open=[], orders=[]) == pytest.approx(500.0)


def test_available_cash_rejects_missing_or_malformed_order_amounts() -> None:
    with pytest.raises(ValueError, match="amount"):
        compute_available_cash(500.0, orders_for_open=[], orders=[{}])
    with pytest.raises(ValueError, match="numeric"):
        compute_available_cash(500.0, orders_for_open=[], orders=[{"amount": True}])
    with pytest.raises(ValueError, match="mirrorID"):
        compute_available_cash(500.0, orders_for_open=[{"amount": 5.0}], orders=[])
    with pytest.raises(ValueError, match="mirrorID"):
        compute_available_cash(
            500.0, orders_for_open=[{"mirrorID": True, "amount": 5.0}], orders=[]
        )
    with pytest.raises(ValueError, match="mirrorID"):
        compute_available_cash(
            500.0, orders_for_open=[{"mirrorID": 0.5, "amount": 5.0}], orders=[]
        )
    with pytest.raises(ValueError, match="amount"):
        compute_available_cash(500.0, orders_for_open=[{"mirrorID": 1}], orders=[])
    with pytest.raises(ValueError, match="arrays"):
        compute_available_cash(500.0, orders_for_open=None, orders=[])
    with pytest.raises(ValueError, match="numeric"):
        compute_available_cash(True, orders_for_open=[], orders=[])


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
        (OrderStatus.CANCELED_PARTIALLY_FILLED, True),
        (OrderStatus.REJECTED_PARTIALLY_FILLED, True),
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


# -- CloseRequest: full close is still the default, byte-identical on the ---
# -- wire; partial close is now expressible (board nightwatch/handoff-2026-09-06.md §7) ------


def test_close_request_defaults_to_full_close() -> None:
    close = CloseRequest(position_id="p1", instrument_id=1)
    body = close.to_json()
    assert body["UnitsToDeduct"] is None
    assert body["InstrumentId"] == 1


def test_close_request_accepts_a_positive_units_to_deduct_for_a_partial_close() -> None:
    close = CloseRequest(position_id="p1", instrument_id=1, units_to_deduct=2.5)
    body = close.to_json()
    assert body["UnitsToDeduct"] == 2.5
    assert body["InstrumentId"] == 1


def test_close_request_rejects_non_positive_units_to_deduct() -> None:
    with pytest.raises(ValueError, match="units_to_deduct"):
        CloseRequest(position_id="p1", instrument_id=1, units_to_deduct=0.0)
    with pytest.raises(ValueError, match="units_to_deduct"):
        CloseRequest(position_id="p1", instrument_id=1, units_to_deduct=-5.0)


# -- partial_close_leaves_legal_remainder: the $10 stub floor ----------------
# (board nightwatch/handoff-2026-09-06.md §7 -- market value, never cost basis, and the
# worked example the design must reproduce.)


def test_min_position_value_usd_is_ten_dollars() -> None:
    assert MIN_POSITION_VALUE_USD == 10.0


def test_full_close_is_always_legal() -> None:
    assert partial_close_leaves_legal_remainder(50.0, 50.0) is True


def test_worked_example_close_the_50_whole_then_1_of_the_60_is_legal() -> None:
    # board nightwatch/handoff-2026-09-06.md §7: need $51 from positions worth $50/$50/$60 ->
    # close the $50 whole (remainder $0), then take $1 from the $60
    # (remainder $59) -- both steps legal.
    assert partial_close_leaves_legal_remainder(50.0, 50.0) is True
    assert partial_close_leaves_legal_remainder(60.0, 1.0) is True


def test_worked_example_taking_51_of_the_60_is_illegal() -> None:
    # Leaves a $9 stub -- below the $10 floor.
    assert partial_close_leaves_legal_remainder(60.0, 51.0) is False


def test_remainder_exactly_at_the_floor_is_legal() -> None:
    assert partial_close_leaves_legal_remainder(60.0, 50.0) is True  # $10 remainder


def test_remainder_just_under_the_floor_is_illegal() -> None:
    assert partial_close_leaves_legal_remainder(60.0, 50.01) is False  # $9.99 remainder


def test_min_position_value_is_overridable_for_the_unverified_cost_basis_case() -> None:
    # The floor itself is a parameter, not hardcoded, so a future correction
    # (board nightwatch/handoff-2026-09-06.md §7: unverified whether the broker gates on
    # market value or cost basis) does not require rewriting call sites.
    assert partial_close_leaves_legal_remainder(60.0, 51.0, min_position_value=5.0) is True


# -- broker-side exits on Position (DESIGN.md §6.A, 2026-09-11) --------------


def test_position_defaults_to_no_knowledge_of_a_broker_take_profit() -> None:
    """A Position constructed without the broker's take-profit/stop-loss keys
    (every pre-2026-09-11 call site, and any payload shape lacking them) must
    construct fine and report "nothing stated" -- None for the rates, False
    for the flags. False here means "the broker did not say there is no TP",
    NOT "there is no TP"; the rate being None is what says "unknown"."""
    position = Position(
        position_id="pos-1",
        instrument_id=1084,
        units=7.233273,
        entry_price=27.65,
        amount=200.0,
        leverage=1,
        is_buy=True,
    )
    assert position.take_profit_rate is None
    assert position.is_no_take_profit is False
    assert position.stop_loss_rate is None
    assert position.is_no_stop_loss is False


def test_position_carries_the_live_11x_take_profit_verbatim() -> None:
    """The live shape (adopt_portfolio_raw.json, positionID 2867253750):
    openRate 27.65 with takeProfitRate 304.15 == exactly 11x, the broker's
    declared defaultTakeProfitPercentage of +1000% applied to an order this
    app never asked to have one."""
    position = Position(
        position_id="2867253750",
        instrument_id=1084,
        units=7.233273,
        entry_price=27.65,
        amount=200.0,
        leverage=1,
        is_buy=True,
        take_profit_rate=304.15,
        is_no_take_profit=False,
        stop_loss_rate=0.0001,
        is_no_stop_loss=True,
    )
    assert position.take_profit_rate / position.entry_price == pytest.approx(11.0, abs=1e-9)
    assert position.is_no_stop_loss is True
