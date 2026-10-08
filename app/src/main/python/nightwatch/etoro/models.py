"""Typed models for eToro API requests/responses (DESIGN §1.1, §4.3).

Plain, framework-free dataclasses so they import unmodified on both desktop
CPython and Chaquopy with no extra dependency (mirrors the constraint already
carried by :mod:`nightwatch.db.repo`'s row types).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Final, Mapping, Sequence

__all__ = [
    "OrderStatus",
    "TERMINAL_ORDER_STATUSES",
    "FILLED_ORDER_STATUSES",
    "Candle",
    "Rate",
    "Eligibility",
    "EligibilityBatchResult",
    "Position",
    "PositionMark",
    "mark_position",
    "Balance",
    "compute_available_cash",
    "OrderRequest",
    "CloseRequest",
    "MIN_POSITION_VALUE_USD",
    "partial_close_leaves_legal_remainder",
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
    {
        OrderStatus.FILLED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCELED_PARTIALLY_FILLED,
        OrderStatus.REJECTED_PARTIALLY_FILLED,
    }
)


@dataclass(frozen=True, slots=True)
class Candle:
    """One ``OneDay`` (or other interval) bar from ``history/candles``.

    ``timestamp`` added 2026-09-05. The parser previously truncated the
    broker's date field to ``[:10]`` for every interval, which is harmless
    for ``OneDay`` and destructive for everything below it: a ``OneMinute``
    bar's own bar time was discarded, leaving only ``YYYY-MM-DD``.

    That truncation silently removed the mechanism gate 10 is built on.
    DESIGN_market_data.md §6/§2.11 make the candles path the freshness
    authority precisely *because* each bar is self-timestamped at bar
    granularity -- a stronger signal than a single date field, and the thing
    that distinguishes a current quote from the frozen 19-hour-old one §2.9
    reproduced. Truncating to a date left a check that cannot tell a bar
    minted this minute from one minted at the open.

    ``date`` keeps its ``YYYY-MM-DD`` meaning so existing callers
    (``market/bars.py``) are unaffected; ``timestamp`` carries the broker's
    full value verbatim and is ``None`` only when the payload has no date
    field at all.

    ``interval`` added 2026-09-05, same day and for the same underlying
    reason as ``timestamp``: gate 10's freshness check (``engine/preflight.py``
    ``_check_candle_freshness``) cannot honestly apply one flat age bound to
    every bar. A ``OneMinute`` bar ~1 minute old is normal; a ``OneDay`` bar
    that is legitimately hours old at T-30 is *also* normal (DESIGN_market_
    data.md §2.11). Without knowing which kind of bar it was handed, the gate
    cannot choose a matching bound and cannot even tell the two cases apart.
    ``get_candles`` already receives ``interval`` as a call parameter (it
    picks the URL path segment), so the value is known for free at parse
    time -- ``_parse_candle`` just has to keep it instead of discarding it,
    the same fix ``timestamp`` was. ``None`` only when a caller builds a
    ``Candle`` without specifying one (e.g. an older test fixture); gate 10
    treats an unknown interval as it treats anything else it cannot verify:
    fails closed rather than guessing a bound.
    """

    date: str  # 'YYYY-MM-DD'
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    timestamp: str | None = None
    interval: str | None = None


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
    """One open broker position, as returned by ``trading/info/portfolio``.

    Bug found 2026-09-04 against a live, non-empty (227-position) portfolio,
    corrected 2026-09-05 (DESIGN_market_data.md §4.2, §4.5, §10.7;
    board nightwatch/handoff-2026-09-04-market-data.md §5; board nightwatch/handoff-2026-09-05.md §1, §4): a
    broker position carries no ``symbol`` string, no current/mark
    price, and no unrealized P&L. This dataclass only carries fields the
    broker genuinely returns:

    * **No ``symbol`` field.** There is no symbol string anywhere on a
      broker position. Resolve one, when a caller needs it for display or
      logging, by joining ``instrument_id`` through the ``instruments`` table
      (``Repo.get_instrument``) -- never by string comparison (that was the
      old, wrong, "symbol match" gate 11 language DESIGN_market_data.md §4.5
      corrects). ``_parse_position`` is a ``@staticmethod`` on the client
      with no ``Repo`` access, so it cannot resolve a symbol itself, and
      should not fake one.
    * **No ``current_price``/``unrealized_pnl`` fields.** Both are genuinely
      absent keys on the broker payload, not sometimes-null. Mark-to-market
      must be computed from a price fetched elsewhere in the same session
      (DESIGN_market_data.md §3, §4.2) against ``amount`` as cost basis --
      see :func:`mark_position` / :class:`PositionMark` below, which take
      that price as an explicit argument rather than fetching one themselves.
    * **``amount`` is a first-class field**, not derived. It is the
      split-invariant re-link key (§4.4/§4.5): a live probe proved eToro
      rescales both ``units`` and ``openRate`` on a split while preserving
      ``amount`` exactly (`amount == units * openRate`, 227/227 verified).
    * **``take_profit_rate``/``is_no_take_profit`` (and the stop-loss pair)
      added 2026-09-11** (DESIGN.md §6.A "Broker-side exits"). These keys
      were always in the payload and were simply never read, so the app was
      structurally blind to a broker-side exit it never asked for:
      ``OrderRequest.to_json`` sends eight keys, none take-profit related,
      and the broker applies its own ``defaultTakeProfitPercentage`` for the
      instrument/leverage config this app orders into. On the live account
      229 of 230 open positions carry ``takeProfitRate / openRate == 11.00``
      (a +1000% TP), and all 230 carry ``isNoStopLoss == true``; position
      3550406905 carries ``takeProfitRate: 0.0`` with
      ``isNoTakeProfit: true``, proving "no TP" is representable in the same
      shape. Both rate fields are ``None`` when the broker omits the key
      entirely (older/other payload shapes must not crash the parser), and
      both ``is_no_*`` flags default to ``False`` -- the absence of the flag
      is not evidence of suppression. Reading the rate without the flag is a
      trap: ``takeProfitRate`` is ``0.0``, not absent, on the suppressed
      position, so the flag is what distinguishes "no TP" from a
      would-be-zero rate.
    """

    position_id: str
    instrument_id: int
    units: float
    entry_price: float
    amount: float
    leverage: int
    is_buy: bool
    open_date: str | None = None
    take_profit_rate: float | None = None
    is_no_take_profit: bool = False
    stop_loss_rate: float | None = None
    is_no_stop_loss: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PositionMark:
    """Mark-to-market for one :class:`Position`, computed at a given price.

    Deliberately a value object returned by :func:`mark_position`, not a
    method that reaches for a price itself -- DESIGN_market_data.md §4.2:
    the price used here MUST be the one already fetched this session for the
    signal (Tiingo/eToro candles, never eToro's ``rates`` endpoint -- see
    §2.9/§4 of the design doc for why that endpoint is unsafe), passed in by
    the caller rather than invented here.
    """

    position_id: str
    price: float
    current_value: float
    unrealized_pnl: float


def mark_position(position: Position, price: float) -> PositionMark:
    """Compute :class:`PositionMark` for ``position`` at ``price``.

    Cost basis is ``position.amount`` (§4.5) -- the broker's ``invested``
    field this used to read does not exist (§4.2/§10.7). This app never
    shorts and never uses leverage above 1 (DESIGN §6.A), so
    ``current_value = units * price`` and ``unrealized_pnl = current_value
    - amount`` hold unconditionally; there is no short-position sign flip to
    handle here.
    """
    current_value = position.units * price
    return PositionMark(
        position_id=position.position_id,
        price=price,
        current_value=current_value,
        unrealized_pnl=current_value - position.amount,
    )


@dataclass(frozen=True, slots=True)
class Balance:
    """Account-level credit and pending-order amounts (DESIGN §1.1/§4.5/§9).

    ``available_cash`` MUST be computed with eToro's documented formula,
    never ``total_balance`` (that is portfolio value, not cash): see
    :func:`compute_available_cash`:

        available = credit − (Σ ordersForOpen[i].amount where mirrorID == 0
                               + Σ orders[i].amount)

    Corrected 2026-09-05: this used to be built straight off ``GET
    /balances``, whose live body was assumed to carry ``credit``,
    ``orders``, and ``ordersForOpen``. A real-account probe found it does
    not -- ``/balances`` only carries ``totalBalance`` and a per-sub-account
    ``balances`` list. All three formula inputs actually live in the
    **portfolio** endpoint's ``clientPortfolio`` envelope instead (see
    :meth:`~nightwatch.etoro.client.EtoroClient.get_balance`'s docstring),
    which is what :class:`EtoroClient` now reads. ``total_balance`` is
    ``None`` in practice as a result -- the portfolio envelope's confirmed
    shape has no equivalent field, and nothing in this codebase reads
    ``total_balance`` today, so ``EtoroClient`` no longer makes a second
    round trip to ``/balances`` just to populate it.
    """

    credit: float
    total_balance: float | None
    available_cash: float
    raw: dict[str, Any] = field(default_factory=dict)


def compute_available_cash(
    credit: float,
    orders_for_open: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
) -> float:
    """Compute broker available cash from eToro's documented formula.

    ``available = credit - (sum of ordersForOpen[i].amount where
    mirrorID == 0) - (sum of orders[i].amount)`` (DESIGN §1.1, §4.5, §9).

    This is deliberately a free function, not just a method on
    :class:`Balance`, so :mod:`nightwatch.etoro.client` can compute it
    straight off the raw JSON body before constructing the dataclass, and so
    it is trivially unit-testable against a hand-built sample payload.
    """
    def amount(row: Mapping[str, Any]) -> float:
        if not isinstance(row, Mapping) or "amount" not in row:
            raise ValueError("pending order is missing amount")
        raw_value = row["amount"]
        if isinstance(raw_value, bool):
            raise ValueError("pending order amount must be numeric")
        value = float(raw_value)
        if not math.isfinite(value) or value < 0:
            raise ValueError("pending order amount must be finite and non-negative")
        return value

    if not isinstance(orders_for_open, (list, tuple)) or not isinstance(orders, (list, tuple)):
        raise ValueError("ordersForOpen and orders must be arrays")
    if isinstance(credit, bool):
        raise ValueError("credit must be numeric")
    credit_value = float(credit)
    if not math.isfinite(credit_value):
        raise ValueError("credit must be finite")
    pending_for_open = 0.0
    for row in orders_for_open:
        if not isinstance(row, Mapping) or "mirrorID" not in row:
            raise ValueError("ordersForOpen row is missing mirrorID")
        if isinstance(row["mirrorID"], bool):
            raise ValueError("ordersForOpen mirrorID must be an integer")
        try:
            mirror_id = float(row["mirrorID"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("ordersForOpen mirrorID must be an integer") from exc
        if not math.isfinite(mirror_id) or not mirror_id.is_integer():
            raise ValueError("ordersForOpen mirrorID must be an integer")
        row_amount = amount(row)
        if mirror_id == 0:
            pending_for_open += row_amount
    return credit_value - pending_for_open - sum(amount(row) for row in orders)


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
    """A close request body (DESIGN §4.3; partial close decided
    board nightwatch/handoff-2026-09-06.md §7).

    ``units_to_deduct=None`` (the default) is a **full** close — byte-identical
    on the wire to this app's original, only behaviour: ``{"InstrumentId":
    ..., "UnitsToDeduct": null}``. A caller that passes a positive
    ``units_to_deduct`` gets a **partial** close instead, deducting exactly
    that many units and leaving the position OPEN with the remainder.

    Partial close was explicitly out of scope until 2026-09-06 (the operator
    reversed an earlier "no" after considering the impact on the strategy as
    a whole, board nightwatch/handoff-2026-09-06.md §7) because SCHG is tracked only as the
    scalar ``schg_position_value``, not per-lot like UPRO — the executor that
    picks *which* lot and *how much* has to be able to close less than a
    whole position. This class only shapes the request body; it does NOT
    enforce the broker's $10 ``minPositionExposure``/``minPositionAmount``
    stub floor -- that requires the position's market value, which this
    dataclass does not carry. See :func:`partial_close_leaves_legal_remainder`
    for that check, which the (not-yet-built, out of scope here) lot-selection
    executor must call before constructing a partial ``CloseRequest``.
    """

    position_id: str
    instrument_id: int
    units_to_deduct: float | None = None

    def __post_init__(self) -> None:
        if self.units_to_deduct is not None and self.units_to_deduct <= 0:
            raise ValueError(
                f"CloseRequest.units_to_deduct must be positive or None, got "
                f"{self.units_to_deduct!r}"
            )

    def to_json(self) -> dict[str, Any]:
        return {"InstrumentId": self.instrument_id, "UnitsToDeduct": self.units_to_deduct}


#: The broker's $10 ``minPositionExposure``/``minPositionAmount`` stub floor
#: (DESIGN §4.3, board nightwatch/handoff-2026-09-06.md §7) — both are $10, verified live
#: 2026-09-05. A close, partial or full, may never leave a position worth
#: less than this and more than zero.
MIN_POSITION_VALUE_USD: Final = 10.0

#: Floating-point slack for treating a remainder as "zero" (i.e. a full
#: close). Deliberately far below any real position size or broker rounding.
_FULL_CLOSE_EPSILON: Final = 1e-6


def partial_close_leaves_legal_remainder(
    position_market_value: float,
    close_market_value: float,
    *,
    min_position_value: float = MIN_POSITION_VALUE_USD,
) -> bool:
    """Would closing ``close_market_value`` out of a position currently worth
    ``position_market_value`` leave a legal remainder?

    board nightwatch/handoff-2026-09-06.md §7, the SCHG close path decision: "order by cost,
    size by market value" — this app orders lots for a close by ``openRate``
    descending (cost basis, per share), but the partial amount, the shortfall
    arithmetic, and this $10 check all use **market value**
    (``units * price``, see :func:`mark_position`), never
    ``Position.amount`` (cost basis, ``amount == units * openRate``,
    227/227 verified). On an appreciated lot the two differ; using cost
    basis here would be wrong on every appreciated lot.

    **ASSUMPTION, explicitly unverified** (board nightwatch/handoff-2026-09-06.md §7): whether
    the broker's $10 floor actually gates on a remainder's market value or
    its cost basis is UNVERIFIED against the live account. This function
    assumes MARKET VALUE. Confirm on the DEMO account 2026-09-11 (§8) before
    trusting this on an appreciated lot; if the broker in fact gates on cost
    basis, this is the one function to fix.

    A remainder of (approximately) zero — i.e. a full close — is always
    legal, matching this app's only production behaviour today. A nonzero
    remainder is legal only if it is at least ``min_position_value``.

    Worked example (board nightwatch/handoff-2026-09-06.md §7): need $51, positions worth
    $50/$50/$60 -> close the $50 whole (remainder $0, legal), then take $1
    from the $60 (remainder $59, legal). Taking $51 from the $60 instead
    would leave a $9 stub and is illegal:

    >>> partial_close_leaves_legal_remainder(50.0, 50.0)   # the $50, in full
    True
    >>> partial_close_leaves_legal_remainder(60.0, 1.0)    # $1 of the $60
    True
    >>> partial_close_leaves_legal_remainder(60.0, 51.0)   # $51 of the $60
    False
    """
    remainder = position_market_value - close_market_value
    if remainder <= _FULL_CLOSE_EPSILON:
        return True
    return remainder >= min_position_value


@dataclass(frozen=True, slots=True)
class OrderResult:
    """The result of one poll of ``orders:lookup`` for a single order.

    ``status_label`` (added 2026-09-11) carries the broker's OWN name for the
    status verbatim -- the live payload sends ``{"id": 3, "name": "Filled"}``,
    and keeping ``name`` alongside the numeric ``id`` is what makes a future
    enum drift (a code we map to one label that eToro calls something else)
    visible instead of silent. ``None`` only when the payload stated a bare
    numeric status with no label at all.
    """

    order_id: str | None
    reference_id: str | None
    status: OrderStatus
    position_id: str | None = None
    fill_price: float | None = None
    fill_units: float | None = None
    fill_amount_usd: float | None = None
    reject_reason: str | None = None
    status_label: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_ORDER_STATUSES

    @property
    def is_filled(self) -> bool:
        """True only for an actual fill. WAITING_FOR_MARKET is never True here."""
        return self.status in FILLED_ORDER_STATUSES


@dataclass(frozen=True)
class EditPositionAck:
    """Acceptance of an asynchronous position edit, not confirmation of it."""

    operation_id: str | None
    position_id: str | None
    reference_id: str | None
    status_code: int


@dataclass(frozen=True, slots=True)
class TradeHistoryEntry:
    """One row of ``GET /trading/info/trade/history`` (DESIGN_market_data.md §10.5).

    Corrected 2026-09-05 against 120 real closed-trade rows from a live
    account (the same probe that closed §10.5): the previous version read
    ``positionID``, ``symbol``, ``action``, ``profit``, ``openDateTime``, and
    ``closeDateTime`` off every row -- **none of those key names exist** on a
    trade-history row. The real keys are ``positionId`` (lowercase ``d`` --
    see the casing note below), ``netProfit``, ``openTimestamp``, and
    ``closeTimestamp``; ``symbol`` and ``action`` do not exist at all.

    **Casing trap, confirmed across both endpoints.** The *portfolio*
    endpoint (:class:`Position`) uses ``positionID``/``instrumentID``
    (capital ``D``); *trade history* uses ``positionId``/``instrumentId``
    (lowercase ``d``). eToro is inconsistent *across* its own endpoints, not
    just internally wrong in one place -- ``_parse_trade_history_entry``
    checks both casings defensively, but the lowercase form is what the live
    trade-history payload actually sends.

    * **No ``symbol`` field**, exactly as with :class:`Position` (§4.2/§10.7):
      resolve one from ``instrument_id`` through the ``instruments`` table
      (``Repo.get_instrument``) -- ``_parse_trade_history_entry`` is a
      ``@staticmethod`` with no ``Repo`` access, so it cannot and must not
      fake one.
    * **No ``action`` field.** ``is_buy`` and ``leverage`` are the real,
      present equivalents.
    * **``investment``** is the closed-trade analogue of ``Position.amount``
      -- cost basis, in account currency, at open.
    * **``fees`` is a real, separate field, and ``netProfit`` is NOT net of
      it.** An earlier pass here checked ``netProfit`` against
      ``units * (closeRate - openRate)`` on a 119-row sample, found one
      mismatching row, and dismissed it as an unrelated "data anomaly." A
      full 120-row check (2026-09-05, DESIGN_market_data.md §10.13) found
      **three more rows like it**: the units-based formula actually matches
      only 117 of 120 rows within a cent, max error **$398.51**, and gets
      *substantially worse* once ``fees`` is added (mean absolute error 3.20
      vs. 0.0032, only 56/120 within a cent). **The formula that actually
      holds is ``investment * (closeRate / openRate - 1)``**, matching
      ``netProfit`` within $0.02 on **120/120** rows, max error $0.0091.
      ``investment`` -- not ``units``/``openRate`` -- is the trustworthy
      quantity on a closed trade, mirroring ``amount`` on an open one
      (:class:`Position`'s docstring, DESIGN_market_data.md §4.5-§4.6): eToro
      retroactively rewrites ``units``/``openRate`` on a split while
      preserving the money field exactly, and that same unreliability shows
      up here from the other side. The open-position invariant
      ``amount ~= units * openRate`` (227/227, §4.5) does **not** carry over
      to closed trades -- its analogue here holds only 116/120. Despite the
      name, ``netProfit`` is the rate P&L only; ``fees`` is reported
      separately and must be subtracted by the caller to get an all-in
      realized P&L inclusive of trading fees.
    """

    position_id: str
    instrument_id: int
    units: float
    is_buy: bool
    leverage: int
    open_rate: float | None
    close_rate: float | None
    profit: float | None
    investment: float | None
    fees: float | None
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
