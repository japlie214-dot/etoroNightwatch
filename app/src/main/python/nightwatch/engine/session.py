"""Daily session orchestration (doc/DESIGN_session.md §5).

S6c-2: preflight, SPY signal, phase A dry-run decision and portfolio snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import logging
import time
from typing import Callable, Mapping, Sequence

import httpx

from ..config import Config, TradingMode
from ..db.repo import Decision, PortfolioSnapshot, PriceWindowFingerprint, Repo, SnapshotPosition, utcnow
from ..etoro.client import EtoroClient
from ..etoro.models import Balance, Candle, Position, Rate
from ..host import Host, get_host
from ..market.calendar import next_close_minus, session_close_datetime
from ..market.source import IexQuote, TiingoClient
from ..market.window import AssembledSignal, WindowAssemblyError, assemble_spy_signal, resolve_provisional_close
from ..notify.telegram import TelegramNotifier
from ..strategy.ladder import decide, LadderConfig, LadderLot, ReentryBucket, LadderInputs, LadderIntent
from ..strategy.state import load, save
from .executor import ExecutionReport, RecoveryReport, execute_buys, recover_unfinished
from .limits import apply_submission_limits
from .halt import write_halt
from .interval_pnl import ClosedLot, IntervalResult, _utc, compute_interval
from .preflight import (
    GateResult, GateSpec, etoro_reachability_probe, gate_1_halt,
    gate_2_power, gate_3_network, gate_4_clock, gate_5_database,
    gate_6_auth, gate_7_environment, gate_8_instruments, gate_9_calendar,
    gate_10_price_data, gate_11_reconciliation, gate_12_risk_caps, run_preflight,
    ProvisionalClose,
)


@dataclass(frozen=True)
class MarketInputs:
    iex: dict[str, IexQuote | None]
    rates: dict[str, Rate]
    candles: dict[str, Candle | None]
    minute_candles: dict[str, Candle | None]
    provisional_closes: dict[str, ProvisionalClose]
    instrument_ids: dict[str, int]
    errors: dict[str, str]


def fetch_market_inputs(
    *, tiingo: TiingoClient, client: EtoroClient, symbols: Sequence[str],
) -> MarketInputs:
    ids = {s: client.resolve_instrument_id(s) for s in symbols}
    rates_by_id = client.get_rates(list(ids.values()))
    rates = {s: rates_by_id[i] for s, i in ids.items() if i in rates_by_id}
    candles = {s: (client.get_candles(ids[s], count=1) or [None])[-1] for s in symbols}
    minute_candles = {s: (client.get_candles(ids[s], count=1, interval="OneMinute") or [None])[-1] for s in symbols}
    try:
        quotes = tiingo.get_iex_quotes(list(symbols))
    except Exception as exc:
        logging.getLogger(__name__).warning("Tiingo IEX unavailable; using candle fallback: %s", exc)
        quotes = {}
    provisional_closes = {}
    errors = {}
    for s in symbols:
        try:
            provisional_closes[s] = resolve_provisional_close(
                s, iex_quote=quotes.get(s), etoro_daily_candle=candles[s],
            )
        except WindowAssemblyError as exc:
            errors[s] = f"{s}: {exc}"
    return MarketInputs({s: quotes.get(s) for s in symbols}, rates, candles, minute_candles, provisional_closes, ids, errors)


def _sleep_until(target: datetime, *, host: Host | None = None) -> None:
    host = host or get_host()
    while (remaining := (target - host.now()).total_seconds()) > 0:
        time.sleep(min(60, remaining))


@dataclass(frozen=True)
class SessionResult:
    session_id: int | None
    status: str
    failed_gate: int | None
    reason: str
    interval: "IntervalResult | None" = None


def fetch_server_date(base_url: str, *, timeout: float = 10.0) -> str | None:
    try:
        response = httpx.get(base_url + "/", timeout=timeout, follow_redirects=False)
    except httpx.HTTPError:
        return None
    return response.headers.get("date")


def build_preflight_gates(
    *, host: Host, config: Config, repo: Repo, client: EtoroClient, tiingo: TiingoClient,
    session_id: int, trading_date: date, now: Callable[[], datetime] | None = None,
    recover: Callable[[], RecoveryReport] | None = None,
    recovery_reports: list[RecoveryReport] | None = None,
) -> list[GateSpec]:
    now = now or host.now
    positions: list[list[Position] | None] = [None]

    def price_data() -> GateResult:
        mi = fetch_market_inputs(tiingo=tiingo, client=client, symbols=config.symbols.all)
        if mi.errors:
            return GateResult(10, "price_data", False, "no provisional close: " + "; ".join(mi.errors.values()), {"errors": mi.errors})
        return gate_10_price_data(
            now=now(), managed_symbols=config.symbols.all,
            provisional_closes=mi.provisional_closes, rates=mi.rates,
            candles={k: v for k, v in mi.candles.items() if v is not None},
            minute_candles=mi.minute_candles,
            config=config.market_data,
        )

    def reconciliation() -> GateResult:
        if recover is not None:
            try:
                report = recover()
            except Exception as exc:
                return GateResult(11, "Reconciliation", False, f"recovery: {type(exc).__name__}: {exc}")
            if recovery_reports is not None:
                recovery_reports.append(report)
        positions[0] = client.get_portfolio()
        instrument_symbols = {i.instrument_id: i.symbol for i in repo.list_instruments()}
        return gate_11_reconciliation(
            positions[0], repo.list_lot_annotations(),
            managed_symbols=config.symbols.traded, instrument_symbols=instrument_symbols,
            abandoned_intents=repo.abandoned_intents(),
            reconciliation_config=config.reconciliation,
        )

    return [
        GateSpec(1, "Halt switch", lambda: gate_1_halt(host)),
        GateSpec(2, "power", lambda: gate_2_power(
            status=host.power_status(), min_battery_percent=config.risk.min_battery_percent,
        )),
        GateSpec(3, "network", lambda: gate_3_network(
            probe=etoro_reachability_probe(config.etoro.base_url),
        )),
        GateSpec(4, "Clock", lambda: gate_4_clock(
            local_now=now(), server_date=fetch_server_date(config.etoro.base_url),
        )),
        GateSpec(5, "database", lambda: gate_5_database(repo=repo, session_id=session_id)),
        GateSpec(6, "auth", lambda: gate_6_auth(client=client)),
        GateSpec(7, "environment", lambda: gate_7_environment(client=client)),
        # Preflight checks the minimum; S7 must re-check each actual amount
        # with check_eligibility_for_order before execution.
        GateSpec(8, "instruments", lambda: gate_8_instruments(
            client=client, symbols=config.symbols,
            order_sizes={s: config.strategy.min_order_size for s in config.symbols.traded},
        )),
        GateSpec(9, "calendar", lambda: gate_9_calendar(trading_date=trading_date)),
        GateSpec(10, "price_data", price_data),
        GateSpec(11, "Reconciliation", reconciliation),
        GateSpec(12, "risk_caps", lambda: gate_12_risk_caps(
            risk=config.risk, repo=repo,
            positions=positions[0] if positions[0] is not None else client.get_portfolio(),
        )),
    ]


class DecideInputError(Exception):
    pass


class SnapshotError(Exception):
    pass


def build_snapshot(
    *, session_id: int, taken_at: str, pnl: Mapping, available_cash: float | None,
    symbol_by_instrument: Mapping[int, str], session_prices: Mapping[str, float],
    sleeve: Mapping[str, float], phase: str = "post_decision",
) -> PortfolioSnapshot:
    if "clientPortfolio" not in pnl:
        raise SnapshotError("snapshot refused: pnl has no clientPortfolio")
    cp = pnl["clientPortfolio"]
    positions = []
    missing = []
    mark_check = []
    checked = set()
    for p in cp["positions"]:
        u = p.get("unrealizedPnL") or {}
        position = SnapshotPosition(
            position_id=str(p["positionID"]), instrument_id=p.get("instrumentID"),
            symbol=symbol_by_instrument.get(p.get("instrumentID")), units=p.get("units"),
            entry_price=p.get("openRate"), current_price=u.get("closeRate"),
            amount=p.get("amount"), leverage=p.get("leverage"),
            unrealized_pnl=u.get("pnL"), open_date=p.get("openDateTime"),
        )
        positions.append(position)
        if position.current_price is None or position.unrealized_pnl is None or position.amount is None:
            missing.append(position.position_id)
        s = position.symbol
        if s is not None and position.instrument_id not in checked:
            price = session_prices.get(s)
            broker = position.current_price
            mark_check.append({"symbol": s, "session_price": price, "broker_close_rate": broker,
                               "diff_pct": None if broker is None or price is None or price == 0
                               else round(100 * (broker - price) / price, 3)})
            checked.add(position.instrument_id)
    complete = not missing and cp.get("accountCurrencyId") == 1
    cash = cp["credit"]
    return PortfolioSnapshot(
        session_id=session_id, taken_at=taken_at, cash=cash, available_cash=available_cash,
        equity=cash + sum(p.amount + p.unrealized_pnl for p in positions) if complete else None,
        unrealized_pnl=cp.get("unrealizedPnL"), realized_pnl=None, positions=positions,
        raw={"version": 1, "phase": phase, "source": "pnl", "complete": complete,
             "missing": missing, "account_currency_id": cp.get("accountCurrencyId"),
             "managed_instrument_ids": sorted(symbol_by_instrument), "sleeve": dict(sleeve),
             "mark_check": mark_check, "pnl": pnl},
    )


def build_ladder_inputs(
    *, iso: str, year: int, signal: AssembledSignal, prices: Mapping[str, float],
    positions: Sequence[Position], symbol_by_instrument: Mapping[int, str],
    balance: Balance, repo: Repo, cash_reserve_usd: float,
) -> tuple[LadderInputs, list[str]]:
    notes = []
    managed = {}
    unmanaged = 0
    for p in positions:
        if p.instrument_id not in symbol_by_instrument:
            unmanaged += 1
            continue
        if not p.is_buy:
            notes.append(f"ignored short position {p.position_id}")
            continue
        managed[p.position_id] = p
    if unmanaged:
        notes.append(f"{unmanaged} position(s) in unmanaged instruments ignored")
    cash = max(0.0, balance.available_cash - cash_reserve_usd)
    schg_value = sum(p.units * prices["SCHG"] for p in managed.values()
                     if symbol_by_instrument[p.instrument_id] == "SCHG")
    lots = []
    for a in repo.list_lot_annotations(is_open=True):
        if a.symbol != "UPRO":
            continue
        if a.entry_reference_spy is None:
            raise DecideInputError(f"position {a.position_id} has no entry_reference_spy")
        p = managed.get(a.position_id)
        if p is None:
            raise DecideInputError(f"position {a.position_id} missing at broker")
        lots.append(LadderLot(
            symbol="UPRO", book=a.book, units=p.units, entry_price=p.entry_price,
            cost_basis=p.amount, entry_date=a.entry_date,
            entry_reference_spy=a.entry_reference_spy, entry_spy_ath=a.entry_spy_ath,
            entry_spy_close=a.entry_spy_close, entry_spy_drawdown=a.entry_spy_drawdown,
            entry_tier=a.entry_tier, bucket_id=a.bucket_id, position_ref=a.position_id,
        ))
    annotated = {lot.position_ref for lot in lots}
    unannotated = sum(1 for p in managed.values()
                      if symbol_by_instrument[p.instrument_id] == "UPRO" and p.position_id not in annotated)
    if unannotated:
        notes.append(f"{unannotated} UPRO position(s) not annotated (adoption is S4): excluded from lots and equity")
    buckets = []
    for b in repo.active_buckets():
        if b.last_reference_spy is None:
            raise DecideInputError(f"bucket {b.bucket_id} has no last_reference_spy")
        buckets.append(ReentryBucket(
            bucket_id=b.bucket_id, sale_date=b.sale_date, sale_spy_close=b.sale_spy_close,
            proceeds=b.proceeds, tranche_amount=b.tranche_amount, created_bar=b.created_bar,
            tranches_bought=b.tranches_bought, last_reference_spy=b.last_reference_spy,
        ))
    mean = repo.mean_schg_value(year - 1)
    if mean is None:
        notes.append("no prior-year SCHG values")
    return LadderInputs(
        trading_date=iso, year=year, spy=signal.spy_context, cone_z=signal.cone_5_z,
        upro_price=prices["UPRO"], schg_price=prices["SCHG"], cash=cash,
        equity=cash + sum(lot.units * prices["UPRO"] for lot in lots) + schg_value,
        schg_position_value=schg_value, open_lots=lots, buckets=buckets,
        prior_year_schg_values=() if mean is None else (mean,),
    ), notes


def arming_refusals(config: Config) -> list[str]:
    refusals = []
    if config.mode is not TradingMode.LIVE:
        refusals.append(f"mode is {config.mode.value}, not live")
    if not config.arm_flag_present:
        refusals.append("NIGHTWATCH_ARM not set")
    names = [name for name in ("max_notional_per_session_usd", "max_daily_loss_usd", "max_open_lots")
             if getattr(config.risk, name) is None]
    if names:
        refusals.append(f"risk limits unset: {', '.join(names)}")
    return refusals


def intent_to_dict(i: LadderIntent) -> dict:
    return {"symbol": i.symbol, "side": i.side, "amount": i.amount,
            "position_ref": i.position_ref, "reason": i.reason, "book": i.book}


def _interval_for(prev, snap, *, client, managed_ids, config, today) -> IntervalResult:
    closed = []
    if prev is not None:
        try:
            rows = client.get_trade_history(start_date=prev.taken_at[:10], end_date=today.isoformat())
        except Exception:
            closed = None
        else:
            seen = set()
            for r in rows:
                if r.instrument_id not in managed_ids:
                    continue
                if r.close_date is not None and _utc(r.close_date) <= _utc(prev.taken_at):
                    continue
                if r.position_id in seen:
                    continue
                seen.add(r.position_id)
                if r.close_date is None:
                    closed = None
                    break
                closed.append(ClosedLot(str(r.position_id), r.instrument_id, r.units,
                                        r.profit, r.fees, r.open_date, r.close_date))
    return compute_interval(
        prev, snap, managed_ids=managed_ids, closed=closed, entry_marks={}, exit_marks={},
        loss_alert_usd=config.risk.max_daily_loss_usd if config.risk.max_daily_loss_usd is not None else float("inf"),
        residual_halt_usd=config.risk.max_unexplained_loss_usd if config.risk.max_unexplained_loss_usd is not None else float("inf"),
    )


def format_interval(r: IntervalResult) -> str:
    if r.state == "no_baseline":
        return "interval: no baseline (first snapshot)"
    pnl = "n/a" if r.pnl_usd is None else f"${r.pnl_usd:,.2f}"
    pct = "n/a" if r.pnl_pct is None else f"{r.pnl_pct:.2f}%"
    res = "n/a" if r.residual_usd is None else f"${r.residual_usd:,.2f}"
    text = f"interval: {r.state}, P&L {pnl}, {pct}, residual {res}"
    if r.reasons:
        text += "; reasons: " + " | ".join(r.reasons[:5])
        if len(r.reasons) > 5:
            text += f" (+{len(r.reasons) - 5} more)"
    return text


def _run_session_core(
    *, host: Host, config: Config, repo: Repo, client: EtoroClient, trading_date: date,
    tiingo: TiingoClient, wait: bool = True,
    sleep_until: Callable[[datetime], None] = _sleep_until,
) -> SessionResult:
    """Run preflight and phase A, executing SCHG opens only when armed.

    Construct the client with repo=repo so gate 8 caches missing instrument
    rows before gate 11 reads them, and role="write" when armed.
    """
    armed = config.is_armed
    if armed:
        refusals = arming_refusals(config) + (
            ["risk.max_orders_per_session unset"] if config.risk.max_orders_per_session is None else []
        )
        if refusals:
            return SessionResult(None, "refused_armed", None, "armed but refused: " + "; ".join(refusals))
    iso = trading_date.isoformat()
    existing = repo.get_session_by_date(iso)
    if existing is not None:
        return SessionResult(
            existing.id, "already_ran", None,
            f"session {existing.id} already exists for {iso} (state {existing.state})",
        )
    s = repo.create_session(
        iso, mode="live" if armed else "dry_run", environment=config.etoro.environment.value,
        armed=armed, state="preflight",
    )
    recovery_reports = []
    recovery_text = ""
    halt_error = None
    halt_text = ""

    def halt_for_tp(items: Sequence[str]) -> None:
        nonlocal halt_error, halt_text
        if not items:
            return
        text = "take-profit not cleared on position(s) " + ", ".join(items)
        try:
            write_halt(host, reason=text, source="session")
            halt_text += "; " + text + "; HALT set"
        except Exception as exc:
            halt_text += f"; {text}; HALT persistence failed: {type(exc).__name__}: {exc}"
            halt_error = str(exc)

    def abort(status: str, reason: str, gate: int | None = None) -> SessionResult:
        reason += recovery_text + halt_text
        repo.finish_session(s.id, state="aborted", outcome=reason, error=halt_error)
        return SessionResult(s.id, status, gate, reason)

    outcome = run_preflight(build_preflight_gates(
        host=host, config=config, repo=repo, client=client, tiingo=tiingo,
        session_id=s.id, trading_date=trading_date,
        recover=(lambda: recover_unfinished(
            repo=repo, client=client,
            abandon_after_sessions=config.reconciliation.non_terminal_order_sessions_before_abandoned,
        )) if armed else None,
        recovery_reports=recovery_reports,
    ), repo=repo, session_id=s.id)
    recovery = recovery_reports[0] if recovery_reports else None
    if recovery is not None:
        halt_for_tp(recovery.tp_failed)
        for name in ("resolved", "still_unresolved", "abandoned", "followups"):
            lines = getattr(recovery, name)
            recovery_text += f"; recovery {name} {len(lines)}"
            if lines:
                recovery_text += ": " + " | ".join(lines)
    failed = outcome.failed
    repo.update_session(
        s.id, preflight_verdict="pass" if outcome.passed else "fail",
        preflight_failed_gate=None if outcome.passed else failed.gate,
        preflight_at=utcnow(),
    )
    if not outcome.passed:
        reason = f"preflight gate {failed.gate} {failed.name}: {failed.reason}"
        return abort("preflight_failed", reason, failed.gate)
    t30 = session_close_datetime(trading_date) - timedelta(minutes=30)
    if wait and host.now() < t30:
        if sleep_until is _sleep_until:
            _sleep_until(t30, host=host)
        else:
            sleep_until(t30)
    mi = fetch_market_inputs(tiingo=tiingo, client=client, symbols=config.symbols.all)
    if mi.errors:
        g = GateResult(10, "price_data", False, "no provisional close: " + "; ".join(mi.errors.values()), {"errors": mi.errors})
    else:
        g = gate_10_price_data(
            now=host.now(), managed_symbols=config.symbols.all,
            provisional_closes=mi.provisional_closes, rates=mi.rates,
            candles={k: v for k, v in mi.candles.items() if v is not None},
            minute_candles=mi.minute_candles,
            config=config.market_data,
        )
    if not g.passed:
        reason = f"gate 10 rerun: {g.reason}"
        return abort("rerun_failed", reason, 10)
    athes = [a.entry_spy_ath for a in repo.list_lot_annotations(is_open=True) if a.entry_spy_ath is not None]
    try:
        sig = assemble_spy_signal(
            tiingo, trading_date=iso, iex_quote=mi.iex.get("SPY"),
            etoro_daily_candle=mi.candles.get("SPY"),
            cone_horizon=config.strategy.cone_horizon, open_lot_entry_spy_ath=athes,
        )
    except Exception as exc:
        reason = f"signal: {type(exc).__name__}: {exc}"
        return abort("signal_failed", reason)
    fp = sig.fingerprint
    repo.record_price_window_fingerprint(PriceWindowFingerprint(
        trading_date=fp.trading_date, symbol=fp.symbol, window_start=fp.window_start,
        window_end=fp.window_end, bar_count=fp.bar_count, series_sha256=fp.series_sha256,
        source=fp.source, source_fetched_at=fp.source_fetched_at,
    ))
    try:
        positions = client.get_portfolio()
        balance = client.get_balance()
    except Exception as exc:
        reason = f"decide inputs: {type(exc).__name__}: {exc}"
        return abort("decide_failed", reason)
    try:
        inputs, notes = build_ladder_inputs(
            iso=iso, year=trading_date.year, signal=sig,
            prices={s: mi.provisional_closes[s].value for s in mi.provisional_closes},
            positions=positions, symbol_by_instrument={v: k for k, v in mi.instrument_ids.items()},
            balance=balance, repo=repo, cash_reserve_usd=config.strategy.cash_reserve_usd,
        )
    except DecideInputError as exc:
        reason = f"decide inputs: {type(exc).__name__}: {exc}"
        return abort("decide_failed", reason)
    if config.strategy.budget_override_usd is not None:
        notes.append("budget_override_usd set but not honoured (ladder has no field)")
    try:
        state = load(repo)
        d = decide(inputs, state, LadderConfig.from_strategy_config(config.strategy))
    except Exception as exc:
        reason = f"decide inputs: {type(exc).__name__}: {exc}"
        return abort("decide_failed", reason)
    repo.insert_decision(Decision(
        session_id=s.id, trading_date=iso, intents=[intent_to_dict(i) for i in d.intents],
        cone_z=inputs.cone_z, spy_close=inputs.spy.close, spy_ath=inputs.spy.ath,
        drawdown=inputs.spy.drawdown, tier=inputs.spy.tier, budget=d.new_state.budget,
        order_size=d.new_state.order_size, cash_target=d.new_state.cash_target,
        equity=inputs.equity, cash=inputs.cash,
        reason="phase A, pre-settlement, buy intents provisional; dry run: " + d.reason,
    ))
    refusals = arming_refusals(config)
    groups = {}
    for i in d.intents:
        groups.setdefault((i.side, i.symbol), []).append(i.amount)
    summaries = []
    for (side, symbol), amounts in groups.items():
        n = len(amounts)
        if amounts[0] is None or (armed and any(a is None for a in amounts)):
            summaries.append(f"{side} {symbol} {n} lot(s)")
        else:
            total = sum(amounts)
            orders = f"{n}x ${amounts[0]:.2f}" if all(a == amounts[0] for a in amounts) else f"{n} orders"
            summaries.append(f"{side} {symbol} {orders} = ${total:.2f}")
    summary = "; ".join(summaries) or "none"
    status = "executed" if armed else "would_have_done"
    orders_sent = False
    reason = f"would have done {len(d.intents)} intent(s): {summary}; ladder: {d.reason}; arming refused: {'; '.join(refusals)}"
    if armed:
        reason = f"armed: {len(d.intents)} intent(s): {summary}; ladder: {d.reason}"
        if recovery is not None and recovery.tp_failed:
            status = "halted_before_execution"
            reason += "; execution skipped: take-profit not cleared at recovery"
            reason += "; strategy state not saved: take-profit not cleared at recovery"
        elif any(i.side != "open" or i.symbol != "SCHG" or i.amount is None or i.amount <= 0
                 for i in d.intents):
            status = "armed_refused_intents"
            reason = "armed: decision has non-SCHG-open intents, S7-3a refuses: " + summary
            reason += "; strategy state not saved: non-SCHG-open intents"
        else:
            accepted, dropped = apply_submission_limits(
                d.intents, cap_usd=config.risk.max_notional_per_session_usd,
                reserved_usd=repo.reserved_buy_usd(iso), cash_usd=inputs.cash,
                open_lots=len(repo.list_lot_annotations(is_open=True)),
                max_open_lots=config.risk.max_open_lots,
                min_order_usd={s: config.strategy.min_order_size for s in config.symbols.traded},
            )
            reason += f"; accepted {len(accepted)}, dropped {len(dropped)}"
            report = ExecutionReport((), None, (), (), ())
            execution_error = None
            orders_sent = bool(accepted)
            if accepted:
                try:
                    report = execute_buys(
                        repo=repo, client=client, session_id=s.id, trading_date=trading_date,
                        intents=accepted, instrument_ids=mi.instrument_ids, spy=inputs.spy,
                        max_orders=config.risk.max_orders_per_session,
                        deadline=session_close_datetime(trading_date) - timedelta(minutes=5),
                        now=host.now,
                    )
                except Exception as exc:
                    execution_error = f"execution error: {type(exc).__name__}: {exc}"
                    reason += "; " + execution_error
            for result in report.results:
                fill = "n/a" if result.fill_amount_usd is None else f"{result.fill_amount_usd:.2f}"
                reason += f"; {result.outcome} ${result.intent.amount:.2f} filled ${fill} {result.detail}"
            reason += f"; stopped={report.stopped}; deferred {len(report.deferred)}; followups {len(report.followups)}"
            if report.followups:
                reason += ": " + " | ".join(report.followups)
            unconfirmed = [f"order {r.order_id}" for r in report.results
                           if r.outcome == "filled" and r.position_id is None]
            halt_for_tp([*report.tp_failed, *unconfirmed])
            if execution_error is not None:
                not_saved = "execution exception"
            elif dropped:
                not_saved = "limits dropped intents"
            elif report.stopped is not None:
                not_saved = "execution stopped: " + report.stopped
            elif report.deferred:
                not_saved = "deferred intents"
            elif any(r.outcome != "filled" for r in report.results):
                not_saved = "non-filled result"
            elif len(report.results) != len(d.intents):
                not_saved = "result count differs from decision"
            elif any("partial fill" in r.detail for r in report.results):
                not_saved = "partial fill"
            elif any(r.position_id is None for r in report.results):
                not_saved = "filled result has no position id"
            else:
                not_saved = None
            if not_saved is None:
                save(repo, d.new_state)
            else:
                reason += "; strategy state not saved: " + not_saved
    reason += recovery_text + halt_text
    if notes:
        reason += f"; inputs: {'; '.join(notes)}"
    if d.warnings:
        reason += f"; warnings: {'; '.join(d.warnings)}"
    interval = None
    # No snapshot means no comparison and no interval-driven HALT.
    try:
        if orders_sent:
            balance = client.get_balance()
        prev = repo.latest_snapshot()
        pnl = client.get_pnl()
        snap = build_snapshot(
            session_id=s.id, taken_at=utcnow(), pnl=pnl, available_cash=balance.available_cash,
            symbol_by_instrument={v: k for k, v in mi.instrument_ids.items()},
            session_prices={s: mi.provisional_closes[s].value for s in mi.provisional_closes},
            sleeve={"cash_reserve_usd": config.strategy.cash_reserve_usd,
                    "sleeve_cash": inputs.cash, "sleeve_equity": inputs.equity,
                    "schg_position_value": inputs.schg_position_value},
            phase="post_execution" if armed else "post_decision",
        )
        sid = repo.insert_snapshot(snap)
        reason += f"; snapshot {sid}: {len(snap.positions)} position(s), equity={snap.equity}, complete={snap.raw['complete']}"
        interval = _interval_for(
            prev, snap, client=client,
            managed_ids=frozenset(mi.instrument_ids[s] for s in config.symbols.traded),
            config=config, today=trading_date,
        )
        reason += "; " + format_interval(interval)
        if interval.state == "integrity" or interval.alert_c:
            try:
                write_halt(host, reason=interval.reasons[0] if interval.reasons
                           else f"alert C residual ${interval.residual_usd:,.2f}", source="session")
                reason += "; HALT set"
            except Exception as e:
                reason += f"; HALT persistence failed: {type(e).__name__}: {e}"
                halt_error = str(e)
    except Exception as e:
        reason += f"; snapshot failed: {type(e).__name__}: {e}"
    repo.finish_session(s.id, state="finished", outcome=reason, error=halt_error)
    return SessionResult(s.id, status, None, reason, interval=interval)


def format_session_report(
    result: SessionResult, *, trading_date: date, environment: str, next_run_at: str,
) -> str:
    session = f"session {result.session_id}" if result.session_id is not None else "no session"
    gate = f" (gate {result.failed_gate})" if result.failed_gate is not None else ""
    reason = result.reason if result.reason is not None else "(no reason)"
    return (
        f"[{environment.upper()}] NIGHTWATCH {trading_date.isoformat()} {session}: {result.status}{gate}\n"
        f"{reason}\n"
        f"next run: {next_run_at}"
    )


def run_session(
    *, host: Host, config: Config, repo: Repo, client: EtoroClient, trading_date: date,
    tiingo: TiingoClient, wait: bool = True,
    sleep_until: Callable[[datetime], None] = _sleep_until,
    notifier: "TelegramNotifier | None" = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> SessionResult:
    if notifier is not None:
        try:
            from ..notify.poll import process_pending

            process_pending(notifier, repo, host, config)
        except Exception as exc:
            logging.getLogger(__name__).warning("Session command poll failed: %s", exc)
    result = _run_session_core(
        host=host, config=config, repo=repo, client=client, trading_date=trading_date,
        tiingo=tiingo, wait=wait, sleep_until=sleep_until,
    )
    if result.status == "already_ran":
        return result
    next_run = next_close_minus(45, after=now())
    next_iso = next_run.astimezone(timezone.utc).isoformat(timespec="seconds")
    if result.session_id is not None:
        try:
            repo.update_session(result.session_id, next_run_at=next_iso)
        except Exception as exc:
            logging.getLogger(__name__).warning("Session next_run_at update failed: %s", exc)
    if notifier is not None:
        try:
            text = format_session_report(
                result, trading_date=trading_date, environment=config.environment.value,
                next_run_at=next_iso,
            )
            notifier.send(
                text, event="session_report", session_id=result.session_id,
                trading_date=trading_date.isoformat(),
            )
        except Exception as exc:
            logging.getLogger(__name__).warning("Session report failed: %s", exc)
    if notifier is not None and "take-profit not cleared" in result.reason:
        failure = "HALT persistence failed: "
        halt = ("HALT persistence FAILED: " + result.reason.split(failure, 1)[1]
                if failure in result.reason else "HALT set")
        tail = (" Next session is NOT protected." if failure in result.reason
                else " Next session stops at gate 1 until you /resume.")
        text = f"[{config.environment.value.upper()}] TP NOT CLEARED — {halt}." + tail
        try:
            notifier.send(text, event="tp_not_cleared", session_id=result.session_id,
                          trading_date=trading_date.isoformat())
        except Exception as exc:
            logging.getLogger(__name__).warning("Session tp_not_cleared alert failed: %s", exc)
    if notifier is not None and result.interval is not None:
        r = result.interval
        env = config.environment.value.upper()
        pnl = "n/a" if r.pnl_usd is None else f"${r.pnl_usd:,.2f}"
        res = "n/a" if r.residual_usd is None else f"${r.residual_usd:,.2f}"
        messages = []
        if r.alert_a:
            messages.append(("alert_a", f"[{env}] ALERT A — UPRO+SCHG P&L {pnl} since last session (threshold -${config.risk.max_daily_loss_usd:,.0f}). Alert only, trading continues."))
        failure = "HALT persistence failed: "
        halt = ("HALT persistence FAILED: " + result.reason.split(failure, 1)[1]
                + ". Next session is NOT protected." if failure in result.reason
                else "HALT set.")
        next_session = " Next session stops at gate 1 until you /resume." if failure not in result.reason else ""
        if r.alert_c:
            messages.append(("alert_c", f"[{env}] ALERT C — {halt} Loss not explained by market: residual {res} (threshold -${config.risk.max_unexplained_loss_usd:,.0f})." + next_session))
        if r.state == "integrity":
            messages.append(("integrity", f"[{env}] INTEGRITY — {halt} " + " | ".join(r.reasons[:5]) + next_session))
        if r.state == "incomplete":
            messages.append(("interval_incomplete", f"[{env}] INCOMPLETE P&L check — no HALT. " + " | ".join(r.reasons[:5])))
        for event, text in messages:
            try:
                notifier.send(text, event=event, session_id=result.session_id,
                              trading_date=trading_date.isoformat())
            except Exception as exc:
                logging.getLogger(__name__).warning("Session %s alert failed: %s", event, exc)
    return result
