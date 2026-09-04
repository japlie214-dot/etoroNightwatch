"""Typed models for eToro API requests/responses (DESIGN §1.1, §4.3).

Plain, framework-free dataclasses so they import unmodified on both desktop
CPython and Chaquopy with no extra dependency (mirrors the constraint already
carried by :mod:`nightwatch.db.repo`'s row types).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Final

__all__ = [
    "OrderStatus",
    "TERMINAL_ORDER_STATUSES",
    "FILLED_ORDER_STATUSES",
    "Candle",
    "Rate",
    "Eligibility",
    "EligibilityBatchResult",
    "Position",
    "Balance",
    "compute_available_cash",
    "OrderRequest",
    "CloseRequest",
    "OrderResult",
    "TradeHistoryEntry",
    "InstrumentSearchResult",
]


class OrderStatus(IntEnum):
    """eToro order status codes (DESIGN §1.1).

    ``HTTP 200 means accepted, not filled`` — every order must be polled via
    ``orders:lookup`` to one of the terminal statuses below. Status 11
    (WaitingForMarket) is explicitly "queued, not filled": it is included in
    :data:`TERMINAL_ORDER_STATUSES` reasoning as a status that *can* stop
    changing for a long time, but it must never be treated as a fill — see
    :data:`FILLED_ORDER_STATUSES`, which it is deliberately absent from.
    """

    RECEIVED = 1
    PLACED = 2
    FILLED = 3
    REJECTED = 4
    PARTIALLY_FILLED = 5
    PENDING_CANCEL = 6
    CANCELED = 7
    EXPIRED = 8
    CANCELED_PARTIALLY_FILLED = 9
    REJECTED_PARTIALLY_FILLED = 10
    WAITING_FOR_MARKET = 11


#: Statuses that end a poll-to-terminal loop: eToro will not move the order
#: further without new input. WAITING_FOR_MARKET is intentionally excluded —
#: it is a queued state that can still resolve to a fill or a rejection, so a
#: poller must keep waiting (bounded by its own timeout), never book it as
#: done.
TERMINAL_ORDER_STATUSES: Final[frozenset[OrderStatus]] = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.CANCELED_PARTIALLY_FILLED,
        OrderStatus.REJECTED_PARTIALLY_FILLED,
    }
)

#: Statuses that represent an actual fill (full or partial). WAITING_FOR_MARKET
#: is not a fill under any circumstance — DESIGN §6.H is explicit that it must
#: never be booked as a success.
FILLED_ORDER_STATUSES: Final[frozenset[OrderStatus]] = frozenset(
    {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}
)


@dataclass(frozen=True, slots=True)
class Candle:
    """One ``OneDay`` (or other interval) bar from ``history/candles``."""

    date: str  # 'YYYY-MM-DD'
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


@dataclass(frozen=True, slots=True)
class Rate:
    """A live quote from ``market-data/instruments/rates``.

    eToro's rates endpoint carries **no market-open flag** (DESIGN §1.1) —
    market-open comes from our own calendar, never from this model.
    """

    instrument_id: int
    bid: float
    ask: float
    last_execution: float | None
    timestamp: str | None = None  # ISO-8601, when the API supplies one


@dataclass(frozen=True, slots=True)
class Eligibility:
    """One instrument's result from ``POST /trading/info/eligibility``."""

    instrument_id: int
    allow_open_position: bool
    min_position_exposure: float | None
    min_position_amount: float | None
    allow_partial_close: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EligibilityBatchResult:
    """The batch response for ``POST /trading/info/eligibility`` (≤100 ids)."""

    results: dict[int, Eligibility]

    def __getitem__(self, instrument_id: int) -> Eligibility:
        return self.results[instrument_id]

    def get(self, instrument_id: int) -> Eligibility | None:
        return self.results.get(instrument_id)


@dataclass(frozen=True, slots=True)
class Position:
    """One open broker position, as returned by ``trading/info/portfolio``."""

    position_id: str
    instrument_id: int
    symbol: str | None
    units: float
    entry_price: float
    current_price: float | None
    invested: float | None
    leverage: int
    is_buy: bool
    unrealized_pnl: float | None = None
    open_date: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Balance:
    """``GET /balances`` — account-level credit and pending-order amounts.

    ``available_cash`` MUST be computed with eToro's documented formula,
    never ``total_balance`` (that is portfolio value, not cash): see
    :func:`compute_available_cash` and DESIGN §1.1/§4.5/§9:

        available = credit − (Σ ordersForOpen[i].amount where mirrorID == 0
                               + Σ orders[i].amount)
    """

    credit: float
    total_balance: float | None
    available_cash: float
    raw: dict[str, Any] = field(default_factory=dict)


def compute_available_cash(
    credit: float,
    orders_for_open: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> float:
    """Compute broker available cash from eToro's documented formula.

    ``available = credit - (sum of ordersForOpen[i].amount where
    mirrorID == 0) - (sum of orders[i].amount)`` (DESIGN §1.1, §4.5, §9).

    This is deliberately a free function, not just a method on
    :class:`Balance`, so :mod:`nightwatch.etoro.client` can compute it
    straight off the raw JSON body before constructing the dataclass, and so
    it is trivially unit-testable against a hand-built sample payload.
    """
    pending_for_open = sum(
        float(order.get("amount", 0.0))
        for order in (orders_for_open or [])
        if int(order.get("mirrorID", 0)) == 0
    )
    pending_orders = sum(float(order.get("amount", 0.0)) for order in (orders or []))
    return float(credit) - pending_for_open - pending_orders


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """An open-order request body (DESIGN §4.3, §6.A).

    The three safety-critical fields are fixed defaults, not caller-settable
    parameters with a default — see the module-level constants in
    :mod:`nightwatch.etoro.client` (``MAX_LEVERAGE``, ``TRANSACTION``,
    ``SETTLEMENT_TYPE``) for the structural enforcement. This dataclass
    itself still *validates* on construction as defense in depth: constructing
    one with a forbidden value raises immediately, before any HTTP call.
    """

    instrument_id: int
    amount: float
    leverage: int = 1
    transaction: str = "buy"
    settlement_type: str = "real"
    order_type: str = "mkt"
    order_currency: str = "usd"
    action: str = "open"

    def __post_init__(self) -> None:
        if self.leverage != 1:
            raise ValueError(
                f"OrderRequest.leverage must be exactly 1, got {self.leverage!r} "
                "(DESIGN §6.A: never margin, structurally)"
            )
        if self.transaction != "buy":
            raise ValueError(
                f"OrderRequest.transaction must be 'buy', got {self.transaction!r} "
                "(DESIGN §6.A: never short, structurally — there is no sellShort "
                "or buyToCover code path)"
            )
        if self.settlement_type != "real":
            raise ValueError(
                f"OrderRequest.settlement_type must be 'real', got "
                f"{self.settlement_type!r} (DESIGN §6.A: never cfd/marginTrade)"
            )
        if self.action != "open":
            raise ValueError(
                f"OrderRequest.action must be 'open', got {self.action!r} "
                "(there is no SELL order — reducing a holding closes a position)"
            )
        if self.amount <= 0:
            raise ValueError("OrderRequest.amount must be positive")

    def to_json(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "transaction": self.transaction,
            "instrumentId": self.instrument_id,
            "orderType": self.order_type,
            "leverage": self.leverage,
            "amount": self.amount,
            "orderCurrency": self.order_currency,
            "settlementType": self.settlement_type,
        }


@dataclass(frozen=True, slots=True)
class CloseRequest:
    """A full-close request body (DESIGN §4.3).

    ``units_to_deduct`` is always ``None`` — a full close is the only close
    type this app performs; partial-close support is explicitly out of scope.
    """

    position_id: str
    instrument_id: int
    units_to_deduct: float | None = None

    def __post_init__(self) -> None:
        if self.units_to_deduct is not None:
            raise ValueError(
                "CloseRequest.units_to_deduct must be None: this app only "
                "ever performs full closes (partial close is out of scope)"
            )

    def to_json(self) -> dict[str, Any]:
        return {"InstrumentId": self.instrument_id, "UnitsToDeduct": self.units_to_deduct}


@dataclass(frozen=True, slots=True)
class OrderResult:
    """The result of one poll of ``orders:lookup`` for a single order."""

    order_id: str | None
    reference_id: str | None
    status: OrderStatus
    position_id: str | None = None
    fill_price: float | None = None
    fill_units: float | None = None
    fill_amount_usd: float | None = None
    reject_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_ORDER_STATUSES

    @property
    def is_filled(self) -> bool:
        """True only for an actual fill. WAITING_FOR_MARKET is never True here."""
        return self.status in FILLED_ORDER_STATUSES


@dataclass(frozen=True, slots=True)
class TradeHistoryEntry:
    """One row of ``GET /trading/info/trade/history``."""

    position_id: str | None
    instrument_id: int
    symbol: str | None
    action: str
    units: float
    open_rate: float | None
    close_rate: float | None
    profit: float | None
    open_date: str | None
    close_date: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InstrumentSearchResult:
    """One hit from ``market-data/instruments/search``."""

    instrument_id: int
    symbol: str
    name: str | None = None
    exchange: str | None = None
    currency: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
