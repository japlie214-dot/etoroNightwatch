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
from ..strategy.state import load
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


def fetch_server_date(base_url: str, *, timeout: float = 10.0) -> str | None:
    try:
        response = httpx.get(base_url + "/", timeout=timeout, follow_redirects=False)
    except httpx.HTTPError:
        return None
    return response.headers.get("date")


def build_preflight_gates(
    *, host: Host, config: Config, repo: Repo, client: EtoroClient, tiingo: TiingoClient,
    session_id: int, trading_date: date, now: Callable[[], datetime] | None = None,
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
    sleeve: Mapping[str, float],
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
        raw={"version": 1, "phase": "post_decision", "source": "pnl", "complete": complete,
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


def _run_session_core(
    *, host: Host, config: Config, repo: Repo, client: EtoroClient, trading_date: date,
    tiingo: TiingoClient, wait: bool = True,
    sleep_until: Callable[[datetime], None] = _sleep_until,
) -> SessionResult:
    """Run dry-run preflight and phase A, stopping at the arm gate.

    Construct the client with role="read", repo=repo so gate 8 caches missing
    instrument rows before gate 11 reads them.
    """
    if config.is_armed:
        return SessionResult(None, "refused_armed", None, "armed execution not built (S7); refusing")
    iso = trading_date.isoformat()
    existing = repo.get_session_by_date(iso)
    if existing is not None:
        return SessionResult(
            existing.id, "already_ran", None,
            f"session {existing.id} already exists for {iso} (state {existing.state})",
        )
    s = repo.create_session(
        iso, mode="dry_run", environment=config.etoro.environment.value,
        armed=False, state="preflight",
    )
    outcome = run_preflight(build_preflight_gates(
        host=host, config=config, repo=repo, client=client, tiingo=tiingo,
        session_id=s.id, trading_date=trading_date,
    ), repo=repo, session_id=s.id)
    failed = outcome.failed
    repo.update_session(
        s.id, preflight_verdict="pass" if outcome.passed else "fail",
        preflight_failed_gate=None if outcome.passed else failed.gate,
        preflight_at=utcnow(),
    )
    if not outcome.passed:
        reason = f"preflight gate {failed.gate} {failed.name}: {failed.reason}"
        repo.finish_session(s.id, state="aborted", outcome=reason)
        return SessionResult(s.id, "preflight_failed", failed.gate, reason)
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
        repo.finish_session(s.id, state="aborted", outcome=reason)
        return SessionResult(s.id, "rerun_failed", 10, reason)
    athes = [a.entry_spy_ath for a in repo.list_lot_annotations(is_open=True) if a.entry_spy_ath is not None]
    try:
        sig = assemble_spy_signal(
            tiingo, trading_date=iso, iex_quote=mi.iex.get("SPY"),
            etoro_daily_candle=mi.candles.get("SPY"),
            cone_horizon=config.strategy.cone_horizon, open_lot_entry_spy_ath=athes,
        )
    except Exception as exc:
        reason = f"signal: {type(exc).__name__}: {exc}"
        repo.finish_session(s.id, state="aborted", outcome=reason)
        return SessionResult(s.id, "signal_failed", None, reason)
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
        repo.finish_session(s.id, state="aborted", outcome=reason)
        return SessionResult(s.id, "decide_failed", None, reason)
    try:
        inputs, notes = build_ladder_inputs(
            iso=iso, year=trading_date.year, signal=sig,
            prices={s: mi.provisional_closes[s].value for s in mi.provisional_closes},
            positions=positions, symbol_by_instrument={v: k for k, v in mi.instrument_ids.items()},
            balance=balance, repo=repo, cash_reserve_usd=config.strategy.cash_reserve_usd,
        )
    except DecideInputError as exc:
        reason = f"decide inputs: {type(exc).__name__}: {exc}"
        repo.finish_session(s.id, state="aborted", outcome=reason)
        return SessionResult(s.id, "decide_failed", None, reason)
    if config.strategy.budget_override_usd is not None:
        notes.append("budget_override_usd set but not honoured (ladder has no field)")
    try:
        state = load(repo)
        d = decide(inputs, state, LadderConfig.from_strategy_config(config.strategy))
    except Exception as exc:
        reason = f"decide inputs: {type(exc).__name__}: {exc}"
        repo.finish_session(s.id, state="aborted", outcome=reason)
        return SessionResult(s.id, "decide_failed", None, reason)
    repo.insert_decision(Decision(
        session_id=s.id, trading_date=iso, intents=[intent_to_dict(i) for i in d.intents],
        cone_z=inputs.cone_z, spy_close=inputs.spy.close, spy_ath=inputs.spy.ath,
        drawdown=inputs.spy.drawdown, tier=inputs.spy.tier, budget=d.new_state.budget,
        order_size=d.new_state.order_size, cash_target=d.new_state.cash_target,
        equity=inputs.equity, cash=inputs.cash,
        reason="phase A, pre-settlement, buy intents provisional; dry run: " + d.reason,
    ))
    refusals = arming_refusals(config)
    summary = "; ".join(f"{i.side} {i.symbol} ${i.amount:.2f}" if i.amount is not None
                        else f"{i.side} {i.symbol} lot {i.position_ref}" for i in d.intents) or "none"
    reason = f"would have done {len(d.intents)} intent(s): {summary}; ladder: {d.reason}; arming refused: {'; '.join(refusals)}"
    if notes:
        reason += f"; inputs: {'; '.join(notes)}"
    if d.warnings:
        reason += f"; warnings: {'; '.join(d.warnings)}"
    try:
        pnl = client.get_pnl()
        snap = build_snapshot(
            session_id=s.id, taken_at=utcnow(), pnl=pnl, available_cash=balance.available_cash,
            symbol_by_instrument={v: k for k, v in mi.instrument_ids.items()},
            session_prices={s: mi.provisional_closes[s].value for s in mi.provisional_closes},
            sleeve={"cash_reserve_usd": config.strategy.cash_reserve_usd,
                    "sleeve_cash": inputs.cash, "sleeve_equity": inputs.equity,
                    "schg_position_value": inputs.schg_position_value},
        )
        sid = repo.insert_snapshot(snap)
        reason += f"; snapshot {sid}: {len(snap.positions)} position(s), equity={snap.equity}, complete={snap.raw['complete']}"
    except Exception as e:
        reason += f"; snapshot failed: {type(e).__name__}: {e}"
    repo.finish_session(s.id, state="finished", outcome=reason)
    return SessionResult(s.id, "would_have_done", None, reason)


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
    return result
