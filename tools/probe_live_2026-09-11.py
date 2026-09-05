"""The 2026-09-11 live-window probe (HANDOFF_2026-09-05.md, DESIGN_market_data.md
§10.5/§10.6/§10.9-§10.13). ONE self-contained on-device script that batches every
open question this project has that needs a live weekday market and/or
on-device Chaquopy execution, because Friday 2026-09-11 is the operator's only
window with the phone plugged in during live US market hours before this all
slips another week.

Run ONLY on the phone (the z10), under Chaquopy, with the real app's Python
source tree on ``sys.path`` (this file adds ``app/src/main/python`` itself,
see ``_ensure_nightwatch_importable`` below, so it works whether invoked from
a plain interpreter that happens to share this repo's layout, or from inside
the installed app's own process). The dev PC cannot reach eToro at all -- see
HANDOFF.md §2 / HANDOFF_2026-09-04_market_data.md §2.8, the Kominfo DNS
sinkhole -- so nothing in sections B/C/D/E1/E2 below can be exercised from a
desktop machine; only section A's pure-import checks and the whole file's own
``python -m py_compile`` syntax check can run off-device.

Read ``doc/RUNBOOK_LIVE_WINDOW_2026-09-11.md`` before running this on the
phone. It explains invocation, what each section proves, what to send back,
and states plainly that section E2 spends real money and is off by default.

SECTIONS (each independently labelled, each independently failing -- a hang
or an exception in one must never stop the others from running and
reporting; see ``run_section``'s thread-based timeout):

  A  -- Chaquopy runtime imports (read-only, no eToro call at all).
  B  -- UPRO rates-freeze re-poll across a live-market interval (READ-ONLY).
  C  -- Live-payload audit of the six unverified parsers (READ-ONLY).
  D  -- Sleeve arithmetic re-measurement (READ-ONLY).
  E1 -- The fee question, read-only half (READ-ONLY).
  E2 -- The fee question, real-money half. OFF BY DEFAULT. Places one real
        $50 SCHG buy order. Requires --e2, the NIGHTWATCH_PROBE_E2_ARM
        environment variable set to the exact phrase checked in
        ``section_e2_real_money_order``, AND a typed interactive
        confirmation. Never runs as part of ``run_all``'s default section
        list.

Every network call below carries an explicit timeout (DESIGN_market_data.md
§8's gotcha: the last Chaquopy probe hung on exactly this). Every print goes
through ``_p``, which forces ``flush=True`` (the same gotcha's other half:
unflushed stdout hid whatever had already run before the hang).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import json
import os
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Make the real nightwatch package importable. This file lives at
# <repo_root>/tools/probe_live_2026-09-11.py; the package root is
# <repo_root>/app/src/main/python, exactly the "pythonpath" pytest.ini already
# uses for the desktop test suite. On-device under Chaquopy the app's own
# source set already puts this on sys.path, so this is a no-op there; it only
# matters for the (very limited) off-device checks this file supports, and
# keeps this script runnable the same way regardless of cwd.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_NIGHTWATCH_SRC = _REPO_ROOT / "app" / "src" / "main" / "python"
if str(_NIGHTWATCH_SRC) not in sys.path:
    sys.path.insert(0, str(_NIGHTWATCH_SRC))


def _p(msg: str) -> None:
    """Print one line, always flushed (DESIGN_market_data.md §8 gotcha)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    print(f"{ts} PROBE: {msg}", flush=True)


def _fmt_exc(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


#: Known live instrument ids (DESIGN_market_data.md §2.9, resolved
#: 2026-09-04): SPY=3000, UPRO=3244, SCHG=1084. Used only as a fallback if a
#: live search fails; the primary path always re-resolves via the API so a
#: stale hardcoded id is never silently trusted.
KNOWN_INSTRUMENT_IDS: dict[str, int] = {"SPY": 3000, "UPRO": 3244, "SCHG": 1084}

#: DESIGN_market_data.md §10.11's recorded figures, re-stated here so
#: section D can diff against them without editing the design doc's prose.
DOCUMENTED_SLEEVE_FIGURES: dict[str, float] = {
    "schg_value_usd": 44867.0,
    "upro_value_usd": 4047.0,
    "sleeve_equity_usd": 50179.0,
    "schg_position_count": 202.0,
    "upro_position_count": 7.0,
    "schg_amount_mean": 202.31,
    "schg_amount_median": 100.0,
}

DEFAULT_OUTPUT_DIR = _REPO_ROOT / "data" / "probe_2026-09-11"


def _save_json(output_dir: Path, name: str, obj: Any) -> Path:
    """Save ``obj`` as pretty JSON under ``output_dir``, timestamped filename.

    DESIGN_market_data.md §9's own retrospective: "the saved probe payloads
    are now the antidote to assumed field shapes." Every raw payload this
    script touches must survive the run in a re-readable file, not just a
    printed line that scrolls off. ``default=str`` so an unexpected non-JSON
    type (e.g. a dataclass slipped in by mistake) still gets written instead
    of raising and losing the evidence.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"{ts}_{name}.json"
    try:
        path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
        _p(f"saved payload: {path}")
    except Exception as e:  # pragma: no cover - defensive, evidence-saving must not itself crash the run
        _p(f"FAILED to save payload {name}: {_fmt_exc(e)}")
    return path


def run_section(
    name: str,
    fn: Callable[..., list[str]],
    *args: Any,
    timeout_s: float = 300.0,
    **kwargs: Any,
) -> bool:
    """Run one section in its own thread with a hard timeout.

    HANDOFF_2026-09-05.md's brief: "a hang or exception in one [section] must
    never let other sections fail to report." A per-call ``httpx`` timeout
    handles the specific cause of the LAST hang (DESIGN_market_data.md §8),
    but this is a second, independent layer against anything else that could
    block (a retry loop, a slow parse, an unexpected stdin read) -- the
    thread is abandoned (Python cannot forcibly kill a thread) but the main
    script moves on and reports the timeout rather than waiting forever.
    Returns True if the section completed without raising or timing out.
    """
    _p(f"=== SECTION {name} -- BEGIN ===")
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn, *args, **kwargs)
    try:
        future.result(timeout=timeout_s)
        _p(f"=== SECTION {name} -- END (ok) ===")
        return True
    except concurrent.futures.TimeoutError:
        _p(
            f"=== SECTION {name} -- END (TIMEOUT after {timeout_s}s -- "
            "abandoning this section's thread, continuing with the rest) ==="
        )
        return False
    except Exception as e:
        _p(f"=== SECTION {name} -- END (EXCEPTION {_fmt_exc(e)}) ===")
        import traceback
        _p(traceback.format_exc())
        return False
    finally:
        # Do not wait for an abandoned/hung thread; shutdown(wait=False) lets
        # the process move on. The thread itself may leak until it eventually
        # finishes or the process exits -- acceptable for a one-shot probe.
        executor.shutdown(wait=False, cancel_futures=False)


# ---------------------------------------------------------------------------
# Section A -- Chaquopy runtime imports
# ---------------------------------------------------------------------------


def section_a_chaquopy_imports(output_dir: Path) -> None:
    """HANDOFF_2026-09-05.md §7 item 4 / DESIGN_market_data.md §10.3.

    READ-ONLY: no eToro call anywhere in this function. Verifies numpy,
    pandas, exchange_calendars, and httpx actually import and do real work
    on-device -- the thing the prior probe never confirmed because it hung.
    The one network touch here (a reachability GET, not an eToro call) is
    unauthenticated and carries an explicit 10s timeout; if it fails or times
    out this section still reports everything measured before it.
    """
    results: dict[str, Any] = {}

    try:
        import httpx
        results["httpx_version"] = httpx.__version__
        _p(f"A: httpx OK version={httpx.__version__}")
    except Exception as e:
        _p(f"A: httpx FAIL {_fmt_exc(e)}")

    try:
        import numpy as np
        arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        mean, std = float(arr.mean()), float(arr.std())
        results["numpy_version"] = np.__version__
        results["numpy_mean"] = mean
        results["numpy_std"] = std
        _p(f"A: numpy OK version={np.__version__} mean={mean} std={std}")
    except Exception as e:
        _p(f"A: numpy FAIL {_fmt_exc(e)}")

    try:
        import numpy as np
        import pandas as pd
        rng = np.random.default_rng(seed=42)
        series = pd.Series(rng.normal(loc=0.0004, scale=0.01, size=300))
        roll = series.rolling(window=260).std(ddof=0)
        last = float(roll.iloc[-1])
        n_valid = int(roll.notna().sum())
        results["pandas_version"] = pd.__version__
        results["pandas_260_roll_std_last"] = last
        results["pandas_n_valid_windows"] = n_valid
        _p(
            f"A: pandas OK version={pd.__version__} 260-window rolling.std() "
            f"last={last:.8f} n_valid_windows={n_valid}"
        )
    except Exception as e:
        _p(f"A: pandas FAIL {_fmt_exc(e)}")

    try:
        import exchange_calendars as xcals
        ver = getattr(xcals, "__version__", "unknown")
        cal = xcals.get_calendar("XNYS")
        n_sessions = len(cal.sessions)
        results["exchange_calendars_version"] = ver
        results["exchange_calendars_xnys_session_count"] = n_sessions
        _p(f"A: exchange_calendars OK version={ver} XNYS sessions={n_sessions}")
    except Exception as e:
        _p(f"A: exchange_calendars FAIL {_fmt_exc(e)}")

    # Unauthenticated reachability check -- explicit timeout, no eToro call
    # with credentials (that is sections B-E's job). A 3xx/4xx is still a
    # SUCCESS here; this only proves DNS + TLS work from this process.
    try:
        import httpx
        with httpx.Client(timeout=10.0, follow_redirects=False) as c:
            resp = c.get("https://api.tiingo.com/")
        results["tiingo_reachability_status"] = resp.status_code
        _p(f"A: tiingo.com reachability -> HTTP {resp.status_code}")
    except Exception as e:
        _p(f"A: tiingo.com reachability FAIL {_fmt_exc(e)}")

    _save_json(output_dir, "section_a_chaquopy_imports", results)


# ---------------------------------------------------------------------------
# Section B -- UPRO rates-freeze re-poll
# ---------------------------------------------------------------------------


def _resolve_instrument_ids(client: Any, symbols: Sequence[str]) -> dict[str, int]:
    """Resolve symbols to instrument ids, falling back to
    ``KNOWN_INSTRUMENT_IDS`` (with a loud note) if a live search fails.
    """
    out: dict[str, int] = {}
    for symbol in symbols:
        try:
            iid = client.resolve_instrument_id(symbol)
            out[symbol] = iid
            _p(f"resolved {symbol} -> instrument_id={iid} (live search)")
        except Exception as e:
            fallback = KNOWN_INSTRUMENT_IDS.get(symbol)
            _p(
                f"WARNING: live resolve for {symbol} failed ({_fmt_exc(e)}); "
                f"falling back to known id {fallback} from "
                "DESIGN_market_data.md §2.9 -- treat any result using this id "
                "as slightly less certain than a freshly-resolved one"
            )
            if fallback is not None:
                out[symbol] = fallback
    return out


def section_b_upro_rates_freeze(
    client: Any, output_dir: Path, *, poll_count: int, poll_interval_s: float
) -> None:
    """DESIGN_market_data.md §10.6(a) / HANDOFF §7 item 7.

    READ-ONLY: only ``get_rates`` calls, never an order. Polls UPRO's quote
    ``poll_count`` times, ``poll_interval_s`` apart, alongside SCHG and SPY as
    controls -- the point is to prove the *market* was moving during the
    window (via the controls) while checking whether UPRO's own quote moved
    with it or stayed frozen, exactly the distinction a single sample cannot
    make (§10.6(a): "a single day's evidence cannot distinguish 'permanent'
    from 'transient'").
    """
    ids = _resolve_instrument_ids(client, ["SPY", "SCHG", "UPRO"])
    if "UPRO" not in ids:
        _p("B: cannot resolve UPRO's instrument id by any path -- aborting section B")
        return
    instrument_ids = list(ids.values())
    samples: list[dict[str, Any]] = []
    for i in range(poll_count):
        poll_ts = datetime.now(timezone.utc).isoformat()
        try:
            rates = client.get_rates(instrument_ids)
            row = {"poll_index": i, "polled_at": poll_ts, "rates": {}}
            for symbol, iid in ids.items():
                rate = rates.get(iid)
                if rate is None:
                    row["rates"][symbol] = None
                    continue
                row["rates"][symbol] = {
                    "instrument_id": iid,
                    "bid": rate.bid,
                    "ask": rate.ask,
                    "last_execution": rate.last_execution,
                    "timestamp": rate.timestamp,
                }
            samples.append(row)
            summary = ", ".join(
                f"{sym}={row['rates'][sym]['last_execution'] if row['rates'].get(sym) else None}"
                f"@{row['rates'][sym]['timestamp'] if row['rates'].get(sym) else None}"
                for sym in ids
            )
            _p(f"B: poll {i + 1}/{poll_count} at {poll_ts} -- {summary}")
        except Exception as e:
            _p(f"B: poll {i + 1}/{poll_count} FAILED: {_fmt_exc(e)}")
            samples.append({"poll_index": i, "polled_at": poll_ts, "error": _fmt_exc(e)})
        if i < poll_count - 1:
            time.sleep(poll_interval_s)

    _save_json(output_dir, "section_b_upro_rates_freeze", samples)

    # Verdict: did each symbol's (last_execution, timestamp) pair change
    # across samples?
    def _distinct_values(symbol: str) -> set[Any]:
        vals = set()
        for s in samples:
            r = s.get("rates", {}).get(symbol) if isinstance(s.get("rates"), dict) else None
            if r:
                vals.add((r["last_execution"], r["timestamp"]))
        return vals

    for symbol in ("SPY", "SCHG", "UPRO"):
        if symbol not in ids:
            continue
        distinct = _distinct_values(symbol)
        moved = len(distinct) > 1
        _p(
            f"B VERDICT {symbol}: {'MOVED' if moved else 'FROZEN/UNCHANGED'} "
            f"across {len(samples)} polls ({len(distinct)} distinct (price,timestamp) pairs)"
        )
    spy_moved = len(_distinct_values("SPY")) > 1 if "SPY" in ids else None
    schg_moved = len(_distinct_values("SCHG")) > 1 if "SCHG" in ids else None
    upro_moved = len(_distinct_values("UPRO")) > 1 if "UPRO" in ids else None
    if spy_moved is False and schg_moved is False:
        _p(
            "B: WARNING -- both controls (SPY, SCHG) were also frozen across "
            "this window. This run is UNINFORMATIVE about UPRO specifically "
            "(the market may have been closed, or the whole poll window too "
            "short) -- do not conclude anything about the UPRO bug from this "
            "alone. Re-run with a longer window during confirmed market hours."
        )
    elif upro_moved is False and (spy_moved or schg_moved):
        _p(
            "B: CONCLUSION -- controls moved but UPRO did not: consistent "
            "with DESIGN_market_data.md §2.9's frozen-rates bug being "
            "PERMANENT, not transient."
        )
    elif upro_moved:
        _p(
            "B: CONCLUSION -- UPRO's quote moved this run: the freeze did "
            "NOT reproduce today. Combined with §2.9's original finding, "
            "this would suggest the bug is TRANSIENT/intermittent rather "
            "than a permanent endpoint defect -- re-poll again another day "
            "before reporting it fixed."
        )


# ---------------------------------------------------------------------------
# Section C -- live-payload audit of the six unverified parsers
# ---------------------------------------------------------------------------


def _extract_candle_rows(data: Any) -> list[Mapping[str, Any]]:
    """Mirror get_candles's own row-extraction (client.py) so the raw rows
    audited here are the same rows the real parser sees."""
    if isinstance(data, list):
        return data
    candles_block = data.get("candles", [data])
    if candles_block and isinstance(candles_block[0], dict) and "candles" in candles_block[0]:
        return candles_block[0]["candles"]
    return candles_block


def _check_truthiness(section_label: str, row: Mapping[str, Any], keys: Sequence[str]) -> None:
    """Flag any key in ``keys`` whose value is present but falsy (0, 0.0,
    "", False) -- HANDOFF_2026-09-05.md §4's "fourth instance" defect class:
    an ``or``/``.get(..., default)`` chain silently loses a legitimate falsy
    value to a fallback. This does not fix anything; it only reports.
    """
    for key in keys:
        if key in row and not row[key] and row[key] is not None:
            _p(
                f"C: TRUTHINESS FLAG [{section_label}] key {key!r} is present "
                f"and falsy ({row[key]!r}) -- if this parser reads it via an "
                "or-chain / .get(...,default) rather than `is not None`, a "
                "legitimate value here would be silently replaced by a "
                "fallback (HANDOFF_2026-09-05.md §4's 'fourth instance')"
            )


def section_c_parser_audit(client: Any, output_dir: Path) -> None:
    """HANDOFF_2026-09-05.md §6/§8: audit the six parsers never independently
    re-verified against a live payload this week: ``_parse_candle``,
    ``get_rates``, ``get_eligibility``, ``_parse_order_result``,
    ``get_instruments``, ``get_costs``.

    READ-ONLY: every call here is a GET, or a POST to a read-only
    info/eligibility/costs endpoint that eToro's own docs describe as a
    quote/estimate, not an order-placing call. Nothing in this function can
    place, modify, or close a position. ``_parse_order_result`` cannot be
    exercised here (it needs a real orderId/referenceId, which read-only
    access has no way to produce) -- see the note this prints, and see
    section E2 for the only place this project can genuinely audit it.

    Resolves its own instrument ids (rather than accepting them
    pre-resolved) so that a slow/hanging resolve call is caught by this
    section's own ``run_section`` timeout instead of blocking ``main()``
    before any section-level protection exists -- exactly the gap a smoke
    test against the (eToro-unreachable) dev PC surfaced while building this
    script: a live network call made directly in ``main()``, outside any
    thread-timeout wrapper, hung past its own 120s harness timeout.
    """
    from nightwatch.probes.live_window import diff_keys, PARSER_TRUTHINESS_KEYS

    ids = _resolve_instrument_ids(client, ["SCHG", "UPRO"])
    schg_id, upro_id = ids.get("SCHG"), ids.get("UPRO")

    # --- _parse_candle ---
    if schg_id is not None:
        try:
            resp = client._request(
                "GET",
                f"/api/v1/market-data/instruments/{schg_id}/history/candles/desc/OneDay/5",
                request_class="market_data",
            )
            raw = resp.json()
            _save_json(output_dir, "section_c_candle_raw", raw)
            rows = _extract_candle_rows(raw)
            if rows:
                row_keys = set(rows[0].keys())
                result = diff_keys(row_keys, "_parse_candle")
                _p(
                    f"C: _parse_candle -- missing_from_payload={sorted(result.missing_from_payload)} "
                    f"unused_in_payload={sorted(result.unused_in_payload)}"
                )
                _check_truthiness("_parse_candle", rows[0], sorted(PARSER_TRUTHINESS_KEYS["_parse_candle"]))
            else:
                _p("C: _parse_candle -- live call returned zero rows, cannot audit keys")
        except Exception as e:
            _p(f"C: _parse_candle audit FAILED: {_fmt_exc(e)}")
    else:
        _p("C: _parse_candle -- skipped, SCHG instrument id unresolved")

    # --- get_rates ---
    id_list = [i for i in (schg_id, upro_id) if i is not None]
    if id_list:
        try:
            resp = client._request(
                "GET",
                "/api/v1/market-data/instruments/rates",
                request_class="market_data",
                params={"instrumentIds": ",".join(str(i) for i in id_list)},
            )
            raw = resp.json()
            _save_json(output_dir, "section_c_rates_raw", raw)
            rows = raw.get("rates", raw if isinstance(raw, list) else [])
            if rows:
                row_keys = set(rows[0].keys()) | {"rates"}
                result = diff_keys(row_keys, "get_rates")
                _p(
                    f"C: get_rates -- missing_from_payload={sorted(result.missing_from_payload)} "
                    f"unused_in_payload={sorted(result.unused_in_payload)}"
                )
                _check_truthiness("get_rates", rows[0], sorted(PARSER_TRUTHINESS_KEYS["get_rates"]))
            else:
                _p("C: get_rates -- live call returned zero rows, cannot audit keys")
        except Exception as e:
            _p(f"C: get_rates audit FAILED: {_fmt_exc(e)}")
    else:
        _p("C: get_rates -- skipped, no instrument ids resolved")

    # --- get_eligibility ---
    if id_list:
        try:
            resp = client._request(
                "POST",
                f"/api/v2/trading/info/{client._demo()}eligibility",
                request_class="eligibility",
                json_body={"instrumentIds": id_list},
            )
            raw = resp.json()
            _save_json(output_dir, "section_c_eligibility_raw", raw)
            rows = raw.get("eligibilities", raw.get("eligibility", raw if isinstance(raw, list) else []))
            if rows:
                row_keys = set(rows[0].keys()) | {"eligibilities"}
                for lc in rows[0].get("leverageConfigs", []) or []:
                    row_keys |= set(lc.keys())
                result = diff_keys(row_keys, "get_eligibility")
                _p(
                    f"C: get_eligibility -- missing_from_payload={sorted(result.missing_from_payload)} "
                    f"unused_in_payload={sorted(result.unused_in_payload)}"
                )
                _check_truthiness("get_eligibility", rows[0], sorted(PARSER_TRUTHINESS_KEYS["get_eligibility"]))
            else:
                _p("C: get_eligibility -- live call returned zero rows, cannot audit keys")
        except Exception as e:
            _p(f"C: get_eligibility audit FAILED: {_fmt_exc(e)}")
    else:
        _p("C: get_eligibility -- skipped, no instrument ids resolved")

    # --- get_instruments ---
    if id_list:
        try:
            resp = client._request(
                "GET",
                "/api/v1/market-data/instruments",
                request_class="market_data",
                params={"instrumentIds": ",".join(str(i) for i in id_list)},
            )
            raw = resp.json()
            _save_json(output_dir, "section_c_instruments_raw", raw)
            rows = raw.get("instrumentDisplayDatas", raw if isinstance(raw, list) else [])
            if rows:
                row_keys = set(rows[0].keys()) | {"instrumentDisplayDatas"}
                result = diff_keys(row_keys, "get_instruments")
                _p(
                    f"C: get_instruments -- (this parser does no field-level "
                    f"extraction; every row key below is currently unread) "
                    f"unused_in_payload={sorted(result.unused_in_payload)}"
                )
            else:
                _p("C: get_instruments -- live call returned zero rows, cannot audit keys")
        except Exception as e:
            _p(f"C: get_instruments audit FAILED: {_fmt_exc(e)}")
    else:
        _p("C: get_instruments -- skipped, no instrument ids resolved")

    # --- get_costs ---
    if schg_id is not None:
        try:
            resp = client._request(
                "POST",
                f"/api/v2/trading/info/{client._demo()}costs",
                request_class="read",
                json_body={
                    "instrumentId": schg_id,
                    "amount": 100.0,
                    "leverage": 1,
                    "transaction": "buy",
                },
            )
            raw = resp.json()
            _save_json(output_dir, "section_c_costs_raw", raw)
            row_keys = set(raw.keys()) if isinstance(raw, dict) else set()
            result = diff_keys(row_keys, "get_costs")
            _p(
                f"C: get_costs -- (this parser does no field-level extraction "
                f"at all; every top-level key below is currently unread) "
                f"keys={sorted(row_keys)} unused_in_payload={sorted(result.unused_in_payload)}"
            )
        except Exception as e:
            _p(f"C: get_costs audit FAILED: {_fmt_exc(e)}")
    else:
        _p("C: get_costs -- skipped, SCHG instrument id unresolved")

    # --- _parse_order_result ---
    _p(
        "C: _parse_order_result -- CANNOT be exercised read-only (needs a "
        "real orderId/referenceId, which no read-only endpoint produces). "
        "If section E2 runs this session, its own order-lookup response is "
        "the only live evidence this project has for this parser; see the "
        "E2 output file for that audit instead."
    )


# ---------------------------------------------------------------------------
# Section D -- sleeve arithmetic re-measurement
# ---------------------------------------------------------------------------


def section_d_sleeve_arithmetic(client: Any, output_dir: Path) -> None:
    """HANDOFF_2026-09-05.md §11's explicit ask: re-derive sleeve arithmetic
    from THIS probe, not from any number in any document, immediately before
    any migration. READ-ONLY: ``get_balance``, ``get_portfolio``,
    ``get_candles`` only.

    Resolves its own instrument ids -- see section_c_parser_audit's
    docstring for why that must happen inside the timeout-wrapped section,
    not once in ``main()``.
    """
    from nightwatch.probes.live_window import verify_sleeve_arithmetic

    ids = _resolve_instrument_ids(client, ["SCHG", "UPRO"])
    schg_id, upro_id = ids.get("SCHG"), ids.get("UPRO")

    try:
        balance = client.get_balance()
        _p(
            f"D: broker_available_cash=${balance.available_cash:,.2f} "
            f"credit=${balance.credit:,.2f}"
        )
    except Exception as e:
        _p(f"D: get_balance FAILED: {_fmt_exc(e)} -- cannot compute sleeve_cash")
        balance = None

    try:
        positions = client.get_portfolio()
    except Exception as e:
        _p(f"D: get_portfolio FAILED: {_fmt_exc(e)} -- aborting sleeve measurement")
        return

    _save_json(
        output_dir,
        "section_d_portfolio_raw",
        [dict(p.raw) for p in positions],
    )

    by_instrument: dict[int, list[Any]] = {}
    for p in positions:
        by_instrument.setdefault(p.instrument_id, []).append(p)

    schg_positions = by_instrument.get(schg_id, []) if schg_id is not None else []
    upro_positions = by_instrument.get(upro_id, []) if upro_id is not None else []
    other_instrument_ids = [iid for iid in by_instrument if iid not in (schg_id, upro_id)]

    _p(
        f"D: position counts -- SCHG={len(schg_positions)} UPRO={len(upro_positions)} "
        f"other_instruments={len(other_instrument_ids)} "
        f"(distinct ids: {sorted(other_instrument_ids)}) "
        f"total_positions={len(positions)}"
    )

    schg_amounts = [p.amount for p in schg_positions]
    if schg_amounts:
        _p(
            f"D: SCHG per-position amount -- mean=${statistics.mean(schg_amounts):.2f} "
            f"median=${statistics.median(schg_amounts):.2f} "
            f"min=${min(schg_amounts):.2f} max=${max(schg_amounts):.2f} "
            f"sum=${sum(schg_amounts):,.2f}"
        )

    # Price for market value: candle close, NEVER the rates endpoint
    # (DESIGN_market_data.md §2.9/§4 -- the rates endpoint is unsafe for
    # anything price-critical, UPRO's freeze being the proof).
    prices: dict[str, float | None] = {"SCHG": None, "UPRO": None}
    for symbol, iid in (("SCHG", schg_id), ("UPRO", upro_id)):
        if iid is None:
            continue
        try:
            candles = client.get_candles(iid, direction="backward", interval="OneDay", count=2)
            if candles:
                prices[symbol] = candles[0].close
                _p(f"D: {symbol} latest candle close=${candles[0].close:.4f} date={candles[0].date}")
        except Exception as e:
            _p(f"D: fetching {symbol} candle FAILED: {_fmt_exc(e)}")

    schg_value = (sum(p.units for p in schg_positions) * prices["SCHG"]) if prices["SCHG"] is not None else None
    upro_value = (sum(p.units for p in upro_positions) * prices["UPRO"]) if prices["UPRO"] is not None else None

    measured: dict[str, float] = {
        "schg_position_count": float(len(schg_positions)),
        "upro_position_count": float(len(upro_positions)),
    }
    if schg_amounts:
        measured["schg_amount_mean"] = statistics.mean(schg_amounts)
        measured["schg_amount_median"] = statistics.median(schg_amounts)
    if schg_value is not None:
        measured["schg_value_usd"] = schg_value
        _p(f"D: SCHG market value = ${schg_value:,.2f}")
    if upro_value is not None:
        measured["upro_value_usd"] = upro_value
        _p(f"D: UPRO market value = ${upro_value:,.2f}")
    if schg_value is not None and upro_value is not None and balance is not None:
        sleeve_equity = schg_value + upro_value + balance.available_cash
        measured["sleeve_equity_usd"] = sleeve_equity
        _p(f"D: sleeve_equity (SCHG + UPRO + broker_available_cash) = ${sleeve_equity:,.2f}")

    _save_json(output_dir, "section_d_measured_figures", measured)

    from_doc = DOCUMENTED_SLEEVE_FIGURES
    mismatches = verify_sleeve_arithmetic(measured, from_doc, tolerance_pct=0.05)
    if mismatches:
        _p("D: !!! DISAGREEMENT between this probe's measurement and DESIGN_market_data.md §10.11 !!!")
        for m in mismatches:
            pct = f"{m.pct_diff:.1%}" if m.pct_diff is not None else "n/a"
            _p(
                f"D:   {m.quantity}: documented={m.documented:,.2f} "
                f"measured={m.measured:,.2f} diff={m.abs_diff:,.2f} ({pct})"
            )
    else:
        _p("D: no disagreement (beyond 5% tolerance) between this measurement and the documented §10.11 figures")


# ---------------------------------------------------------------------------
# Section E1 -- the fee question, read-only half
# ---------------------------------------------------------------------------


def section_e1_fee_readonly(client: Any, output_dir: Path) -> None:
    """The operator's fee report, read-only half. ALWAYS SAFE TO RUN --
    nothing in this function places, modifies, or closes an order. Queries
    ``get_costs``/``get_eligibility`` for SCHG and UPRO, and partitions all
    120 closed trades by instrument to check whether plain-ETF (SCHG) trades
    ever carry a nonzero fee, versus UPRO/other instruments.

    Resolves its own instrument ids -- see section_c_parser_audit's
    docstring for why.
    """
    from nightwatch.probes.live_window import partition_trades_by_instrument

    ids = _resolve_instrument_ids(client, ["SCHG", "UPRO"])
    schg_id, upro_id = ids.get("SCHG"), ids.get("UPRO")

    for symbol, iid in (("SCHG", schg_id), ("UPRO", upro_id)):
        if iid is None:
            continue
        try:
            costs = client.get_costs(iid, 100.0)
            _save_json(output_dir, f"section_e1_costs_{symbol}", costs)
            _p(f"E1: get_costs({symbol}, $100) raw keys = {sorted(costs.keys())}")
            # Print any key that looks fee-shaped, without assuming a name.
            fee_like = {k: v for k, v in costs.items() if "fee" in k.lower()}
            if fee_like:
                _p(f"E1: {symbol} fee-shaped fields in get_costs response: {fee_like}")
            else:
                _p(f"E1: {symbol} -- no key containing 'fee' found in get_costs response")
        except Exception as e:
            _p(f"E1: get_costs({symbol}) FAILED: {_fmt_exc(e)}")

    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trades = client.get_trade_history(start_date="1970-01-01", end_date=today, page_size=500)
        _p(f"E1: fetched {len(trades)} closed-trade rows")
        trade_dicts = [dataclasses.asdict(t) for t in trades]
        _save_json(output_dir, "section_e1_trade_history_raw", trade_dicts)

        summary = partition_trades_by_instrument(trade_dicts)
        for instrument_id, s in sorted(summary.items(), key=lambda kv: -abs(kv[1].sum_fees)):
            label = (
                "SCHG" if instrument_id == schg_id
                else "UPRO" if instrument_id == upro_id
                else f"instrument_id={instrument_id}"
            )
            _p(
                f"E1: {label}: n={s.count} sum(fees)=${s.sum_fees:,.2f} "
                f"sum(investment)=${s.sum_investment:,.2f} any_nonzero_fee={s.any_nonzero_fee}"
            )
        if schg_id in summary:
            if summary[schg_id].any_nonzero_fee:
                _p(
                    "E1: CONTRADICTS the operator's 'ETFs are always 0 fees' "
                    "hypothesis -- at least one closed SCHG trade carries a "
                    "nonzero fee."
                )
            else:
                _p(
                    "E1: CONSISTENT with the operator's hypothesis for closed "
                    "trades -- every closed SCHG trade in this account's full "
                    "history has fees == 0. This does not by itself prove a "
                    "future SCHG buy will disclose no fee (§E2 is the only "
                    "way to check a fresh order's own disclosure)."
                )
    except Exception as e:
        _p(f"E1: get_trade_history FAILED: {_fmt_exc(e)}")


# ---------------------------------------------------------------------------
# Section E2 -- the fee question, real-money half. OFF BY DEFAULT.
# ---------------------------------------------------------------------------

#: The operator must set this environment variable to exactly this string
#: for section E2 to even be considered -- a flag alone is not enough
#: (defense in depth, matching this codebase's own "arming takes two keys"
#: pattern in nightwatch.config).
E2_ARM_ENV_VAR = "NIGHTWATCH_PROBE_E2_ARM"
E2_ARM_PHRASE = "I UNDERSTAND THIS SPENDS REAL MONEY"
#: The exact phrase the operator must type (or pass via --e2-confirm) for
#: section E2 to proceed, on top of the environment variable above.
E2_CONFIRM_PHRASE = "PLACE REAL ORDER"
#: eToro's own live-verified minimum for SCHG (DESIGN.md's
#: min_order_size docstring, config.py: minPositionExposure = 50.0).
E2_ORDER_AMOUNT_USD = 50.0


def section_e2_real_money_order(
    client: Any, output_dir: Path, *, confirm_phrase: str | None
) -> None:
    """PLACES ONE REAL $50 SCHG BUY ORDER. Spends real money. Never called
    by ``run_all`` -- only reachable via ``--e2`` on the command line, and
    even then this function itself re-checks the environment-variable arm
    key and the typed confirmation before doing anything, exactly the way
    ``nightwatch.config``'s ``is_armed`` requires two independent keys before
    the strategy engine may trade. This is the operator's decision alone;
    building this capability is not the same as authorizing its use (see the
    module docstring and doc/RUNBOOK_LIVE_WINDOW_2026-09-11.md).
    """
    from nightwatch.probes.live_window import reconcile_fee_hypothesis

    if os.environ.get(E2_ARM_ENV_VAR, "") != E2_ARM_PHRASE:
        _p(
            f"E2: REFUSING -- environment variable {E2_ARM_ENV_VAR} is not set "
            f"to the exact required phrase. Nothing was sent to eToro."
        )
        return

    if confirm_phrase != E2_CONFIRM_PHRASE:
        _p(
            f"E2: REFUSING -- the typed confirmation did not match "
            f"{E2_CONFIRM_PHRASE!r}. Nothing was sent to eToro."
        )
        return

    ids = _resolve_instrument_ids(client, ["SCHG"])
    schg_id = ids.get("SCHG")
    if schg_id is None:
        _p("E2: REFUSING -- SCHG instrument id could not be resolved.")
        return

    _p(
        f"E2: ARMED. Placing one real ${E2_ORDER_AMOUNT_USD:.2f} SCHG buy order "
        f"(instrument_id={schg_id}). This is real money."
    )

    try:
        balance_before = client.get_balance()
        _p(f"E2: cash BEFORE = ${balance_before.available_cash:,.2f}")
    except Exception as e:
        _p(f"E2: get_balance (before) FAILED: {_fmt_exc(e)} -- aborting E2, no order sent")
        return

    try:
        client.check_eligibility_for_order(schg_id, E2_ORDER_AMOUNT_USD)
    except Exception as e:
        _p(f"E2: eligibility check FAILED: {_fmt_exc(e)} -- aborting E2, no order sent")
        return

    request_id = str(uuid.uuid4())
    try:
        order_id = client.place_buy_order(request_id, schg_id, E2_ORDER_AMOUNT_USD)
        _p(f"E2: order submitted, order_id={order_id!r} request_id={request_id}")
    except Exception as e:
        _p(f"E2: place_buy_order FAILED: {_fmt_exc(e)}")
        return

    try:
        poll_result = client.poll_order_to_terminal(order_id=order_id, timeout_seconds=90.0)
        order = poll_result.order
        _save_json(output_dir, "section_e2_order_result_raw", order.raw)
        _p(
            f"E2: order terminal status={order.status.name} timed_out={poll_result.timed_out} "
            f"fill_price={order.fill_price} fill_units={order.fill_units} "
            f"fill_amount_usd={order.fill_amount_usd} reject_reason={order.reject_reason}"
        )
        _p(f"E2: full order raw payload keys = {sorted(order.raw.keys())}")
        fee_like = {k: v for k, v in order.raw.items() if "fee" in k.lower()}
        _p(f"E2: fee-shaped fields in the order response: {fee_like or 'NONE FOUND'}")
    except Exception as e:
        _p(f"E2: poll_order_to_terminal FAILED: {_fmt_exc(e)} -- order may still have been placed, check manually")
        return

    try:
        balance_after = client.get_balance()
        _p(f"E2: cash AFTER = ${balance_after.available_cash:,.2f}")
    except Exception as e:
        _p(f"E2: get_balance (after) FAILED: {_fmt_exc(e)}")
        balance_after = None

    position_amount = None
    if order.position_id is not None:
        try:
            positions = client.get_portfolio()
            match = next((p for p in positions if p.position_id == order.position_id), None)
            if match is not None:
                position_amount = match.amount
                _save_json(output_dir, "section_e2_new_position_raw", dict(match.raw))
                _p(
                    f"E2: matched new position {match.position_id}: "
                    f"amount=${match.amount:.2f} units={match.units} openRate={match.entry_price}"
                )
        except Exception as e:
            _p(f"E2: fetching new position FAILED: {_fmt_exc(e)}")

    # Deliberately NOT an `x and y or z` chain (HANDOFF_2026-09-05.md §4's
    # "fourth instance": a legitimate falsy value, e.g. a genuine 0.0 fee,
    # must not be lost to a fallback here of all places).
    disclosed_fee_raw = next(iter(fee_like.values())) if fee_like else None
    try:
        disclosed_fee = float(disclosed_fee_raw) if disclosed_fee_raw is not None else None
    except (TypeError, ValueError):
        disclosed_fee = None

    if balance_after is not None:
        recon = reconcile_fee_hypothesis(
            cash_before=balance_before.available_cash,
            cash_after=balance_after.available_cash,
            order_amount_usd=E2_ORDER_AMOUNT_USD,
            disclosed_fee=disclosed_fee,
            position_amount=position_amount,
        )
        _save_json(output_dir, "section_e2_fee_reconciliation", dataclasses.asdict(recon))
        _p(f"E2: cash_delta=${recon.cash_delta:.2f} (order amount=${E2_ORDER_AMOUNT_USD:.2f})")
        _p(f"E2: VERDICT -- {recon.verdict}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--sections", default="A,C,D,E1,B",
        help="comma-separated subset of A,B,C,D,E1 to run (E2 is never in this list, see --e2)",
    )
    parser.add_argument("--section-timeout", type=float, default=300.0, help="seconds per section")
    parser.add_argument("--poll-count", type=int, default=6, help="section B: number of rate polls")
    parser.add_argument("--poll-interval", type=float, default=300.0, help="section B: seconds between polls")
    parser.add_argument(
        "--role", default="read", choices=("read", "write"),
        help="eToro credential role for sections A-D/E1 (default read -- structurally cannot place orders "
             "even if this script had a bug, see DESIGN §5's separate read/write credential pair)",
    )
    parser.add_argument("--e2", action="store_true", help="also attempt section E2 (real money, see module docstring)")
    parser.add_argument(
        "--e2-confirm", default=None,
        help=f"must equal {E2_CONFIRM_PHRASE!r} exactly, or omit to be prompted interactively",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir
    _p(f"probe_live_2026-09-11 starting. output_dir={output_dir}")

    try:
        from nightwatch.config import load_config
        from nightwatch.etoro.client import EtoroClient
        from nightwatch.host import get_host
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
    if config.environment.value == "real":
        _p(
            "!!! THIS CONFIG POINTS AT THE REAL-MONEY ACCOUNT. Sections B-E1 "
            "are read-only and safe regardless; E2 (if --e2 is passed) will "
            "place a real order against this real account. !!!"
        )

    read_client = EtoroClient(config.etoro, role=args.role)

    requested = {s.strip().upper() for s in args.sections.split(",") if s.strip()}

    if "A" in requested:
        run_section("A", section_a_chaquopy_imports, output_dir, timeout_s=args.section_timeout)

    # NOTE: instrument-id resolution is NOT done here in main(). A smoke test
    # against the (eToro-unreachable) dev PC found that a live network call
    # made directly in main(), outside any section's thread-timeout, can hang
    # past this script's own --section-timeout with nothing to stop it --
    # exactly the failure mode this whole probe exists to avoid repeating.
    # Each section below resolves its own ids internally, inside its own
    # run_section-wrapped thread, so the timeout actually covers it.

    if "C" in requested:
        run_section("C", section_c_parser_audit, read_client, output_dir, timeout_s=args.section_timeout)
    if "D" in requested:
        run_section("D", section_d_sleeve_arithmetic, read_client, output_dir, timeout_s=args.section_timeout)
    if "E1" in requested:
        run_section("E1", section_e1_fee_readonly, read_client, output_dir, timeout_s=args.section_timeout)
    if "B" in requested:
        run_section(
            "B", section_b_upro_rates_freeze, read_client, output_dir,
            poll_count=args.poll_count, poll_interval_s=args.poll_interval,
            timeout_s=max(args.section_timeout, args.poll_count * args.poll_interval + 120.0),
        )

    if args.e2:
        confirm = args.e2_confirm
        if confirm is None:
            try:
                confirm = input(
                    f"Type exactly {E2_CONFIRM_PHRASE!r} to place one real "
                    f"${E2_ORDER_AMOUNT_USD:.2f} SCHG buy order, anything else to abort: "
                )
            except EOFError:
                confirm = ""
        write_client = EtoroClient(config.etoro, role="write")
        run_section(
            "E2", section_e2_real_money_order, write_client, output_dir,
            confirm_phrase=confirm, timeout_s=120.0,
        )
        write_client.close()

    read_client.close()
    _p("probe_live_2026-09-11 finished. See output_dir for saved raw payloads.")
    return 0


def run_all() -> None:
    """Chaquopy/Kotlin entry point mirroring android-probe-deps/probe/nwprobe.py's
    ``run_all()`` convention -- callable directly via
    ``Python.getInstance().getModule("probe_live_2026_09_11").callAttr("run_all")``
    if this file is copied into an on-device probe harness's Python source set
    under that module name. Runs with every default (read-only sections A/C/D/
    E1/B; E2 never runs from this entry point).
    """
    main([])


if __name__ == "__main__":
    sys.exit(main())
