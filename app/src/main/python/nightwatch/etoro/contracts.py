"""Small stdlib-only checker for observed eToro response contracts.

Contracts describe response shapes, not exact key sets: new broker fields are
accepted, while missing paths, type changes, and known-shape violations are
reported. Call sites decide whether a report is fatal or advisory.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True, slots=True)
class FieldRule:
    path: str
    kind: str
    required: bool = True
    one_of: frozenset[str | int] = frozenset()
    minimum: float | None = None
    non_empty: bool = False


@dataclass(frozen=True, slots=True)
class Contract:
    name: str
    fields: tuple[FieldRule, ...]
    evidence: str
    gaps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Violation:
    kind: str  # missing | wrong_type | unexpected_shape
    path: str
    expected: str
    actual: str


@dataclass(frozen=True, slots=True)
class ContractReport:
    contract: str
    complete: bool
    violations: tuple[Violation, ...]


def _f(
    path: str,
    kind: str,
    *,
    optional: bool = False,
    one_of: tuple[str | int, ...] = (),
    minimum: float | None = None,
    non_empty: bool = False,
) -> FieldRule:
    return FieldRule(path, kind, not optional, frozenset(one_of), minimum, non_empty)


_ORDER_STATUSES = (
    "Received", "Placed", "Filled", "Rejected", "PartiallyFilled",
    "PendingCancel", "Canceled", "Expired", "CanceledPartiallyFilled",
    "RejectedPartiallyFilled", "WaitingForMarket",
)


CONTRACTS: dict[str, Contract] = {
    "balance": Contract("balance", (
        _f("$", "object"), _f("$.gcid", "integer"), _f("$.totalBalance", "number"),
        _f("$.displayCurrency", "string"), _f("$.balances", "array"),
        _f("$.balances[*]", "object"), _f("$.balances[*].accountId", "string"),
        _f("$.balances[*].accountType", "string"), _f("$.balances[*].balance", "number"),
        _f("$.balances[*].currency", "string"),
    ), "Sep 11 real and demo /balances bodies."),
    "portfolio-envelope": Contract("portfolio-envelope", (
        _f("$", "object"), _f("$.clientPortfolio", "object"),
        _f("$.clientPortfolio.positions", "array"),
        _f("$.clientPortfolio.positions[*]", "object"),
        _f("$.clientPortfolio.positions[*].positionID", "integer", minimum=1),
        _f("$.clientPortfolio.positions[*].instrumentID", "integer", minimum=1),
        _f("$.clientPortfolio.positions[*].units", "number", minimum=0),
        _f("$.clientPortfolio.positions[*].openRate", "number", minimum=0),
        _f("$.clientPortfolio.positions[*].amount", "number", minimum=0),
        *tuple(_f(f"$.clientPortfolio.{name}", "array") for name in (
            "mirrors", "orders", "stockOrders", "entryOrders", "exitOrders",
            "ordersForOpen", "ordersForClose", "ordersForCloseMultiple",
        )),
        _f("$.clientPortfolio.credit", "number"),
    ), "Sep 11 real and demo portfolio bodies.", (
        "orders and ordersForOpen were empty in all captured portfolios; row schemas are unobserved.",
    )),
    "pnl": Contract("pnl", (
        _f("$", "object"), _f("$.clientPortfolio", "object"),
        _f("$.clientPortfolio.positions", "array"),
        _f("$.clientPortfolio.positions[*]", "object"),
        _f("$.clientPortfolio.unrealizedPnL", "number"),
        _f("$.clientPortfolio.accountCurrencyId", "number"),
        _f("$.clientPortfolio.credit", "number"),
        *tuple(_f(f"$.clientPortfolio.{name}", "array") for name in (
            "mirrors", "orders", "stockOrders", "entryOrders", "exitOrders",
            "ordersForOpen", "ordersForClose", "ordersForCloseMultiple",
        )),
    ), "Sep 11 real and demo PnL bodies.", (
        "orders and ordersForOpen were empty in all captured portfolios; row schemas are unobserved.",
    )),
    "candle": Contract("candle", (
        _f("$", "object"), _f("$.interval", "string", non_empty=True),
        _f("$.candles", "array"), _f("$.candles[*]", "object"),
        _f("$.candles[*].instrumentId", "integer", minimum=1),
        _f("$.candles[*].candles", "array"),
        _f("$.candles[*].candles[*]", "object"),
        *tuple(_f(f"$.candles[*].candles[*].{name}", "number") for name in (
            "open", "high", "low", "close",
        )),
        _f("$.candles[*].candles[*].fromDate", "string", optional=True, non_empty=True),
        _f("$.candles[*].candles[*].date", "string", optional=True, non_empty=True),
        _f("$.candles[*].candles[*].volume", "number", optional=True),
    ), "Sep 11 candles response."),
    "rates": Contract("rates", (
        _f("$", "object"), _f("$.rates", "array"), _f("$.rates[*]", "object"),
        _f("$.rates[*].instrumentID", "integer", minimum=1),
        *tuple(_f(f"$.rates[*].{name}", "number") for name in ("bid", "ask", "lastExecution")),
        _f("$.rates[*].date", "string", non_empty=True),
    ), "Sep 11 rates response."),
    "eligibility": Contract("eligibility", (
        _f("$", "object"), _f("$.currency", "string"),
        _f("$.eligibilities", "array"), _f("$.eligibilities[*]", "object"),
        _f("$.eligibilities[*].instrumentId", "integer", minimum=1),
        _f("$.eligibilities[*].symbol", "string", non_empty=True),
        _f("$.eligibilities[*].allowOpenPosition", "bool"),
        _f("$.eligibilities[*].allowPartialClosePosition", "bool"),
        _f("$.eligibilities[*].minPositionExposure", "number", minimum=0),
        _f("$.eligibilities[*].leverageConfigs", "array"),
        _f("$.eligibilities[*].leverageConfigs[*]", "object"),
        _f("$.notFoundInstrumentIds", "array"), _f("$.notFoundSymbols", "array"),
    ), "Sep 11 eligibility response."),
    "instruments": Contract("instruments", (
        _f("$", "object"), _f("$.instrumentDisplayDatas", "array"),
        _f("$.instrumentDisplayDatas[*]", "object"),
        _f("$.instrumentDisplayDatas[*].instrumentID", "integer", minimum=1),
        _f("$.instrumentDisplayDatas[*].instrumentDisplayName", "string", non_empty=True),
        _f("$.instrumentDisplayDatas[*].symbolFull", "string", non_empty=True),
    ), "Sep 11 instruments response."),
    "costs": Contract("costs", (
        _f("$", "object"), _f("$.instrumentId", "integer", minimum=1),
        _f("$.symbol", "string", non_empty=True), _f("$.costs", "array"),
        _f("$.costs[*]", "object"), _f("$.costs[*].costType", "string"),
        _f("$.costs[*].currency", "string"), _f("$.costs[*].value", "number"),
        _f("$.lastUpdated", "string", non_empty=True),
    ), "Sep 11 costs response."),
    "trade-history": Contract("trade-history", (
        _f("$", "array"), _f("$[*]", "object"),
        *tuple(_f(f"$[*].{name}", kind) for name, kind in (
            ("positionId", "integer"), ("instrumentId", "integer"), ("units", "number"),
            ("isBuy", "bool"), ("leverage", "number"), ("netProfit", "number"),
            ("investment", "number"), ("fees", "number"),
            ("openTimestamp", "string"), ("closeTimestamp", "string"),
        )),
    ), "Sep 11 captured trade-history row raw payload."),
    "order-lookup": Contract("order-lookup", (
        _f("$", "object"), _f("$.orderId", "integer", minimum=1),
        _f("$.status", "object"), _f("$.status.id", "integer", one_of=tuple(range(1, 12))),
        _f("$.status.name", "string", one_of=_ORDER_STATUSES),
        _f("$.asset", "object"), _f("$.asset.instrumentId", "integer", minimum=1),
        _f("$.positionExecutions", "array"),
        _f("$.positionExecutions[*]", "object"),
        _f("$.positionExecutions[*].positionId", "integer", minimum=1),
        _f("$.positionExecutions[*].openingData", "object", optional=True),
        _f("$.positionExecutions[*].openingData.units", "number", optional=True, minimum=0),
        _f("$.positionExecutions[*].openingData.avgPrice", "number", optional=True, minimum=0),
        _f("$.requestedUnits", "number", optional=True, minimum=0),
    ), "Sep 11 orders:lookup response; only Filled was captured.", (
        "Only status name Filled was captured; other names mirror the current local status enum.",
    )),
    "legacy-order-lookup": Contract("legacy-order-lookup", (
        _f("$", "object"), _f("$.orderID", "integer", minimum=1),
        _f("$.statusID", "integer", one_of=tuple(range(1, 12))),
        _f("$.instrumentID", "integer", minimum=1), _f("$.positions", "array"),
        _f("$.positions[*]", "object"),
        _f("$.positions[*].positionID", "integer", minimum=1),
        _f("$.positions[*].rate", "number", optional=True, minimum=0),
        _f("$.positions[*].units", "number", optional=True, minimum=0),
    ), "One sanitized Sep 18 demo read from the deprecated v1 order-ID endpoint; only Filled observed.", (
        "This is a demo-only fallback after v2 404; other status variants remain unobserved.",
    )),
    "close-response": Contract("close-response", (
        _f("$", "object"), _f("$.orderForClose", "object"),
        _f("$.orderForClose.positionID", "integer", minimum=1),
        _f("$.orderForClose.instrumentID", "integer", minimum=1),
        _f("$.orderForClose.orderID", "integer", minimum=1),
        _f("$.orderForClose.statusID", "integer", minimum=1),
    ), "Sep 11 close POST response; token removed from fixture.", (
        "No GET close-order status response was captured.",
    )),
    # Declared so adding a parser still requires a contract, without claiming
    # ground truth for response variants absent from the Sep 11 capture set.
    "market-search-unobserved": Contract("market-search-unobserved", (),
        "No raw /market-data/search response is present in the fixture set.",
        ("No captured search response; wait for T7 before defining field paths.",)),
    "close-order-status-unobserved": Contract("close-order-status-unobserved", (),
        "No GET close-orders/{orderId} response was captured.",
        ("No captured dedicated close-order status response.",)),
    "order-write-unobserved": Contract("order-write-unobserved", (),
        "No order POST response contract fixture was included.",
        ("Only lookup and close POST bodies were captured.",)),
}


# The AST coverage test maps every raw-response parser/helper to an observed
# or explicitly unobserved contract. Adding a parser requires an entry here.
PARSER_CONTRACTS: dict[str, str] = {
    "EtoroClient.get_balance": "portfolio-envelope",
    "EtoroClient._detect_environment": "balance",
    "EtoroClient._detect_environment_in": "balance",
    "EtoroClient._fetch_portfolio_envelope": "portfolio-envelope",
    "EtoroClient.get_portfolio": "portfolio-envelope",
    "EtoroClient.get_portfolio_strict": "portfolio-envelope",
    "EtoroClient.get_pnl": "pnl",
    "EtoroClient._parse_position": "portfolio-envelope",
    "EtoroClient.get_trade_history": "trade-history",
    "EtoroClient._closed_on_or_before": "trade-history",
    "EtoroClient._parse_trade_history_entry": "trade-history",
    "EtoroClient.search_instrument": "market-search-unobserved",
    "EtoroClient.get_instruments": "instruments",
    "EtoroClient.get_rates": "rates",
    "EtoroClient.get_candles": "candle",
    "EtoroClient._parse_candle": "candle",
    "EtoroClient.get_eligibility": "eligibility",
    "EtoroClient._min_position_amount_for_real_long": "eligibility",
    "EtoroClient.get_costs": "costs",
    "EtoroClient._order_id_from_write_response": "order-write-unobserved",
    "EtoroClient.lookup_order": "order-lookup",
    "EtoroClient.lookup_close_order": "close-order-status-unobserved",
    "EtoroClient._parse_close_order_result": "close-order-status-unobserved",
    "EtoroClient._execution_rows": "order-lookup",
    "EtoroClient._id_string": "order-lookup",
    "EtoroClient._first_float": "order-lookup",
    "EtoroClient._parse_order_result": "order-lookup",
    "compute_available_cash": "portfolio-envelope",
}


_TOKEN = re.compile(r"\[\*\]|[^.\[\]]+")


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _resolve(payload: Any, path: str) -> tuple[list[tuple[Any, str]], list[str]]:
    if path == "$":
        return [(payload, "$")], []
    tokens = _TOKEN.findall(path[1:])
    current = [(payload, "$")]
    missing: list[str] = []
    for token in tokens:
        next_values: list[tuple[Any, str]] = []
        if token == "[*]":
            for value, concrete in current:
                if isinstance(value, list):
                    next_values.extend((item, f"{concrete}[{i}]") for i, item in enumerate(value))
            current = next_values
            continue
        for value, concrete in current:
            if not isinstance(value, Mapping):
                continue  # The parent's own rule reports a type change.
            if token in value:
                next_values.append((value[token], f"{concrete}.{token}"))
            else:
                missing.append(f"{concrete}.{token}")
        current = next_values
    return current, missing


def check_payload(
    contract_name: str,
    payload: Any,
    *,
    on_violation: Callable[[ContractReport], None] | None = None,
) -> ContractReport:
    """Check known paths and optionally emit one alert-ready report.

    Reports do not carry raw values, preventing account data from leaking into
    notification text. Unknown/additive keys are intentionally ignored.
    """
    contract = CONTRACTS[contract_name]
    violations: list[Violation] = []
    for rule in contract.fields:
        matches, missing_paths = _resolve(payload, rule.path)
        if rule.required:
            violations.extend(
                Violation("missing", path, rule.kind, "missing") for path in missing_paths
            )
        if not matches:
            if rule.required and not missing_paths and "[*]" not in rule.path:
                violations.append(Violation("missing", rule.path, rule.kind, "missing"))
            continue
        for value, concrete in matches:
            actual = _kind(value)
            correct_type = actual == rule.kind or rule.kind == "number" and actual == "integer"
            if not correct_type:
                violations.append(Violation("wrong_type", concrete, rule.kind, actual))
                continue
            if rule.one_of and value not in rule.one_of:
                violations.append(Violation(
                    "unexpected_shape", concrete, f"one of {sorted(rule.one_of, key=str)!r}", actual
                ))
            elif rule.non_empty and isinstance(value, str) and not value.strip():
                violations.append(Violation("unexpected_shape", concrete, "non-empty string", actual))
            elif rule.minimum is not None and isinstance(value, (int, float)) and value < rule.minimum:
                violations.append(Violation("unexpected_shape", concrete, f">= {rule.minimum}", actual))
            elif isinstance(value, float) and not math.isfinite(value):
                violations.append(Violation("unexpected_shape", concrete, "finite number", actual))
    report = ContractReport(contract.name, not contract.gaps, tuple(violations))
    if report.violations and on_violation is not None:
        on_violation(report)
    return report
