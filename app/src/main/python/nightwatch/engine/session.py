"""Daily session orchestration (doc/DESIGN_session.md §5).

S6b: preflight, T-30 market rerun and SPY signal assembly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
import time
from typing import Callable, Sequence

import httpx

from ..config import Config
from ..db.repo import PriceWindowFingerprint, Repo, utcnow
from ..etoro.client import EtoroClient
from ..etoro.models import Candle, Position, Rate
from ..host import Host, get_host
from ..market.calendar import session_close_datetime
from ..market.source import IexQuote, TiingoClient
from ..market.window import WindowAssemblyError, assemble_spy_signal, resolve_provisional_close
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
    return MarketInputs({s: quotes.get(s) for s in symbols}, rates, candles, provisional_closes, ids, errors)


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


def run_session(
    *, host: Host, config: Config, repo: Repo, client: EtoroClient, trading_date: date,
    tiingo: TiingoClient, wait: bool = True,
    sleep_until: Callable[[datetime], None] = _sleep_until,
) -> SessionResult:
    """Run dry-run preflight and assemble the SPY signal.

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
    reason = f"dry run: cone_5_z={sig.cone_5_z}, drawdown={sig.spy_context.drawdown}, tier={sig.spy_context.tier}; decide not built (S6c)"
    repo.finish_session(s.id, state="finished", outcome=reason)
    return SessionResult(s.id, "signal_ready", None, reason)
