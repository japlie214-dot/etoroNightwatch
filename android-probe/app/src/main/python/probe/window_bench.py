"""On-device measurement: the cost of parsing a FULL-HISTORY SPY payload vs the
previously-used 276-bar window, following the REAL code path
(``nightwatch.market.source.TiingoClient.get_eod_history``'s row-parsing,
``nightwatch.market.window.validate_window_completeness``,
``nightwatch.strategy.cone.cone_5_z``, and
``nightwatch.market.window._spy_context_with_provisional``) -- not an
invented simplified parse.

Ad-hoc probe/measurement code (task instruction: do not modify application
source; keep probe code inside android-probe/ or scratch). Reads two
already-on-device Tiingo raw JSON files (pushed by adb beforehand -- this
module makes no network call itself, isolating pure parse+compute cost from
fetch latency) and reports wall-clock time and peak Python-level memory
(tracemalloc) for each stage, for both payloads, plus a same-process ratio.

Both input payloads are REAL Tiingo responses (one pulled directly from the
device in an earlier probe run -- pulled_probe_output's 276-bar section_f
file; one pulled fresh from the PC, since Tiingo (unlike eToro) IS reachable
from the dev PC -- confirmed this session with a plain 403-on-bad-token
response, then a real 200 with the project's own API key). Neither number
below is synthetic.
"""

from __future__ import annotations

import gc
import json
import time
import tracemalloc
from pathlib import Path
from typing import Any

__all__ = ["run"]


def _p(buf: list[str], msg: str) -> None:
    line = f"BENCH: {msg}"
    print(line, flush=True)
    buf.append(line)


def _parse_eod_rows(rows: list[dict[str, Any]]):
    """Byte-for-byte mirror of TiingoClient.get_eod_history's row parsing
    (nightwatch/market/source.py) -- same field reads, same EodBar shape,
    duplicated here (not imported) only because the real EodBar is a frozen
    dataclass and importing it needs nightwatch on sys.path, which this
    module also does below; kept as a plain tuple list would understate
    real cost, so we build the actual dataclass.
    """
    from nightwatch.market.source import EodBar

    return [
        EodBar(
            date=str(row["date"])[:10],
            close=float(row["close"]),
            adj_close=float(row["adjClose"]),
            split_factor=float(row.get("splitFactor", 1.0)),
            div_cash=float(row.get("divCash", 0.0)),
        )
        for row in rows
    ]


def _measure_one(buf: list[str], label: str, raw_bytes: bytes) -> dict[str, Any]:
    import sys

    sys_root = str(Path(__file__).resolve().parents[2])  # .../src/main/python
    if sys_root not in sys.path:
        sys.path.insert(0, sys_root)

    from nightwatch.market.window import (
        MIN_HISTORY_BARS,
        _spy_context_with_provisional,
        validate_window_completeness,
    )
    from nightwatch.strategy.cone import cone_5_z

    result: dict[str, Any] = {"label": label, "input_bytes": len(raw_bytes)}

    # -- stage 1: JSON parse (response.json() equivalent) --------------
    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()
    rows = json.loads(raw_bytes)
    t_json = time.perf_counter() - t0
    _, peak_json = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result["n_rows"] = len(rows)
    result["json_parse_seconds"] = t_json
    result["json_parse_peak_bytes"] = peak_json

    # -- stage 2: build EodBar list (the real get_eod_history parse step) --
    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()
    bars = _parse_eod_rows(rows)
    t_eodbar = time.perf_counter() - t0
    _, peak_eodbar = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result["eodbar_build_seconds"] = t_eodbar
    result["eodbar_build_peak_bytes"] = peak_eodbar

    # -- stage 3: validate_window_completeness (gate 10 item 2 interface) --
    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()
    completeness = validate_window_completeness(
        "SPY", bars, min_sessions=min(MIN_HISTORY_BARS, len(bars))
    )
    t_complete = time.perf_counter() - t0
    _, peak_complete = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result["completeness_seconds"] = t_complete
    result["completeness_peak_bytes"] = peak_complete
    result["completeness_passed"] = completeness.passed
    result["completeness_reason"] = completeness.reason

    # -- stage 4: downstream computation -- cone_5_z + ATH/drawdown/tier --
    provisional_close = bars[-1].close  # stand-in: real session splices a fresh
    # IEX/candle quote here; using the window's own last close isolates the
    # ATH/drawdown/cone COMPUTE cost from the provisional-close network call,
    # which is out of scope for this parse/compute measurement.
    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()
    closes = [bar.adj_close for bar in bars] + [bars[-1].adj_close]
    z = cone_5_z(closes, horizon=5)
    spy_context = _spy_context_with_provisional(bars, provisional_close, open_lot_entry_spy_ath=())
    t_derive = time.perf_counter() - t0
    _, peak_derive = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result["derive_seconds"] = t_derive
    result["derive_peak_bytes"] = peak_derive
    result["cone_5_z"] = z
    result["ath"] = spy_context.ath
    result["close"] = spy_context.close
    result["drawdown"] = spy_context.drawdown
    result["tier"] = spy_context.tier

    result["total_seconds"] = t_json + t_eodbar + t_complete + t_derive
    result["peak_bytes_max_stage"] = max(peak_json, peak_eodbar, peak_complete, peak_derive)

    _p(
        buf,
        f"{label}: n_rows={result['n_rows']} bytes={result['input_bytes']} "
        f"json={t_json*1000:.2f}ms eodbar={t_eodbar*1000:.2f}ms "
        f"completeness={t_complete*1000:.2f}ms derive={t_derive*1000:.2f}ms "
        f"TOTAL={result['total_seconds']*1000:.2f}ms "
        f"peak_bytes(max stage)={result['peak_bytes_max_stage']} "
        f"cone_5_z={z} ath={spy_context.ath} dd={spy_context.drawdown} tier={spy_context.tier}",
    )
    return result


def run(payload_276_path: str, payload_full_path: str, output_dir: str, repeats: int = 3) -> str:
    """Kotlin entry point. Reads both payload files (already on-device,
    pushed by adb beforehand -- no network call happens here), runs each
    through the real parse/derive code path ``repeats`` times (first run
    plus two more, so a JIT/cache warmup effect is visible rather than
    hidden), and writes a JSON report to ``output_dir``.
    """
    buf: list[str] = []
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    _p(buf, f"payload_276_path={payload_276_path}")
    _p(buf, f"payload_full_path={payload_full_path}")

    all_runs: dict[str, list[dict[str, Any]]] = {"276_bar": [], "full_history": []}

    try:
        raw_276 = Path(payload_276_path).read_bytes()
        raw_full = Path(payload_full_path).read_bytes()
    except Exception as e:
        _p(buf, f"FATAL: could not read input payload files: {e!r}")
        report = {"error": repr(e), "log": buf}
        (out / "window_bench_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return "\n".join(buf)

    for i in range(repeats):
        _p(buf, f"--- repeat {i+1}/{repeats} ---")
        all_runs["276_bar"].append(_measure_one(buf, f"276_bar_run{i+1}", raw_276))
        all_runs["full_history"].append(_measure_one(buf, f"full_history_run{i+1}", raw_full))

    # Ratio using the LAST (warmed-up) run of each.
    last_276 = all_runs["276_bar"][-1]
    last_full = all_runs["full_history"][-1]
    ratio = {
        "bytes_ratio": last_full["input_bytes"] / last_276["input_bytes"],
        "rows_ratio": last_full["n_rows"] / last_276["n_rows"],
        "total_seconds_ratio": last_full["total_seconds"] / last_276["total_seconds"]
        if last_276["total_seconds"] > 0
        else None,
        "peak_bytes_ratio": last_full["peak_bytes_max_stage"] / last_276["peak_bytes_max_stage"]
        if last_276["peak_bytes_max_stage"] > 0
        else None,
        "276_bar_last": last_276,
        "full_history_last": last_full,
    }
    _p(
        buf,
        f"RATIO (full/276, last run): bytes={ratio['bytes_ratio']:.3f} "
        f"rows={ratio['rows_ratio']:.3f} total_seconds={ratio['total_seconds_ratio']} "
        f"peak_bytes={ratio['peak_bytes_ratio']}",
    )

    report = {"runs": all_runs, "ratio": ratio, "log": buf}
    report_path = out / "window_bench_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    _p(buf, f"report written to {report_path}")

    return "\n".join(buf)
