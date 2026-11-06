"""The 2026-09-11 order-latency measurement harness (RUNBOOK_LIVE_WINDOW_
2026-09-11.md §8(b); DESIGN_session.md §3.2/§3.4/§4.8/§4.9; DESIGN.md §1.1's
standing highest-risk unknown: live eToro order latency has never been
measured).

Places small buy/close orders against the eToro **DEMO account only** and
times every observed status transition to terminal, for:

  1. A full OPEN (a small SCHG buy).
  2. A full CLOSE of that same position.
  3. A PARTIAL close of a second position (the design's viability now
     depends on partial closes working AND on their latency, §4.6.1/§4.8 --
     never measured before this).

...repeated several times so variance is visible, not just a single sample,
and projects the measured per-order latency against DESIGN_session.md
§4.9's order counts (~2-3 lots per sell today, ~10 as the book matures) to
answer the real question: does the two-phase session (§3.2) fit inside the
T-30 window.

THIS SCRIPT WILL NEVER BE POINTED AT THE REAL ACCOUNT. Before placing a
single order it fetches the config's environment, the derived credential
label, and a live ``/balances`` probe, and refuses -- loudly, printing every
check it performed -- unless every one of those signals POSITIVELY confirms
demo (:func:`nightwatch.probes.order_latency.evaluate_demo_confirmation`).
"Unable to confirm" is treated exactly like "confirmed real": refused. See
that function's own docstring for why this is a stricter bar than
``EtoroClient.verify_environment()`` alone, and stricter than preflight gate
7 (DESIGN §5), which remains an unimplemented stub this harness cannot rely
on.

Run ONLY on the phone (the z10), under Chaquopy, with the real app's Python
source tree on ``sys.path`` -- see ``tools/probe_live_2026-09-11.py``'s own
docstring for why (the dev PC cannot reach eToro at all, board nightwatch/handoff.md §2). Off
-device, only ``python -m py_compile`` and the desktop test suite (which
exercises the pure logic in ``nightwatch.probes.order_latency`` against a
mocked broker, never a live one) can be run.

Every print goes through ``_p``, which forces ``flush=True`` -- the same
DESIGN_market_data.md §8 gotcha ``tools/probe_live_2026-09-11.py`` was built
around. Structured (JSON) output is written to ``--output-dir``; a
human-readable summary prints at the end and is also saved to that
directory, so Friday is execution, not authoring.

SAFETY, restated from the task (see also RUNBOOK §8(b)):
  - DEMO account only, fail-closed (above).
  - Small order sizes only (``--order-amount-usd``, default $50 -- eToro's
    own live-verified SCHG minimum, DESIGN.md §1.1).
  - Exactly what this script is about to do is printed before every order.
  - Safe to abort partway: every position this script opens is tracked
    until its closing order is confirmed terminal; anything still open when
    the script ends (success, exception, or Ctrl-C) is reported loudly, in
    both the console output and a dedicated output file, never silently
    dropped.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NIGHTWATCH_SRC = _REPO_ROOT / "app" / "src" / "main" / "python"
if str(_NIGHTWATCH_SRC) not in sys.path:
    sys.path.insert(0, str(_NIGHTWATCH_SRC))

DEFAULT_OUTPUT_DIR = _REPO_ROOT / "data" / "probe_order_latency_2026-09-11"

#: eToro's own live-verified minimum for SCHG (DESIGN.md §1.1;
#: config.py's min_order_size docstring). Small and deliberate -- this is a
#: latency probe, not a position (RUNBOOK §8(b) step 1).
DEFAULT_ORDER_AMOUNT_USD = 50.0

#: RUNBOOK §8(b) step 4b: partial close roughly half the position's value,
#: leaving a remainder well above the $10 minPositionExposure/
#: minPositionAmount floor -- NOT a boundary test of that floor (deliberately
#: kept a separate concern, per the runbook's own note).
DEFAULT_PARTIAL_FRACTION = 0.5

#: RUNBOOK §8(b) step 5: "a few repeats, not one."
DEFAULT_REPEATS = 4


def _p(msg: str) -> None:
    """Print one line, always flushed (DESIGN_market_data.md §8 gotcha)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    print(f"{ts} LATENCY_PROBE: {msg}", flush=True)


def _wall_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fmt_exc(e: BaseException) -> str:
    status = getattr(e, "status_code", None)
    return f"{type(e).__name__} status={status if type(status) is int else 'unknown'}"


def _save_json(output_dir: Path, name: str, obj: Any) -> Path:
    """Save ``obj`` as pretty JSON under ``output_dir``, timestamped filename
    (mirrors ``tools/probe_live_2026-09-11.py``'s ``_save_json`` exactly --
    same convention, same reason: a raw payload that only ever gets printed
    is a payload that can scroll off and be lost)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"{ts}_{name}.json"
    try:
        path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
        _p(f"saved: {path}")
    except Exception as e:  # pragma: no cover - defensive, evidence-saving must not itself crash the run
        _p(f"FAILED to save {name}: {_fmt_exc(e)}")
    return path


def _trace_to_dict(trace: Any) -> dict[str, Any]:
    d = dataclasses.asdict(trace)
    return d


def _confirm_demo_or_die(client: Any, config: Any) -> Any:
    """Run every demo-confirmation check and return the resulting
    ``DemoConfirmation``. Does NOT exit the process itself -- ``main()``
    checks ``.confirmed`` and refuses to proceed, so this stays a pure
    orchestration step the caller controls, consistent with this project's
    "never bury a safety decision inside a function that also has side
    effects on the happy path" preference.
    """
    from nightwatch.etoro.client import EtoroClient
    from nightwatch.probes.order_latency import evaluate_demo_confirmation

    config_environment = config.etoro.environment.value
    credential_label = client._credentials.label  # noqa: SLF001 -- see module docstring

    verify_environment_ok = True
    verify_environment_error: str | None = None
    detected_signal: str | None = None
    try:
        # Signal 4 (2026-09-11, corrected): the live /balances body is
        # structurally identical between demo and real -- same gcid, same
        # accountId, same accountType ("Trading"), same subType (null) -- so
        # scanning it can NEVER positively confirm demo, and this gate was
        # permanently inconclusive as a result (exit 3, correctly failing
        # closed). See EtoroClient.verify_environment's docstring and the raw
        # captures under data/probe_2026-09-11/env_probe_{demo,real}/.
        #
        # The body scan is still run first (free, and self-healing if eToro
        # ever adds a marker), then falls through to the positive
        # path-segment probe: eToro authorises a credential for the demo
        # segment or the real one, never both, so the 200/403 pattern names
        # the binding of the CREDENTIAL rather than restating the config.
        #
        # Fail-closed is unchanged: probe_environment() returns None for any
        # ambiguous or failed probe, and evaluate_demo_confirmation treats a
        # None detected_signal as a refusal exactly as before.
        response = client._request("GET", "/api/v1/balances", request_class="read")  # noqa: SLF001
        raw = response.json()
        detected_signal = EtoroClient._detect_environment(raw)  # noqa: SLF001
        if detected_signal is None:
            detected_signal = client.probe_environment()
        if detected_signal is not None and detected_signal != config_environment:
            verify_environment_ok = False
            verify_environment_error = (
                f"live environment probe signal={detected_signal!r} disagrees with "
                f"configured environment={config_environment!r}"
            )
    except Exception as e:
        verify_environment_ok = False
        verify_environment_error = f"probe itself failed: {_fmt_exc(e)}"

    return evaluate_demo_confirmation(
        config_environment=config_environment,
        credential_label=credential_label,
        verify_environment_ok=verify_environment_ok,
        verify_environment_error=verify_environment_error,
        detected_signal=detected_signal,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS,
                         help="how many times to repeat the whole buy-then-close cycle (RUNBOOK §8(b) step 5: 3-5)")
    parser.add_argument("--order-amount-usd", type=float, default=DEFAULT_ORDER_AMOUNT_USD,
                         help="USD size of every buy this script places (small, deliberate)")
    parser.add_argument("--partial-fraction", type=float, default=DEFAULT_PARTIAL_FRACTION,
                         help="fraction of the second position's units to deduct on the partial-close leg")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="seconds between lookup_order polls")
    parser.add_argument("--poll-timeout", type=float, default=90.0, help="seconds before a poll gives up on one leg")
    parser.add_argument("--orders-low", type=int, default=2, help="DESIGN_session.md §4.9: lots touched per sell today")
    parser.add_argument("--orders-high", type=int, default=3, help="§4.9: lots touched per sell today, upper bound")
    parser.add_argument("--orders-mature", type=int, default=10, help="§4.9: lots touched per sell once the book matures")
    args = parser.parse_args(argv)

    if not (3 <= args.repeats <= 5):
        _p(f"WARNING: --repeats={args.repeats} is outside RUNBOOK §8(b) step 5's '3-5' guidance -- proceeding anyway, "
           "since the operator may have a good reason (a shorter market window), but this is noted for the record.")

    output_dir: Path = args.output_dir
    _p(f"probe_order_latency_2026-09-11 starting. output_dir={output_dir}")

    try:
        from nightwatch.config import load_config
        from nightwatch.etoro.client import EtoroClient
        from nightwatch.etoro.models import partial_close_leaves_legal_remainder
        from nightwatch.host import get_host
        from nightwatch.probes.order_latency import (
            OrderLegTrace,
            ProjectionResult,
            project_session_budget,
            render_human_summary,
            run_close_leg,
            run_open_leg,
            summarize_leg,
        )
    except Exception as e:
        _p(f"FATAL: cannot import nightwatch package: {_fmt_exc(e)}")
        _p("Make sure this is run where app/src/main/python/nightwatch is importable (see this file's docstring).")
        return 2

    try:
        config = load_config(host=get_host())
    except Exception as e:
        _p(f"FATAL: load_config() failed: {_fmt_exc(e)}")
        return 2

    _p(f"config: {config.describe()}")

    try:
        client = EtoroClient(config.etoro, role="write")
    except Exception as e:
        _p(f"FATAL: could not build the eToro client: {_fmt_exc(e)}")
        _p("Most likely config/.secrets.env is missing or does not carry the demo credential pair "
           "(ETORO_DEMO_PRIVATE_KEY / ETORO_PUBLIC_KEY). No order was placed.")
        return 2

    demo = _confirm_demo_or_die(client, config)
    _save_json(output_dir, "demo_confirmation", dataclasses.asdict(demo))
    _p(f"demo confirmation: confirmed={demo.confirmed}")
    for reason in demo.reasons:
        _p(f"  - {reason}")
    if not demo.confirmed:
        _p("REFUSING TO PLACE ANY ORDER. See the reasons above. Fix config/config.toml (and/or the "
           "credentials this harness resolved) so etoro.environment is genuinely, verifiably 'demo', then re-run.")
        client.close()
        return 3

    try:
        schg_id = client.resolve_instrument_id("SCHG")
        _p(f"SCHG resolved to instrument_id={schg_id}")
    except Exception as e:
        _p(f"FATAL: could not resolve SCHG instrument id: {_fmt_exc(e)} -- refusing to guess, aborting")
        client.close()
        return 4

    open_traces: list[OrderLegTrace] = []
    close_full_traces: list[OrderLegTrace] = []
    close_partial_traces: list[OrderLegTrace] = []
    close_cleanup_traces: list[OrderLegTrace] = []
    all_traces: list[OrderLegTrace] = []
    left_open: dict[str, str] = {}  # position_id -> human-readable note

    def _record_open(position_id: str | None, note: str) -> None:
        if position_id is not None:
            left_open[position_id] = note

    def _clear_open(position_id: str | None) -> None:
        if position_id is not None:
            left_open.pop(position_id, None)

    try:
        for repeat in range(args.repeats):
            _p(f"=== REPEAT {repeat + 1}/{args.repeats} ===")

            # --- Leg 1: full open, position A -------------------------------
            request_id_a = str(uuid.uuid4())
            _p(
                f"ABOUT TO: place a BUY order for SCHG (instrument_id={schg_id}), "
                f"amount=${args.order_amount_usd:.2f} USD, request_id={request_id_a} (DEMO account)"
            )
            trace_a = run_open_leg(
                client, request_id=request_id_a, instrument_id=schg_id,
                amount_usd=args.order_amount_usd, repeat_index=repeat,
                clock=time.monotonic, wall_clock=_wall_clock, sleep=time.sleep,
                poll_interval_s=args.poll_interval, poll_timeout_s=args.poll_timeout,
            )
            open_traces.append(trace_a)
            all_traces.append(trace_a)
            _p(
                f"open (position A) -- terminal_status={trace_a.terminal_status} "
                f"timed_out={trace_a.timed_out} time_to_terminal_s={trace_a.time_to_terminal_s} "
                f"position_id={trace_a.position_id} transitions={[o.status for o in trace_a.observations]}"
            )
            if trace_a.position_id is not None:
                _record_open(trace_a.position_id, f"repeat {repeat + 1}: opened by request_id={request_id_a}, never confirmed closed")
            elif trace_a.timed_out:
                # A timed-out open is NOT proof that nothing opened: the order
                # may still fill broker-side after this script gives up, and no
                # position_id was ever observed to close it by. Never let the
                # summary claim "no position left open" in that case.
                left_open[f"order:{trace_a.order_id}"] = (
                    f"repeat {repeat + 1}: open request_id={request_id_a} TIMED OUT before any "
                    "position_id was observed -- it may still have filled; CHECK THE DEMO ACCOUNT BY HAND"
                )

            # --- Leg 2: full close of position A ----------------------------
            if trace_a.position_id is not None and not trace_a.timed_out:
                request_id_close_a = str(uuid.uuid4())
                _p(
                    f"ABOUT TO: FULLY close position {trace_a.position_id} "
                    f"(instrument_id={schg_id}), request_id={request_id_close_a} (DEMO account)"
                )
                trace_close_a = run_close_leg(
                    client, leg="close_full", request_id=request_id_close_a,
                    position_id=trace_a.position_id, instrument_id=schg_id,
                    units_to_deduct=None, repeat_index=repeat,
                    clock=time.monotonic, wall_clock=_wall_clock, sleep=time.sleep,
                    poll_interval_s=args.poll_interval, poll_timeout_s=args.poll_timeout,
                )
                close_full_traces.append(trace_close_a)
                all_traces.append(trace_close_a)
                _p(
                    f"close_full (position A) -- terminal_status={trace_close_a.terminal_status} "
                    f"timed_out={trace_close_a.timed_out} time_to_terminal_s={trace_close_a.time_to_terminal_s} "
                    f"transitions={[o.status for o in trace_close_a.observations]}"
                )
                if not trace_close_a.timed_out and trace_close_a.terminal_status == "FILLED":
                    _clear_open(trace_a.position_id)
            else:
                _p(
                    f"SKIPPING close of position A -- open leg either timed out or produced no "
                    f"position_id. If a position_id exists, it remains tracked as left-open."
                )

            # --- Leg 3: full open, position B (precursor to partial close) --
            request_id_b = str(uuid.uuid4())
            _p(
                f"ABOUT TO: place a second BUY order for SCHG (instrument_id={schg_id}), "
                f"amount=${args.order_amount_usd:.2f} USD, request_id={request_id_b} (DEMO account) "
                "-- this position will be PARTIALLY closed next (RUNBOOK §8(b) step 4b)"
            )
            trace_b = run_open_leg(
                client, request_id=request_id_b, instrument_id=schg_id,
                amount_usd=args.order_amount_usd, repeat_index=repeat,
                clock=time.monotonic, wall_clock=_wall_clock, sleep=time.sleep,
                poll_interval_s=args.poll_interval, poll_timeout_s=args.poll_timeout,
            )
            open_traces.append(trace_b)
            all_traces.append(trace_b)
            _p(
                f"open (position B) -- terminal_status={trace_b.terminal_status} "
                f"timed_out={trace_b.timed_out} time_to_terminal_s={trace_b.time_to_terminal_s} "
                f"position_id={trace_b.position_id} transitions={[o.status for o in trace_b.observations]}"
            )
            if trace_b.position_id is not None:
                _record_open(trace_b.position_id, f"repeat {repeat + 1}: opened by request_id={request_id_b}, never confirmed closed")
            elif trace_b.timed_out:
                left_open[f"order:{trace_b.order_id}"] = (
                    f"repeat {repeat + 1}: open request_id={request_id_b} TIMED OUT before any "
                    "position_id was observed -- it may still have filled; CHECK THE DEMO ACCOUNT BY HAND"
                )

            if trace_b.position_id is None or trace_b.timed_out:
                _p("SKIPPING partial-close leg -- position B's open did not produce a usable, terminal position.")
                continue

            fill_units = trace_b.raw_final.get("units")
            fill_price = None
            if trace_b.raw_final.get("executionPrice") is not None:
                fill_price = float(trace_b.raw_final["executionPrice"])
            if fill_units is None:
                _p(
                    "SKIPPING partial-close leg -- the open order's raw response carries no 'units' "
                    "field, so this script cannot compute a safe UnitsToDeduct without guessing. "
                    f"Flattening position {trace_b.position_id} with a full close instead, so this "
                    "repeat leaves nothing dangling (the partial-close latency simply goes unmeasured)."
                )
                request_id_nounits = str(uuid.uuid4())
                trace_nounits = run_close_leg(
                    client, leg="close_cleanup", request_id=request_id_nounits,
                    position_id=trace_b.position_id, instrument_id=schg_id,
                    units_to_deduct=None, repeat_index=repeat,
                    clock=time.monotonic, wall_clock=_wall_clock, sleep=time.sleep,
                    poll_interval_s=args.poll_interval, poll_timeout_s=args.poll_timeout,
                )
                close_cleanup_traces.append(trace_nounits)
                all_traces.append(trace_nounits)
                if not trace_nounits.timed_out and trace_nounits.terminal_status == "FILLED":
                    _clear_open(trace_b.position_id)
                continue
            fill_units = float(fill_units)
            units_to_deduct = fill_units * args.partial_fraction
            market_value = fill_units * fill_price if fill_price is not None else args.order_amount_usd
            close_market_value = units_to_deduct * fill_price if fill_price is not None else market_value * args.partial_fraction
            if not partial_close_leaves_legal_remainder(market_value, close_market_value):
                _p(
                    f"SKIPPING partial-close leg -- a {args.partial_fraction:.0%} close of position "
                    f"{trace_b.position_id} (market_value~=${market_value:.2f}) would leave an illegal "
                    "sub-$10 remainder. Adjust --partial-fraction or --order-amount-usd. Falling back "
                    "to a full close so nothing is left dangling."
                )
                request_id_fallback = str(uuid.uuid4())
                trace_fallback = run_close_leg(
                    client, leg="close_cleanup", request_id=request_id_fallback,
                    position_id=trace_b.position_id, instrument_id=schg_id,
                    units_to_deduct=None, repeat_index=repeat,
                    clock=time.monotonic, wall_clock=_wall_clock, sleep=time.sleep,
                    poll_interval_s=args.poll_interval, poll_timeout_s=args.poll_timeout,
                )
                close_cleanup_traces.append(trace_fallback)
                all_traces.append(trace_fallback)
                if not trace_fallback.timed_out and trace_fallback.terminal_status == "FILLED":
                    _clear_open(trace_b.position_id)
                continue

            # --- Leg 4 (4b): partial close of position B --------------------
            request_id_partial = str(uuid.uuid4())
            _p(
                f"ABOUT TO: PARTIALLY close position {trace_b.position_id} "
                f"(instrument_id={schg_id}), units_to_deduct={units_to_deduct:.6f} "
                f"(~{args.partial_fraction:.0%} of {fill_units:.6f} units, market_value~=${market_value:.2f}), "
                f"request_id={request_id_partial} (DEMO account) -- HAND-BUILT via close_position's "
                "units_to_deduct parameter; see this leg's own note in the summary output."
            )
            trace_partial = run_close_leg(
                client, leg="close_partial", request_id=request_id_partial,
                position_id=trace_b.position_id, instrument_id=schg_id,
                units_to_deduct=units_to_deduct, repeat_index=repeat,
                clock=time.monotonic, wall_clock=_wall_clock, sleep=time.sleep,
                poll_interval_s=args.poll_interval, poll_timeout_s=args.poll_timeout,
            )
            close_partial_traces.append(trace_partial)
            all_traces.append(trace_partial)
            _p(
                f"close_partial (position B) -- terminal_status={trace_partial.terminal_status} "
                f"timed_out={trace_partial.timed_out} time_to_terminal_s={trace_partial.time_to_terminal_s} "
                f"transitions={[o.status for o in trace_partial.observations]}"
            )

            # --- Step 7: flatten the remainder (cleanup close, untimed leg) -
            if not trace_partial.timed_out and trace_partial.position_id is not None:
                request_id_cleanup = str(uuid.uuid4())
                _p(
                    f"ABOUT TO: flatten the remainder of position {trace_partial.position_id} "
                    f"with a full close (cleanup, not one of the three measured legs), "
                    f"request_id={request_id_cleanup} (DEMO account)"
                )
                trace_cleanup = run_close_leg(
                    client, leg="close_cleanup", request_id=request_id_cleanup,
                    position_id=trace_partial.position_id, instrument_id=schg_id,
                    units_to_deduct=None, repeat_index=repeat,
                    clock=time.monotonic, wall_clock=_wall_clock, sleep=time.sleep,
                    poll_interval_s=args.poll_interval, poll_timeout_s=args.poll_timeout,
                )
                close_cleanup_traces.append(trace_cleanup)
                all_traces.append(trace_cleanup)
                _p(
                    f"close_cleanup (position B remainder) -- terminal_status={trace_cleanup.terminal_status} "
                    f"timed_out={trace_cleanup.timed_out} time_to_terminal_s={trace_cleanup.time_to_terminal_s}"
                )
                if not trace_cleanup.timed_out and trace_cleanup.terminal_status == "FILLED":
                    _clear_open(trace_partial.position_id)
            else:
                _p(
                    f"CANNOT flatten position {trace_partial.position_id} automatically -- the partial "
                    "close leg itself timed out. It remains tracked as left-open."
                )
    except BaseException as e:
        open_trace = getattr(e, "open_leg_trace", None)
        if open_trace is not None:
            _save_json(output_dir, "failed_open_trace", open_trace)
            open_key = f"order:{open_trace['order_id'] or open_trace['request_id']}"
            left_open[open_key] = (
                f"repeat {open_trace['repeat_index'] + 1}: open attempt is unresolved; "
                f"reconcile by order_id={open_trace['order_id']} and request_id={open_trace['request_id']} "
                "before declaring the demo book flat"
            )
        close_trace = getattr(e, "close_leg_trace", None)
        if close_trace is not None:
            _save_json(output_dir, "failed_close_trace", close_trace)
        _p(f"EXCEPTION during measurement loop: {_fmt_exc(e)} -- stopping here, reporting whatever was collected.")
    finally:
        _save_json(output_dir, "all_traces", [_trace_to_dict(t) for t in all_traces])

        partial_fill_events = [
            f"{t.leg} repeat={t.repeat_index + 1} request_id={t.request_id} position_id={t.position_id}"
            for t in all_traces
            if t.is_partial_fill and t.leg != "close_partial"
        ]
        timed_out_events = [
            f"{t.leg} repeat={t.repeat_index + 1} request_id={t.request_id} order_id={t.order_id}"
            for t in all_traces
            if t.timed_out
        ]
        left_open_report = [f"{pos_id}: {note}" for pos_id, note in left_open.items()]
        if left_open_report:
            _save_json(output_dir, "left_open_positions", left_open_report)

        leg_stats: dict[str, Any] = {}
        for leg_name, traces in (
            ("open", open_traces),
            ("close_full", close_full_traces),
            ("close_partial", close_partial_traces),
            ("close_cleanup", close_cleanup_traces),
        ):
            terminal_times = [t.time_to_terminal_s for t in traces if not t.timed_out and t.time_to_terminal_s is not None]
            if terminal_times:
                leg_stats[leg_name] = summarize_leg(leg_name, terminal_times)

        projections: list[Any] = []
        if "close_full" in leg_stats or "close_partial" in leg_stats:
            close_times = [
                v
                for t in (close_full_traces + close_partial_traces)
                if not t.timed_out and (v := t.time_to_terminal_s) is not None
            ]
            if close_times:
                combined_close_stats = summarize_leg("close_full+close_partial (combined)", close_times)
                leg_stats["close_combined"] = combined_close_stats
                projections.append(
                    project_session_budget(
                        combined_close_stats.mean_s, orders_low=args.orders_low, orders_high=args.orders_high,
                        label=f"today (~{args.orders_low}-{args.orders_high} lots/sell, §4.9)",
                    )
                )
                projections.append(
                    project_session_budget(
                        combined_close_stats.mean_s, orders_low=args.orders_mature, orders_high=args.orders_mature,
                        label=f"mature book (~{args.orders_mature} lots/sell, §4.9)",
                    )
                )

        if leg_stats:
            _save_json(
                output_dir, "leg_statistics",
                {name: dataclasses.asdict(stats) for name, stats in leg_stats.items()},
            )
        if projections:
            _save_json(output_dir, "t30_projections", [dataclasses.asdict(p) for p in projections])

        summary = render_human_summary(
            demo=demo, leg_stats=leg_stats, projections=projections,
            partial_fill_events=partial_fill_events, timed_out_events=timed_out_events,
            left_open_positions=left_open_report,
        )
        print(summary, flush=True)
        summary_path = output_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_summary.txt"
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(summary, encoding="utf-8")
            _p(f"summary saved: {summary_path}")
        except Exception as e:  # pragma: no cover - defensive
            _p(f"FAILED to save summary: {_fmt_exc(e)}")

        client.close()

    if left_open:
        _p(f"FINISHED WITH {len(left_open)} POSITION(S) LEFT OPEN -- see left_open_positions output file.")
        return 1
    _p("probe_order_latency_2026-09-11 finished. Book left flat.")
    return 0


def run_all(output_dir: str) -> int:
    """Chaquopy/Kotlin entry point, mirroring ``tools/probe_live_2026-09-11.py``'s
    own ``run_all()`` convention -- callable via
    ``Python.getInstance().getModule("probe_order_latency_2026_09_11").callAttr("run_all", dir)``
    once this file is copied into the app's Chaquopy Python source set under
    that module name (note the UNDERSCORES: a Python module name cannot
    contain hyphens, so the on-device copy must be renamed, exactly as
    ``probe_live_2026_09_11.py`` already is).

    ``output_dir`` is required rather than defaulted, because this file's own
    :data:`DEFAULT_OUTPUT_DIR` is computed from ``__file__``'s repo layout and
    resolves to a non-existent (and unwritable) path once the file lives in an
    APK's source set. Every other setting keeps its documented default
    (RUNBOOK §8(b)). Returns ``main()``'s exit code instead of calling
    ``sys.exit``, so a Kotlin caller gets the value rather than a SystemExit.
    """
    return main(["--output-dir", str(output_dir)])


if __name__ == "__main__":
    sys.exit(main())
