"""Daily session orchestration (doc/DESIGN_session.md §5).

S6a: steps [1] only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

import httpx

from ..config import Config
from ..db.repo import Repo, utcnow
from ..etoro.client import EtoroClient
from ..etoro.models import Position
from ..host import Host
from .preflight import (
    GateResult, GateSpec, etoro_reachability_probe, gate_1_halt,
    gate_2_power, gate_3_network, gate_4_clock, gate_5_database,
    gate_6_auth, gate_7_environment, gate_8_instruments, gate_9_calendar,
    gate_11_reconciliation, gate_12_risk_caps, run_preflight, unimplemented_gate,
)


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
    *, host: Host, config: Config, repo: Repo, client: EtoroClient,
    session_id: int, trading_date: date, now: Callable[[], datetime] | None = None,
) -> list[GateSpec]:
    now = now or host.now
    positions: list[list[Position] | None] = [None]

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
        unimplemented_gate(10, "price_data"),  # wired in S6b (trunk/T015)
        GateSpec(11, "Reconciliation", reconciliation),
        GateSpec(12, "risk_caps", lambda: gate_12_risk_caps(
            risk=config.risk, repo=repo,
            positions=positions[0] if positions[0] is not None else client.get_portfolio(),
        )),
    ]


def run_session(
    *, host: Host, config: Config, repo: Repo, client: EtoroClient, trading_date: date,
) -> SessionResult:
    """Run dry-run preflight with constructed dependencies.

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
        host=host, config=config, repo=repo, client=client,
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
    repo.update_session(s.id, state="settled")
    return SessionResult(s.id, "preflight_passed", None, "preflight passed")
