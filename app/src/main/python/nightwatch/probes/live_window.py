"""Pure, off-device-testable logic for the 2026-09-11 live probe.

Built for ``tools/probe_live_2026-09-11.py`` (HANDOFF_2026-09-05.md §7 items 4
and 7, DESIGN_market_data.md §10.5/§10.6/§10.9-§10.13, and the new operator fee
report). Everything in this module is plain arithmetic and dict/list
manipulation -- no ``httpx``, no ``time.sleep``, no file I/O -- specifically so
it can be exercised by the desktop test suite (which cannot reach eToro at
all, HANDOFF.md §2/§9) without a live payload. The probe script does the
network I/O and calls into this module for the parts that are worth getting
right and worth testing.

Nothing here is a source of truth about what the live API returns -- the
``PARSER_READ_KEYS`` registry below is a manually maintained snapshot of what
each parser in ``etoro/client.py`` reads, as of 2026-09-05. If a parser is
edited, this registry can silently go stale; the probe script's report says
so explicitly rather than presenting a diff as ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "PARSER_READ_KEYS",
    "KeyDiff",
    "diff_keys",
    "PARSER_REQUIRED_KEY_GROUPS",
    "PARSER_OPTIONAL_KEYS",
    "RequiredKeyDiff",
    "diff_required_keys",
    "InstrumentFeeSummary",
    "partition_trades_by_instrument",
    "SleeveMismatch",
    "verify_sleeve_arithmetic",
    "FeeReconciliation",
    "reconcile_fee_hypothesis",
]

# ---------------------------------------------------------------------------
# Parser read-key registry -- keep in sync with etoro/client.py (2026-09-05).
# ---------------------------------------------------------------------------

#: For each of the six parsers HANDOFF_2026-09-05.md §6/§8 lists as
#: "unverified against live payloads", the set of raw JSON keys that parser's
#: *current* code actually reads (top-level keys only for the two that do no
#: field-level parsing at all -- ``get_instruments``/``get_costs`` -- see the
#: note on each entry below). Truthiness-sensitive fields (handled via an
#: ``or``-chain rather than an explicit ``is not None`` check -- the §4
#: "fourth instance" defect class) are listed separately in
#: ``PARSER_TRUTHINESS_KEYS`` so the probe can flag them even when the key
#: itself is present.
PARSER_READ_KEYS: dict[str, frozenset[str]] = {
    # _parse_candle (client.py ~888): row-level keys, envelope resolved by
    # get_candles before this is called.
    "_parse_candle": frozenset(
        {"fromDate", "date", "timestamp", "open", "Open", "high", "High",
         "low", "Low", "close", "Close", "volume"}
    ),
    # get_rates (client.py ~803): envelope key "rates" plus per-row keys.
    "get_rates": frozenset(
        {"rates", "instrumentID", "instrumentId", "InstrumentID",
         "bid", "Bid", "ask", "Ask", "lastExecution", "date", "timestamp",
         "dateTime"}
    ),
    # get_eligibility (client.py ~920): envelope + per-row + nested
    # leverageConfigs keys.
    "get_eligibility": frozenset(
        {"eligibilities", "eligibility", "instrumentId", "instrumentID",
         "allowOpenPosition", "minPositionExposure", "minPositionAmount",
         "allowPartialClosePosition", "allowPartialClose", "leverageConfigs",
         "settlementType", "direction"}
    ),
    # _parse_order_result (client.py ~1156): envelope "order" plus row keys.
    "_parse_order_result": frozenset(
        {"order", "status", "orderStatus", "orderId", "referenceId",
         "positionId", "executionPrice", "units", "amount", "rejectReason"}
    ),
    # get_instruments (client.py ~782): does NOT parse individual fields at
    # all -- it returns the raw rows under the "instrumentDisplayDatas" key
    # verbatim. The only "key this parser reads" is the envelope key itself;
    # everything inside each row is unread by client code today. Recorded
    # this way deliberately so a diff against it reports every row-level key
    # as "present in payload, ignored by parser" -- which is true and is the
    # point of auditing it.
    "get_instruments": frozenset({"instrumentDisplayDatas"}),
    # get_costs (client.py ~1036): same story -- returns dict(response.json())
    # with zero field-level parsing. The "keys read" set is deliberately
    # empty; every top-level key in a real payload will show up as "ignored".
    "get_costs": frozenset(),
}

#: Fields read via a falsy-losing ``or``/``.get(..., default)`` chain rather
#: than an explicit ``is not None`` check, per parser -- the §4 "fourth
#: instance" defect class (a legitimate ``0`` silently loses to a fallback
#: key). Listed here so the probe can flag these specifically when auditing a
#: live payload, independent of the key-presence diff.
PARSER_TRUTHINESS_KEYS: dict[str, frozenset[str]] = {
    # get_rates: `row.get("bid", row.get("Bid", 0.0))` etc. -- a real 0.0 bid
    # is indistinguishable from "absent" here.
    "get_rates": frozenset({"bid", "Bid", "ask", "Ask"}),
    # _parse_candle: `row.get("open", row.get("Open", 0.0))` etc. -- same
    # shape, a genuine 0.0 OHLC value (implausible for these instruments, but
    # not impossible for a data glitch) would be silently indistinguishable
    # from "field absent".
    "_parse_candle": frozenset({"open", "Open", "high", "High", "low", "Low",
                                 "close", "Close"}),
    # get_eligibility: `row.get("instrumentId", row.get("instrumentID", 0))`
    # -- an instrument id of 0 would silently look identical to "missing".
    "get_eligibility": frozenset({"instrumentId", "instrumentID"}),
}


@dataclass(frozen=True, slots=True)
class KeyDiff:
    """Result of comparing a live payload's keys against a parser's read set."""

    parser_name: str
    missing_from_payload: frozenset[str]
    """Keys the parser reads that are ABSENT from this live payload -- the
    exact defect class found four times this week (HANDOFF_2026-09-05 §4)."""
    unused_in_payload: frozenset[str]
    """Keys present in the payload that the parser never reads."""
    payload_keys: frozenset[str]
    parser_keys: frozenset[str]

    @property
    def is_clean(self) -> bool:
        """True iff every key the parser reads is present in the payload.

        Says nothing about ``unused_in_payload`` -- an unread field is not a
        defect, only a thing worth knowing about.
        """
        return not self.missing_from_payload


def diff_keys(payload_keys: Iterable[str], parser_name: str) -> KeyDiff:
    """Compare a live (flat, single-row) payload's keys against
    ``PARSER_READ_KEYS[parser_name]``.

    ``payload_keys`` should be the keys of one representative row/object from
    the live response (the probe script is responsible for picking the right
    level of nesting -- e.g. one candle row, one rates row -- before calling
    this). Raises ``KeyError`` if ``parser_name`` is not in the registry,
    deliberately -- a silently-empty diff for an unregistered parser would be
    exactly the "didn't error, not a pass" trap this whole exercise exists to
    avoid.
    """
    if parser_name not in PARSER_READ_KEYS:
        raise KeyError(
            f"{parser_name!r} is not in PARSER_READ_KEYS -- add it before "
            "auditing it, or this diff proves nothing"
        )
    parser_keys = PARSER_READ_KEYS[parser_name]
    payload_key_set = frozenset(payload_keys)
    return KeyDiff(
        parser_name=parser_name,
        missing_from_payload=parser_keys - payload_key_set,
        unused_in_payload=payload_key_set - parser_keys,
        payload_keys=payload_key_set,
        parser_keys=parser_keys,
    )


# ---------------------------------------------------------------------------
# Required-vs-fallback key diff (§F) -- fixes diff_keys's false-positive rate
# ---------------------------------------------------------------------------

#: Groups of alternative key names, per parser, where AT LEAST ONE member of
#: each inner ``frozenset`` must be present in a real payload for that field
#: to be considered satisfied. This is the fix for ``diff_keys``'s high
#: false-positive rate against an ``or``/``.get(..., .get(...))`` fallback
#: chain: section C's own probe run flagged ``_parse_candle`` for six
#: "missing" keys that are entirely correct, because each flagged name is one
#: alternative among several (e.g. ``date``/``timestamp`` are both fallbacks
#: for ``fromDate``, and a real payload satisfying ``fromDate`` alone is not
#: missing anything). ``diff_keys``/``PARSER_READ_KEYS`` is left as-is
#: (still used by section C) -- this is a separate, additive registry for
#: section F, not a fix applied retroactively to section C's own audit.
PARSER_REQUIRED_KEY_GROUPS: dict[str, list[frozenset[str]]] = {
    # _parse_candle (client.py ~910): raw_date = row.get("fromDate") or
    # row.get("date") or row.get("timestamp") -- one date-shaped key
    # required, any of the three satisfies it. OHLC each have a plain/
    # capitalised fallback pair.
    "_parse_candle": [
        frozenset({"fromDate", "date", "timestamp"}),
        frozenset({"open", "Open"}),
        frozenset({"high", "High"}),
        frozenset({"low", "Low"}),
        frozenset({"close", "Close"}),
    ],
    # get_rates (client.py ~824): instrument id has a three-way casing
    # fallback (the exact bug §2.9/§C found: a live response used a fourth,
    # unhandled casing); bid/ask each have a plain/capitalised fallback.
    "get_rates": [
        frozenset({"instrumentID", "instrumentId", "InstrumentID"}),
        frozenset({"bid", "Bid"}),
        frozenset({"ask", "Ask"}),
    ],
    # get_eligibility (client.py ~941): envelope key has a two-way fallback;
    # instrumentId/instrumentID likewise.
    "get_eligibility": [
        frozenset({"eligibilities", "eligibility"}),
        frozenset({"instrumentId", "instrumentID"}),
    ],
    # get_eod_history (market/source.py get_eod_history): no fallback chain
    # at all -- each of these is read via a single dict-index/key, so each
    # is its own singleton "group" (still expressed as a group so the same
    # function/shape handles both cases uniformly).
    "get_eod_history": [
        frozenset({"date"}),
        frozenset({"close"}),
        frozenset({"adjClose"}),
    ],
    # get_iex_quotes (market/source.py get_iex_quotes): `ticker`/`tngoLast`
    # are the only two fields the parser actually requires (a row missing
    # either is skipped entirely, deliberately, per that method's own
    # docstring -- "a missing key is no data, never a zero price").
    "get_iex_quotes": [
        frozenset({"ticker"}),
        frozenset({"tngoLast"}),
    ],
}

#: Keys a parser reads (or could read) but which may legitimately be absent
#: from a real payload with no defect implied -- a genuinely optional field
#: the code already treats as ``None``/a safe default on absence, not a
#: fallback alternative for some other required field. Not reported as
#: "missing" by :func:`diff_required_keys`; still surfaced as "known but
#: optional" so nothing about the payload is silently ignored.
PARSER_OPTIONAL_KEYS: dict[str, frozenset[str]] = {
    "_parse_candle": frozenset({"volume"}),
    "get_rates": frozenset({"lastExecution", "date", "timestamp", "dateTime"}),
    "get_eligibility": frozenset(
        {
            "allowOpenPosition", "minPositionExposure", "minPositionAmount",
            "allowPartialClosePosition", "allowPartialClose", "leverageConfigs",
            "settlementType", "direction",
        }
    ),
    "get_eod_history": frozenset({"splitFactor", "divCash"}),
    "get_iex_quotes": frozenset(
        {"timestamp", "lastSaleTimestamp", "quoteTimestamp", "prevClose"}
    ),
}


@dataclass(frozen=True, slots=True)
class RequiredKeyDiff:
    """Result of :func:`diff_required_keys` -- distinguishes a genuinely
    missing required field (every alternative in its group absent from the
    payload) from a satisfied fallback chain (at least one alternative
    present), the distinction ``diff_keys`` does not make.
    """

    parser_name: str
    missing_groups: tuple[frozenset[str], ...]
    """Groups where NONE of the alternatives are present in the payload --
    a genuine gap, the same defect class HANDOFF_2026-09-05.md §4 names."""
    satisfied_groups: tuple[tuple[frozenset[str], str], ...]
    """(group, key_that_satisfied_it) for every group with >=1 key present."""
    unused_in_payload: frozenset[str]
    """Payload keys that are neither part of a required group nor a
    documented optional key for this parser -- present, and currently
    ignored entirely."""
    payload_keys: frozenset[str]

    @property
    def is_clean(self) -> bool:
        """True iff every required group has at least one alternative
        present. Says nothing about ``unused_in_payload``."""
        return not self.missing_groups


def diff_required_keys(payload_keys: Iterable[str], parser_name: str) -> RequiredKeyDiff:
    """Compare a live (flat, single-row) payload's keys against
    ``PARSER_REQUIRED_KEY_GROUPS[parser_name]``, crediting any one member of
    each alternative group as satisfying it -- unlike ``diff_keys``, which
    flags every alternative name individually and so over-reports "missing"
    keys for any field read through an ``or``/``.get(..., .get(...))`` chain.

    Raises ``KeyError`` if ``parser_name`` is not in the registry,
    deliberately, for the same reason ``diff_keys`` does: a silently-empty
    diff for an unregistered parser would prove nothing.
    """
    if parser_name not in PARSER_REQUIRED_KEY_GROUPS:
        raise KeyError(
            f"{parser_name!r} is not in PARSER_REQUIRED_KEY_GROUPS -- add it "
            "before auditing it, or this diff proves nothing"
        )
    groups = PARSER_REQUIRED_KEY_GROUPS[parser_name]
    optional = PARSER_OPTIONAL_KEYS.get(parser_name, frozenset())
    payload_key_set = frozenset(payload_keys)

    missing: list[frozenset[str]] = []
    satisfied: list[tuple[frozenset[str], str]] = []
    for group in groups:
        present = sorted(group & payload_key_set)
        if present:
            satisfied.append((group, present[0]))
        else:
            missing.append(group)

    known = optional.union(*groups) if groups else frozenset(optional)
    unused = payload_key_set - known
    return RequiredKeyDiff(
        parser_name=parser_name,
        missing_groups=tuple(missing),
        satisfied_groups=tuple(satisfied),
        unused_in_payload=unused,
        payload_keys=payload_key_set,
    )


# ---------------------------------------------------------------------------
# Fee partitioning (§E1 -- the fee question, read-only half)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstrumentFeeSummary:
    instrument_key: Any
    """Whatever the caller partitioned by -- instrument_id or symbol."""
    count: int
    sum_fees: float
    sum_investment: float
    any_nonzero_fee: bool


def partition_trades_by_instrument(
    trades: Sequence[Mapping[str, Any]],
    *,
    instrument_key_field: str = "instrument_id",
    fees_field: str = "fees",
    investment_field: str = "investment",
) -> dict[Any, InstrumentFeeSummary]:
    """Partition closed trades by instrument and sum ``fees``/``investment``.

    HANDOFF_2026-09-05.md §5: across all 120 real closed trades, Sigma
    ``fees`` = -$380.40 -- non-zero *somewhere* in this account's history.
    The operator's new fee report hypothesises plain-ETF (SCHG) trades never
    carry a fee, unlike leveraged/other instruments (UPRO, or the 18
    positions in 5 other instruments). This is the arithmetic that tests that
    hypothesis, independent of which instrument turns out to be which --
    the probe script supplies ``instrument_key_field`` values (ids or
    resolved symbols) and this function only sums and partitions.

    A trade whose fee/investment field is ``None`` contributes ``0.0`` to the
    sums (not excluded from ``count``) -- a missing fee is data to report, not
    a row to silently drop.
    """
    out: dict[Any, dict[str, Any]] = {}
    for trade in trades:
        key = trade.get(instrument_key_field)
        bucket = out.setdefault(
            key, {"count": 0, "sum_fees": 0.0, "sum_investment": 0.0, "any_nonzero_fee": False}
        )
        bucket["count"] += 1
        fee = trade.get(fees_field)
        if fee is not None:
            fee = float(fee)
            bucket["sum_fees"] += fee
            if fee != 0.0:
                bucket["any_nonzero_fee"] = True
        investment = trade.get(investment_field)
        if investment is not None:
            bucket["sum_investment"] += float(investment)
    return {
        key: InstrumentFeeSummary(
            instrument_key=key,
            count=v["count"],
            sum_fees=v["sum_fees"],
            sum_investment=v["sum_investment"],
            any_nonzero_fee=v["any_nonzero_fee"],
        )
        for key, v in out.items()
    }


# ---------------------------------------------------------------------------
# Sleeve arithmetic re-measurement (§D)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SleeveMismatch:
    quantity: str
    documented: float
    measured: float
    abs_diff: float
    pct_diff: float | None
    """None when documented is 0 (percentage undefined)."""


def verify_sleeve_arithmetic(
    measured: Mapping[str, float],
    documented: Mapping[str, float],
    *,
    tolerance_pct: float = 0.02,
) -> list[SleeveMismatch]:
    """Compare freshly measured sleeve figures against DESIGN_market_data.md
    §10.11's recorded ("documented") figures.

    HANDOFF_2026-09-05.md §11 is explicit: this document family has twice let
    a plausible number sit next to a measured one, and the fix is to
    re-measure immediately before any migration rather than reuse a prior
    session's numbers as inputs. Returns one :class:`SleeveMismatch` per
    quantity present in *both* mappings whose relative difference exceeds
    ``tolerance_pct`` (2% default -- generous, since a live SCHG/UPRO price a
    day or two later than the document's own measurement should legitimately
    move the dollar figures a little; this is a "loudly flag a real
    disagreement" check, not a byte-for-byte equality check). A quantity
    documented as exactly 0.0 is compared by absolute difference against a
    small fixed epsilon instead of a percentage, since percentage is
    undefined at zero.
    """
    mismatches: list[SleeveMismatch] = []
    for key, doc_value in documented.items():
        if key not in measured:
            continue
        measured_value = measured[key]
        abs_diff = abs(measured_value - doc_value)
        if doc_value == 0.0:
            pct_diff = None
            flagged = abs_diff > 1.0  # epsilon in dollars
        else:
            pct_diff = abs_diff / abs(doc_value)
            flagged = pct_diff > tolerance_pct
        if flagged:
            mismatches.append(
                SleeveMismatch(
                    quantity=key,
                    documented=doc_value,
                    measured=measured_value,
                    abs_diff=abs_diff,
                    pct_diff=pct_diff,
                )
            )
    return mismatches


# ---------------------------------------------------------------------------
# The fee hypothesis (§E2 arithmetic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeeReconciliation:
    cash_before: float
    cash_after: float
    order_amount_usd: float
    disclosed_fee: float | None
    position_amount: float | None
    cash_delta: float
    """cash_before - cash_after. Positive means cash went down."""
    expected_cash_delta_if_no_fee: float
    """Equals order_amount_usd -- what the delta would be if the whole order
    amount left cash and no separate fee was charged."""
    cash_delta_matches_order_amount: bool
    """True if cash_delta is within one cent of order_amount_usd -- i.e. cash
    moved by exactly the order amount, no more, no less."""
    position_amount_matches_order_amount: bool | None
    """True if position_amount is within one cent of order_amount_usd. None
    if position_amount is unknown."""
    verdict: str
    """A short, human-readable conclusion for exactly this run's numbers --
    not a general claim about eToro's fee model, only about this one trade."""


def reconcile_fee_hypothesis(
    *,
    cash_before: float,
    cash_after: float,
    order_amount_usd: float,
    disclosed_fee: float | None,
    position_amount: float | None,
    cents_tolerance: float = 0.01,
) -> FeeReconciliation:
    """Settle (for one order) whether a fee is hidden inside the position
    ``amount``, charged separately against cash, or not charged at all.

    The operator's hypothesis (HANDOFF_2026-09-05.md's brief for this probe):
    a $100 SCHG buy whose response *disclosed* a fee nonetheless produced a
    $100 position and no extra cash deduction -- i.e. ``amount`` may already
    net out fee-vs-shares, making fees irrelevant to this app's arithmetic
    (a $100 position is $100, full stop). This function only does the
    subtraction and comparison; it does not fetch anything and it is not
    itself the safety gate that keeps this real-money section opt-in --  see
    ``tools/probe_live_2026-09-11.py``'s ``section_e2`` for that.
    """
    cash_delta = cash_before - cash_after
    cash_delta_matches_order_amount = abs(cash_delta - order_amount_usd) <= cents_tolerance
    position_amount_matches_order_amount = (
        None
        if position_amount is None
        else abs(position_amount - order_amount_usd) <= cents_tolerance
    )

    if disclosed_fee is not None and disclosed_fee != 0.0:
        if cash_delta_matches_order_amount and position_amount_matches_order_amount:
            verdict = (
                "Fee was disclosed but cash moved by exactly the order amount and "
                "the position amount equals the order amount too -- the disclosed "
                "fee is not visible in either cash or the position; if a fee is "
                "genuinely charged it must be netted against something not "
                "observed here (e.g. real vs. displayed exposure), not confirmed "
                "hidden inside `amount` by this evidence alone."
            )
        elif cash_delta_matches_order_amount and position_amount_matches_order_amount is False:
            verdict = (
                "Fee was disclosed, cash moved by exactly the order amount, but "
                "the position amount is LESS than the order amount -- consistent "
                "with the operator's hypothesis: the fee appears to have been "
                "carved out of the position's own amount rather than charged "
                "against cash separately."
            )
        elif not cash_delta_matches_order_amount:
            verdict = (
                "Fee was disclosed and cash moved by MORE than the order amount -- "
                "the fee was charged separately against cash, not hidden inside "
                "the position amount. This contradicts the operator's hypothesis "
                "for this trade."
            )
        else:
            verdict = "Fee was disclosed; cash/position deltas do not fit a simple pattern -- inspect raw payloads."
    else:
        if cash_delta_matches_order_amount and position_amount_matches_order_amount:
            verdict = (
                "No fee was disclosed, cash moved by exactly the order amount, "
                "and the position amount equals the order amount -- consistent "
                "with a genuinely fee-free ETF trade; no evidence a fee exists "
                "anywhere in this order."
            )
        else:
            verdict = (
                "No fee was disclosed but the cash/position deltas do not match "
                "the order amount cleanly -- something moved money this evidence "
                "does not explain; inspect raw payloads before concluding "
                "anything."
            )

    return FeeReconciliation(
        cash_before=cash_before,
        cash_after=cash_after,
        order_amount_usd=order_amount_usd,
        disclosed_fee=disclosed_fee,
        position_amount=position_amount,
        cash_delta=cash_delta,
        expected_cash_delta_if_no_fee=order_amount_usd,
        cash_delta_matches_order_amount=cash_delta_matches_order_amount,
        position_amount_matches_order_amount=position_amount_matches_order_amount,
        verdict=verdict,
    )
