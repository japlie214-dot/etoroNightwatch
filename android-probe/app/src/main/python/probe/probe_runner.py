"""On-device orchestration entry point for tonight's read-only probe run.

Runs, in order:

1. ``probe_live_2026_09_11.main()`` (a byte-for-byte copy of the concurrent
   agent's ``tools/probe_live_2026-09-11.py`` -- see that file's own
   docstring, which explicitly anticipates being "copied into an on-device
   probe harness's Python source set" this way) restricted to
   ``--sections A,C,D,E1,F`` -- section B (needs an open US market, closed
   tonight, Saturday 2026-09-05) is deliberately excluded from this call, and
   section E2 (places a real order) is never reachable from anything this
   module exposes at all. Section F (added 2026-09-05, read-only) exercises
   ``market.window``/``strategy.cone`` end-to-end against real Tiingo/eToro
   payloads for the first time -- see that section's own docstring in the
   copied probe script.
2. The sharpened E1 fee audit (``nightwatch.probes.fee_audit``), which goes
   beyond that copied script's own read-only ``section_e1_fee_readonly``
   (an aggregate only) to a full per-trade table, groupings by instrument and
   by leverage/short, the complete fee-bearing subset, correlation checks,
   a dividend/account-field exposure scan, and a strict reconciliation
   against the two known account-wide totals.

Called from Kotlin via ``PyBridge.runLiveProbe`` -- see
``doc/RUNBOOK_DEVICE_PROBE.md`` for the exact adb commands this depends on
(pushing ``config.toml``/``.secrets.env`` onto the device beforehand under
``<config_dir>/config/``, and pulling the saved JSON payloads back after).

Every network call this makes goes through ``nightwatch.etoro.client``,
which already carries its own timeouts (``config.etoro.timeout_seconds``);
nothing here adds an untimed call. Every line printed goes through ``_p``,
flushed immediately, mirroring the copied probe's own gotcha-avoidance.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
# This file lives at .../android-probe/app/src/main/python/probe/probe_runner.py.
# Its parent's parent is the python source ROOT (.../src/main/python), where
# the copied probe_live_2026_09_11.py sits as a top-level module. Chaquopy's
# own source-set wiring already puts that root on sys.path, but this is a
# no-op-safe belt-and-suspenders insert, the same pattern
# probe_live_2026_09_11.py itself uses for the nightwatch package.
_SOURCE_ROOT = _HERE.parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))


def _p(buf: list[str], msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    line = f"{ts} RUNNER: {msg}"
    print(line, flush=True)
    buf.append(line)


def _save_json(output_dir: Path, name: str, obj: Any) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"{ts}_{name}.json"
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return path


#: The sections ``run_default`` requests when the caller names none. Exactly
#: the literal this module hardcoded before 2026-09-11, so every existing
#: caller (``PyBridge.runLiveProbe``, the UI button) keeps its old behaviour
#: byte-for-byte: B excluded (market was closed when this was written),
#: E2 never present.
DEFAULT_SECTIONS = "A,C,D,E1,F"

#: Sections this module refuses to request under any circumstances, whatever
#: the caller passes. E2 places a REAL order with REAL money and is not
#: authorised from any on-device route; ``--e2`` (the copied probe's separate
#: opt-in flag) is likewise never constructed anywhere in this file. This is
#: a fail-closed allowlist-complement: an unrecognised token is passed
#: through to the probe's own argparse (which rejects it loudly), but a
#: forbidden one is stripped and reported here, before any import happens.
FORBIDDEN_SECTIONS = frozenset({"E2"})


def _sanitize_sections(buf: list[str], sections: str | None) -> str:
    """Normalise a caller-supplied ``--sections`` string and strip anything in
    :data:`FORBIDDEN_SECTIONS`.

    Returns the sanitized comma-joined string. An empty/None input falls back
    to :data:`DEFAULT_SECTIONS`; an input that consisted ONLY of forbidden
    sections also falls back to the default rather than passing an empty
    ``--sections`` through (which the probe's argparse would read as "no
    sections", a silent no-op that looks like success).
    """
    if not sections or not sections.strip():
        _p(buf, f"sections not specified; using default {DEFAULT_SECTIONS!r}")
        return DEFAULT_SECTIONS

    requested = [tok.strip().upper() for tok in sections.split(",") if tok.strip()]
    kept = [tok for tok in requested if tok not in FORBIDDEN_SECTIONS]
    dropped = [tok for tok in requested if tok in FORBIDDEN_SECTIONS]
    for tok in dropped:
        _p(
            buf,
            f"REFUSED section {tok!r}: it is on this module's FORBIDDEN_SECTIONS list "
            "(real money / not authorised from any on-device route). Stripped from the request.",
        )
    if not kept:
        _p(buf, f"nothing left after sanitizing {sections!r}; falling back to {DEFAULT_SECTIONS!r}")
        return DEFAULT_SECTIONS
    result = ",".join(kept)
    _p(buf, f"sections requested={sections!r} -> sanitized={result!r}")
    return result


def run_sections(config_dir: str, output_dir: str, sections: str | None = None) -> str:
    """Explicitly-named sibling of :func:`run_default`, for callers that want
    the parameter to be obvious at the call site. Identical behaviour; see
    :func:`run_default` for the contract.
    """
    return run_default(config_dir, output_dir, sections)


def run_default(config_dir: str, output_dir: str, sections: str | None = None) -> str:
    """Kotlin entry point. Runs the requested sections (default A, C, D, E1,
    F), then the E1-plus fee audit. Never runs E2 (real money) -- it is
    stripped by :func:`_sanitize_sections` whatever the caller asks for, and
    the copied probe's separate ``--e2`` opt-in flag is never constructed
    here at all.

    Args:
        sections: comma-separated section list passed through to the copied
            probe's ``--sections``. ``None``/empty means
            :data:`DEFAULT_SECTIONS` (``"A,C,D,E1,F"``), which is exactly
            what this function did unconditionally before 2026-09-11 -- so
            existing callers see no behaviour change. Section B needs an OPEN
            US market and can take ~25 minutes; callers requesting it must
            raise their own timeout accordingly.
        config_dir: a directory containing a ``config/`` subfolder with
            ``config.toml`` and ``.secrets.env`` (pushed onto the device by
            the operator beforehand -- see the runbook). Passed through as
            ``NIGHTWATCH_HOME`` so :func:`nightwatch.host.repo_root` resolves
            to it instead of trying to compute a desktop repo path that does
            not exist on-device.
        output_dir: where every raw payload and report this run produces is
            written, so the operator can ``adb pull`` it back afterward.

    Returns a short summary string (also printed line-by-line as it runs, so
    Chaquopy's logcat has it even if this function itself is later killed).
    Never raises -- every failure is caught and reported in the returned
    string, so a crash partway through still surfaces whatever ran before it
    rather than losing everything silently (mirrors the copied probe's own
    per-section isolation, one level up).
    """
    buf: list[str] = []
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    os.environ["NIGHTWATCH_HOME"] = config_dir
    _p(buf, f"NIGHTWATCH_HOME set to {config_dir!r}")
    _p(buf, f"output_dir={out}")
    resolved_sections = _sanitize_sections(buf, sections)
    _p(
        buf,
        "NOTE: NIGHTWATCH_HOME is only read the first time nightwatch.host is "
        "imported in this process. If you need to point at a different "
        "config_dir, force-stop and relaunch the app first so a fresh "
        "process picks up the new value.",
    )

    try:
        import probe_live_2026_09_11 as probe
    except Exception as e:
        _p(buf, f"FATAL: cannot import copied probe_live_2026_09_11: {e!r}")
        _p(buf, traceback.format_exc())
        return "\n".join(buf)

    _p(buf, f"=== sections {resolved_sections} (E2 never runs from here; --e2 is never passed) ===")
    try:
        probe.main(["--output-dir", str(out), "--sections", resolved_sections])
    except SystemExit as e:
        _p(buf, f"probe_live_2026_09_11.main() exited with code {e.code!r} (argparse error or explicit return)")
    except Exception as e:
        _p(buf, f"probe_live_2026_09_11.main() raised: {e!r}")
        _p(buf, traceback.format_exc())

    _p(buf, "=== E1-plus: full per-trade fee audit (nightwatch.probes.fee_audit) ===")
    try:
        _run_fee_audit_plus(buf, out)
    except Exception as e:
        _p(buf, f"E1-plus FAILED: {e!r}")
        _p(buf, traceback.format_exc())

    _p(buf, "=== run_default finished -- see output_dir for every saved file ===")
    return "\n".join(buf)


def _run_fee_audit_plus(buf: list[str], output_dir: Path) -> None:
    """The sharpened E1 analysis: HANDOFF's fuller brief for tonight, built
    on top of (not instead of) the copied probe's own read-only
    ``section_e1_fee_readonly``. See ``nightwatch.probes.fee_audit`` for the
    pure arithmetic; this function only does I/O and reporting.
    """
    from nightwatch.config import load_config
    from nightwatch.etoro.client import EtoroClient
    from nightwatch.host import get_host
    from nightwatch.probes.fee_audit import (
        build_fee_audit_rows,
        check_dividend_or_account_fee_exposure,
        correlate_fees_with,
        fee_bearing_rows,
        group_fees_by_instrument,
        group_fees_by_leverage,
        reconcile_account_totals,
    )

    config = load_config(host=get_host())
    _p(buf, f"E1+: config: {config.describe()}")
    if config.environment.value == "real":
        _p(buf, "E1+: this config points at the REAL account -- every call below is read-only regardless.")

    client = EtoroClient(config.etoro, role="read")
    try:
        symbol_by_id: dict[int, str] = {}
        for symbol in ("SCHG", "UPRO"):
            try:
                iid = client.resolve_instrument_id(symbol)
                symbol_by_id[iid] = symbol
            except Exception as e:
                _p(buf, f"E1+: could not resolve {symbol} (cosmetic only, audit still runs on instrument_id): {e!r}")

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trades = client.get_trade_history(start_date="1970-01-01", end_date=today, page_size=500)
        _p(buf, f"E1+: fetched {len(trades)} closed-trade rows")
        trade_dicts = [dataclasses.asdict(t) for t in trades]
        _save_json(output_dir, "e1plus_trade_history_raw", trade_dicts)

        rows = build_fee_audit_rows(trade_dicts, symbol_by_instrument_id=symbol_by_id)
        _save_json(output_dir, "e1plus_per_trade_table", [dataclasses.asdict(r) for r in rows])
        _p(buf, f"E1+: per-trade table saved, {len(rows)} rows")

        by_instrument = group_fees_by_instrument(rows)
        _save_json(output_dir, "e1plus_by_instrument", [dataclasses.asdict(g) for g in by_instrument])
        for g in by_instrument:
            label = g.symbol or f"instrument_id={g.instrument_id}"
            _p(
                buf,
                f"E1+: BY INSTRUMENT {label}: n={g.count} sum(fees)=${g.sum_fees:,.2f} "
                f"sum(investment)=${g.sum_investment:,.2f} any_nonzero_fee={g.any_nonzero_fee}",
            )
            if label == "SCHG" and g.any_nonzero_fee:
                _p(buf, "E1+: !!! a plain-ETF (SCHG) trade carried a nonzero fee -- see e1plus_fee_bearing_rows_full !!!")

        by_leverage = group_fees_by_leverage(rows)
        _save_json(output_dir, "e1plus_by_leverage", {k: dataclasses.asdict(v) for k, v in by_leverage.items()})
        for key, g in by_leverage.items():
            _p(
                buf,
                f"E1+: BY LEVERAGE/SHORT {key}: n={g.count} sum(fees)=${g.sum_fees:,.2f} "
                f"sum(investment)=${g.sum_investment:,.2f} any_nonzero_fee={g.any_nonzero_fee}",
            )

        bearing = fee_bearing_rows(rows)
        _save_json(output_dir, "e1plus_fee_bearing_rows_full", [dataclasses.asdict(r) for r in bearing])
        _p(
            buf,
            f"E1+: {len(bearing)} of {len(rows)} rows carry a nonzero fee "
            "-- full list below and in e1plus_fee_bearing_rows_full:",
        )
        for r in bearing:
            _p(
                buf,
                f"E1+:   FEE ROW pos={r.position_id} instrument={r.symbol or r.instrument_id} "
                f"open={r.open_date} close={r.close_date} investment={r.investment} "
                f"fees={r.fees} leveraged_or_short={r.is_leveraged_or_short} holding_days={r.holding_days}",
            )

        corr_duration = correlate_fees_with(rows, "holding_days")
        corr_investment = correlate_fees_with(rows, "investment")
        _save_json(
            output_dir,
            "e1plus_correlations",
            {"holding_days": dataclasses.asdict(corr_duration), "investment": dataclasses.asdict(corr_investment)},
        )
        _p(buf, f"E1+: correlation(fees, holding_days) = {corr_duration.pearson_r} ({corr_duration.note})")
        _p(buf, f"E1+: correlation(fees, investment)   = {corr_investment.pearson_r} ({corr_investment.note})")

        dividend_check = check_dividend_or_account_fee_exposure([t.get("raw", {}) for t in trade_dicts])
        _save_json(output_dir, "e1plus_dividend_account_field_check", dataclasses.asdict(dividend_check))
        _p(buf, f"E1+: dividend/account field exposure check: {dividend_check.verdict}")

        recon = reconcile_account_totals(rows)
        _save_json(output_dir, "e1plus_reconciliation", dataclasses.asdict(recon))
        _p(buf, f"E1+: RECONCILIATION: {recon.verdict}")
    finally:
        client.close()
