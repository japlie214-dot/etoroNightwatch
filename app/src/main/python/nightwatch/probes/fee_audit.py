"""Pure, offline-testable logic for the sharpened E1 fee audit (2026-09-05).

``tools/probe_live_2026-09-11.py``'s own ``section_e1_fee_readonly`` (a
read-only file this module does not touch) only computes an *aggregate*
sum(fees)/sum(investment) per instrument. The operator's fee report has since
been sharpened with new information this module exists to act on:

1. The operator has never closed a single UPRO position -- ruling out
   overnight/financing fees on the leveraged UPRO as the source of the
   account-wide fee total.
2. Their new hypotheses are dividend-related or account/benefits-related
   charges, not trade commissions.
3. An API buy order for SCHG (commission-free) once *disclosed* a fee that
   was never actually charged.

None of that can be answered by an aggregate. This module builds the full
per-trade table, groups fees by instrument (the single most important
output -- it answers whether any plain-ETF trade ever carried a fee) and by
leverage/short-vs-1x-long, isolates the fee-bearing subset in full, checks
correlation of fees against holding duration and investment size, scans for
any dividend/account-level field the API even exposes, and reconciles the
per-trade sums against the two known account-wide totals (Sigma fees =
-$380.40, Sigma netProfit = +$327.25) -- loudly, if they disagree.

Nothing here performs I/O -- no ``httpx``, no file writes, no ``time.sleep``
-- exactly the split ``nightwatch.probes.live_window`` already established,
so this is exercisable by the desktop test suite without a live payload. The
probe-side runner (``android-probe/app/src/main/python/probe/probe_runner.py``)
does the network I/O and calls into this module for the arithmetic.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "TradeFeeRow",
    "build_fee_audit_rows",
    "InstrumentFeeGroup",
    "group_fees_by_instrument",
    "LeverageFeeGroup",
    "LEVERAGED_OR_SHORT",
    "PLAIN_1X_LONG",
    "group_fees_by_leverage",
    "fee_bearing_rows",
    "FeeCorrelation",
    "correlate_fees_with",
    "FeeReconciliationTotals",
    "EXPECTED_SUM_FEES",
    "EXPECTED_SUM_NET_PROFIT",
    "reconcile_account_totals",
    "DividendFeeExposureCheck",
    "check_dividend_or_account_fee_exposure",
]

#: HANDOFF_2026-09-05's own recorded aggregate, across all 120 real closed
#: trades: Sigma netProfit = +$327.25 but Sigma fees = -$380.40, i.e. the
#: account's true all-in result is a $53.15 LOSS where netProfit alone
#: reports a profit. The per-trade rows this module builds must reproduce
#: both figures, or that disagreement is itself the finding.
EXPECTED_SUM_FEES = -380.40
EXPECTED_SUM_NET_PROFIT = 327.25

LEVERAGED_OR_SHORT = "leveraged_or_short"
PLAIN_1X_LONG = "plain_1x_long"


def _parse_days_between(open_date: Any, close_date: Any) -> float | None:
    """Best-effort day-count between two ISO-ish date/datetime strings.

    Returns ``None`` if either side is missing or unparseable -- a row this
    cannot compute a duration for is reported as "unknown", never silently
    dropped from the audit or coerced to 0 (0 days would look like a same-day
    round trip, a materially different and misleading claim).
    """
    if not open_date or not close_date:
        return None
    from datetime import datetime

    def _parse(value: Any) -> datetime | None:
        text = str(value)
        # eToro timestamps show up as either plain dates or full ISO
        # datetimes, sometimes with a trailing 'Z'. Try the common shapes
        # rather than assuming one.
        for candidate in (text, text.replace("Z", "+00:00")):
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text[:10])
        except ValueError:
            return None

    start, end = _parse(open_date), _parse(close_date)
    if start is None or end is None:
        return None
    delta = end - start
    return delta.total_seconds() / 86400.0


@dataclass(frozen=True, slots=True)
class TradeFeeRow:
    """One closed trade, flattened for the per-trade fee audit table."""

    position_id: str
    instrument_id: int
    symbol: str | None
    open_date: str | None
    close_date: str | None
    investment: float | None
    net_profit: float | None
    fees: float | None
    leverage: int
    is_buy: bool
    is_leveraged_or_short: bool
    """True iff ``leverage != 1`` or the trade was a short (``not is_buy``)."""
    holding_days: float | None


def build_fee_audit_rows(
    trades: Sequence[Mapping[str, Any]],
    *,
    symbol_by_instrument_id: Mapping[int, str] | None = None,
    instrument_key_field: str = "instrument_id",
    fees_field: str = "fees",
    investment_field: str = "investment",
    net_profit_field: str = "profit",
    leverage_field: str = "leverage",
    is_buy_field: str = "is_buy",
    open_date_field: str = "open_date",
    close_date_field: str = "close_date",
    position_id_field: str = "position_id",
) -> list[TradeFeeRow]:
    """Flatten every closed trade into one :class:`TradeFeeRow`.

    Field names default to :class:`nightwatch.etoro.models.TradeHistoryEntry`'s
    own attribute names (as produced by ``dataclasses.asdict``), not eToro's
    raw wire names -- the caller is expected to hand this the parsed
    dataclasses-as-dicts, the same shape ``section_e1_fee_readonly`` already
    saves to disk, not the raw JSON.
    """
    symbol_by_instrument_id = symbol_by_instrument_id or {}
    rows: list[TradeFeeRow] = []
    for trade in trades:
        instrument_id = trade.get(instrument_key_field)
        leverage = trade.get(leverage_field)
        leverage_int = int(leverage) if leverage is not None else 1
        is_buy = bool(trade.get(is_buy_field, True))
        open_date = trade.get(open_date_field)
        close_date = trade.get(close_date_field)
        rows.append(
            TradeFeeRow(
                position_id=str(trade.get(position_id_field)),
                instrument_id=instrument_id,
                symbol=symbol_by_instrument_id.get(instrument_id),
                open_date=open_date,
                close_date=close_date,
                investment=trade.get(investment_field),
                net_profit=trade.get(net_profit_field),
                fees=trade.get(fees_field),
                leverage=leverage_int,
                is_buy=is_buy,
                is_leveraged_or_short=(leverage_int != 1) or (not is_buy),
                holding_days=_parse_days_between(open_date, close_date),
            )
        )
    return rows


@dataclass(frozen=True, slots=True)
class InstrumentFeeGroup:
    """Fees grouped by instrument -- the single most important E1 output.

    Answers, per instrument, whether ANY trade in it ever carried a nonzero
    fee -- in particular, whether a plain 1x ETF (SCHG) trade ever did.
    """

    instrument_id: Any
    symbol: str | None
    count: int
    sum_fees: float
    sum_investment: float
    sum_net_profit: float
    any_nonzero_fee: bool


def group_fees_by_instrument(rows: Sequence[TradeFeeRow]) -> list[InstrumentFeeGroup]:
    """Group :func:`build_fee_audit_rows`' output by instrument.

    A ``None`` fee/investment/profit contributes 0.0 to the sums (mirroring
    ``nightwatch.probes.live_window.partition_trades_by_instrument``) but the
    row still counts -- a missing value is data to report, not a row to drop.
    Returned sorted by ``abs(sum_fees)`` descending, so the instrument
    carrying the account's fee burden is always first.
    """
    buckets: dict[Any, dict[str, Any]] = {}
    for row in rows:
        bucket = buckets.setdefault(
            row.instrument_id,
            {
                "symbol": row.symbol,
                "count": 0,
                "sum_fees": 0.0,
                "sum_investment": 0.0,
                "sum_net_profit": 0.0,
                "any_nonzero_fee": False,
            },
        )
        bucket["count"] += 1
        if row.symbol is not None:
            bucket["symbol"] = row.symbol
        if row.fees is not None:
            bucket["sum_fees"] += float(row.fees)
            if float(row.fees) != 0.0:
                bucket["any_nonzero_fee"] = True
        if row.investment is not None:
            bucket["sum_investment"] += float(row.investment)
        if row.net_profit is not None:
            bucket["sum_net_profit"] += float(row.net_profit)

    groups = [
        InstrumentFeeGroup(
            instrument_id=instrument_id,
            symbol=v["symbol"],
            count=v["count"],
            sum_fees=v["sum_fees"],
            sum_investment=v["sum_investment"],
            sum_net_profit=v["sum_net_profit"],
            any_nonzero_fee=v["any_nonzero_fee"],
        )
        for instrument_id, v in buckets.items()
    ]
    return sorted(groups, key=lambda g: -abs(g.sum_fees))


@dataclass(frozen=True, slots=True)
class LeverageFeeGroup:
    """Fees grouped by whether the trade was leveraged/short vs 1x long."""

    bucket: str
    count: int
    sum_fees: float
    sum_investment: float
    any_nonzero_fee: bool


def group_fees_by_leverage(rows: Sequence[TradeFeeRow]) -> dict[str, LeverageFeeGroup]:
    """Group by :attr:`TradeFeeRow.is_leveraged_or_short`.

    Tests the operator's leading prior hypothesis (an overnight/financing
    charge on a leveraged or shorted position) independent of *which*
    instrument that turns out to be -- useful precisely because it does not
    assume UPRO is the only leveraged instrument in this account's history.
    """
    buckets: dict[str, dict[str, Any]] = {
        LEVERAGED_OR_SHORT: {"count": 0, "sum_fees": 0.0, "sum_investment": 0.0, "any_nonzero_fee": False},
        PLAIN_1X_LONG: {"count": 0, "sum_fees": 0.0, "sum_investment": 0.0, "any_nonzero_fee": False},
    }
    for row in rows:
        key = LEVERAGED_OR_SHORT if row.is_leveraged_or_short else PLAIN_1X_LONG
        b = buckets[key]
        b["count"] += 1
        if row.fees is not None:
            b["sum_fees"] += float(row.fees)
            if float(row.fees) != 0.0:
                b["any_nonzero_fee"] = True
        if row.investment is not None:
            b["sum_investment"] += float(row.investment)
    return {
        key: LeverageFeeGroup(
            bucket=key,
            count=v["count"],
            sum_fees=v["sum_fees"],
            sum_investment=v["sum_investment"],
            any_nonzero_fee=v["any_nonzero_fee"],
        )
        for key, v in buckets.items()
    }


def fee_bearing_rows(rows: Sequence[TradeFeeRow]) -> list[TradeFeeRow]:
    """Every row with a genuinely nonzero fee, in full.

    120 rows total is small enough to print every fee-bearing one rather than
    summarizing (the operator's explicit ask) -- a row whose ``fees`` is
    ``None`` is NOT included here (unknown is not "nonzero"); only a fee that
    is present and numerically nonzero counts.
    """
    return [r for r in rows if r.fees is not None and float(r.fees) != 0.0]


@dataclass(frozen=True, slots=True)
class FeeCorrelation:
    """Pearson correlation of ``fees`` against one other numeric field."""

    metric: str
    n: int
    pearson_r: float | None
    """``None`` when undefined: fewer than two rows with both values
    present, or one side has zero variance (``statistics.correlation``
    itself raises ``StatisticsError`` in that case -- reported here as
    "undefined", not as 0.0, which would misleadingly claim "no
    correlation" rather than "not computable")."""
    note: str


def correlate_fees_with(
    rows: Sequence[TradeFeeRow], metric: str
) -> FeeCorrelation:
    """Correlate ``fees`` against ``holding_days`` (financing-charge
    hypothesis) or ``investment`` (percentage-commission hypothesis).

    Only rows where both ``fees`` and the chosen metric are present are used.
    This computes the number, it does not eyeball it -- the operator's
    explicit ask.
    """
    if metric not in ("holding_days", "investment"):
        raise ValueError(f"metric must be 'holding_days' or 'investment', got {metric!r}")

    pairs: list[tuple[float, float]] = []
    for row in rows:
        other = row.holding_days if metric == "holding_days" else row.investment
        if row.fees is None or other is None:
            continue
        pairs.append((float(other), float(row.fees)))

    n = len(pairs)
    if n < 2:
        return FeeCorrelation(metric=metric, n=n, pearson_r=None, note="fewer than 2 rows with both values present")

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    try:
        r = statistics.correlation(xs, ys)
    except statistics.StatisticsError as e:
        return FeeCorrelation(metric=metric, n=n, pearson_r=None, note=f"undefined: {e}")
    return FeeCorrelation(metric=metric, n=n, pearson_r=r, note=f"pearson r over {n} rows with both values present")


@dataclass(frozen=True, slots=True)
class FeeReconciliationTotals:
    """Whether the per-trade rows reproduce the two known account totals."""

    n_rows: int
    sum_fees: float
    sum_net_profit: float
    expected_sum_fees: float
    expected_sum_net_profit: float
    fees_diff: float
    net_profit_diff: float
    fees_reconcile: bool
    net_profit_reconciles: bool
    verdict: str


def reconcile_account_totals(
    rows: Sequence[TradeFeeRow],
    *,
    expected_sum_fees: float = EXPECTED_SUM_FEES,
    expected_sum_net_profit: float = EXPECTED_SUM_NET_PROFIT,
    tolerance: float = 0.01,
) -> FeeReconciliationTotals:
    """Sum ``fees``/``net_profit`` across every row and diff against the two
    account-wide figures HANDOFF_2026-09-05.md already recorded.

    A ``None`` fee/profit contributes 0.0, matching every other grouping
    function in this module. If the sums do not reconcile within
    ``tolerance`` dollars, that disagreement is reported as a genuine
    finding (a different total than what was previously recorded, or a
    parsing/field-name problem), never silently adjusted to match.
    """
    sum_fees = sum(float(r.fees) for r in rows if r.fees is not None)
    sum_net_profit = sum(float(r.net_profit) for r in rows if r.net_profit is not None)
    fees_diff = sum_fees - expected_sum_fees
    net_profit_diff = sum_net_profit - expected_sum_net_profit
    fees_reconcile = abs(fees_diff) <= tolerance
    net_profit_reconciles = abs(net_profit_diff) <= tolerance

    if fees_reconcile and net_profit_reconciles:
        verdict = (
            f"RECONCILES: sum(fees)=${sum_fees:,.2f} and sum(net_profit)=${sum_net_profit:,.2f} "
            f"both match the recorded account totals within ${tolerance:.2f}."
        )
    else:
        parts = []
        if not fees_reconcile:
            parts.append(f"sum(fees)=${sum_fees:,.2f} vs expected ${expected_sum_fees:,.2f} (diff ${fees_diff:,.2f})")
        if not net_profit_reconciles:
            parts.append(
                f"sum(net_profit)=${sum_net_profit:,.2f} vs expected ${expected_sum_net_profit:,.2f} "
                f"(diff ${net_profit_diff:,.2f})"
            )
        verdict = "DOES NOT RECONCILE -- " + "; ".join(parts) + (
            " -- this is itself a major finding: report it, do not adjust the expected "
            "figures to match without independently re-deriving them."
        )

    return FeeReconciliationTotals(
        n_rows=len(rows),
        sum_fees=sum_fees,
        sum_net_profit=sum_net_profit,
        expected_sum_fees=expected_sum_fees,
        expected_sum_net_profit=expected_sum_net_profit,
        fees_diff=fees_diff,
        net_profit_diff=net_profit_diff,
        fees_reconcile=fees_reconcile,
        net_profit_reconciles=net_profit_reconciles,
        verdict=verdict,
    )


#: Substrings this project has any reason to expect on a dividend- or
#: account-level charge field, if the API exposes one at all. Deliberately
#: broad (case-insensitive substring match, not an exact key list) since the
#: whole point of this check is "we don't know the field name" -- a narrower
#: exact-match list would just repeat the same "assumed field shape" mistake
#: DESIGN_market_data.md §9 already flagged once.
_DIVIDEND_OR_ACCOUNT_KEYWORDS: tuple[str, ...] = (
    "dividend", "div", "account", "custody", "benefit", "maintenance", "inactivity",
)


@dataclass(frozen=True, slots=True)
class DividendFeeExposureCheck:
    """Whether ANY dividend- or account-level fee field is exposed at all."""

    checked_payload_count: int
    fields_found: tuple[str, ...]
    verdict: str


def _iter_keys(obj: Any) -> Iterable[str]:
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            yield str(key)
            yield from _iter_keys(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _iter_keys(item)


def check_dividend_or_account_fee_exposure(
    raw_payloads: Sequence[Mapping[str, Any]],
    *,
    keywords: Sequence[str] = _DIVIDEND_OR_ACCOUNT_KEYWORDS,
) -> DividendFeeExposureCheck:
    """Scan every key (recursively) across ``raw_payloads`` for anything
    dividend- or account-charge-shaped.

    The operator's new hypothesis (fees may be dividend-related, or an
    account/benefits charge) cannot be tested by inference from
    ``netProfit``/``fees`` alone -- either the API exposes a field for it, or
    it does not. This says so plainly rather than guessing: an empty
    ``fields_found`` means no such field was found in the payloads checked,
    which is itself the answer, not an inconclusive result.
    """
    found: set[str] = set()
    lowered_keywords = tuple(k.lower() for k in keywords)
    for payload in raw_payloads:
        for key in _iter_keys(payload):
            key_lower = key.lower()
            if any(kw in key_lower for kw in lowered_keywords):
                found.add(key)

    if found:
        verdict = (
            f"{len(found)} field(s) with a dividend/account-charge-shaped name were found "
            f"in the {len(raw_payloads)} payload(s) checked -- inspect these specifically, "
            "do not assume they are the source without checking their actual values: "
            f"{sorted(found)}"
        )
    else:
        verdict = (
            f"NO field with a dividend- or account-charge-shaped name was found anywhere in "
            f"the {len(raw_payloads)} payload(s) checked. Say so plainly: this API surface, as "
            "far as this probe can see, does not expose dividend or account-level fee data at "
            "all -- the fee-bearing rows must be explained some other way (or via an endpoint "
            "this probe never called)."
        )

    return DividendFeeExposureCheck(
        checked_payload_count=len(raw_payloads),
        fields_found=tuple(sorted(found)),
        verdict=verdict,
    )
