"""§4.7 adoption-migration DRY-RUN runner (doc/DESIGN_market_data.md §4.3,
§4.4, §4.7, §10.9, §10.10; ``nightwatch.market.adopt``).

**Split by design, matching ``adopt.py``'s own "no network I/O" rule and the
project's one real, hard constraint (board nightwatch/handoff-2026-09-04-market-data.md
§2.8): the dev PC cannot reach eToro at all (Kominfo DNS sinkhole), but IT
CAN reach Tiingo.** So this file is deliberately two independently-testable
halves:

* :func:`load_positions_from_raw` / :func:`load_instruments_from_meta` --
  pure, offline, no network -- turn the JSON an on-device fetch already
  produced (``android-probe/app/src/main/python/probe/adopt_fetch.py``,
  pulled back via ``adb pull``) into real ``Position``/``Instrument``
  objects, using the exact same parser the live client itself uses
  (``EtoroClient._parse_position``, a ``@staticmethod`` with no network or
  DB access).
* :func:`fetch_spy_history` -- the one function in this file that touches
  the network, and it only ever talks to Tiingo, reachable from this
  machine.
* :func:`verify_open_dates_covered` -- the correctness check this task's
  brief cares about most: does every position's entry date resolve to a
  REAL SPY bar, or would ``build_adoption_plan`` mark it unresolved?
  ``nightwatch.market.adopt`` resolves a non-trading open date to the
  preceding NYSE trading day (T068), requiring that exact bar. A missing
  trading-date bar is treated exactly like "predates the window": unresolved, and
  ``run_adoption_backfill`` halts the ENTIRE migration (writes nothing) if
  even one position is unresolved. This function surfaces that risk BEFORE
  the dry run halts opaquely, by classifying every date as
  covered / not-a-trading-day / before-window.
* :func:`run_dry_run` -- opens a fresh temp (or copied-live) sqlite
  database, upserts the instrument rows the on-device fetch resolved, and
  calls ``nightwatch.market.adopt.run_adoption_backfill(..., dry_run=True)``
  -- ``dry_run`` is never anything but ``True`` anywhere in this file.
* :func:`main` -- CLI glue: reads the pulled JSON, fetches SPY, builds/opens
  the db, runs the dry run, prints ``AdoptionPlan.report()`` plus the extra
  numbers the task brief asks for.

**Never writes ``lot_annotations``.** ``dry_run=True`` is hardcoded in the
one call site (:func:`run_dry_run`) and never parameterised -- there is no
flag or environment variable in this file that can flip it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NIGHTWATCH_SRC = _REPO_ROOT / "app" / "src" / "main" / "python"
if str(_NIGHTWATCH_SRC) not in sys.path:
    sys.path.insert(0, str(_NIGHTWATCH_SRC))

from nightwatch.config import load_config  # noqa: E402
from nightwatch.db.repo import Instrument, Repo  # noqa: E402
from nightwatch.etoro.client import EtoroClient  # noqa: E402
from nightwatch.etoro.models import Position  # noqa: E402
from nightwatch.host import DesktopHost  # noqa: E402
from nightwatch.market.adopt import (  # noqa: E402
    AdoptionError,
    AdoptionPlan,
    build_spy_context_index,
    _entry_trading_date,
    run_adoption_backfill,
)
from nightwatch.market.source import EodBar, TiingoClient  # noqa: E402
from nightwatch.market.window import FULL_HISTORY_START_DATE  # noqa: E402

__all__ = [
    "OpenDateCoverage",
    "load_positions_from_raw",
    "load_instruments_from_meta",
    "fetch_spy_history",
    "verify_open_dates_covered",
    "run_dry_run",
    "main",
]


# ---------------------------------------------------------------------------
# Offline half: parse the on-device fetch's JSON. No network, no DB.
# ---------------------------------------------------------------------------


def load_positions_from_raw(raw_positions: Sequence[Mapping[str, Any]]) -> list[Position]:
    """Reconstruct ``Position`` objects from ``adopt_portfolio_raw.json``
    (a list of ``dict(p.raw)`` saved on-device by ``probe.adopt_fetch``).

    Uses the real ``EtoroClient._parse_position`` static method -- the exact
    parser the live client applies to a freshly-fetched portfolio -- so this
    is not a second, possibly-drifted reimplementation of that parsing
    logic; it is a static method and touches neither network nor DB.
    """
    return [EtoroClient._parse_position(raw) for raw in raw_positions]


def load_instruments_from_meta(
    meta_rows: Sequence[Mapping[str, Any]],
    *,
    managed_instrument_ids: Mapping[str, int] | None = None,
) -> list[Instrument]:
    """Build ``Instrument`` rows from ``adopt_instruments_meta_raw.json``
    (``EtoroClient.get_instruments()``'s ``instrumentDisplayDatas`` rows,
    saved on-device).

    The confirmed live field for the ticker is ``symbolFull`` (verified
    against a real response, 2026-09-05 -- see this task's own report for
    the raw example). ``managed_instrument_ids`` (``adopt_managed_instrument_
    ids.json``, from ``EtoroClient.resolve_instrument_id``) is consulted
    first and wins on a conflict, since it comes from the authoritative
    symbol->id search rather than a display-metadata field whose exact
    contract is less pinned down elsewhere in this codebase.
    """
    by_id: dict[int, str] = {}
    for row in meta_rows:
        iid = row.get("instrumentID", row.get("instrumentId"))
        symbol = row.get("symbolFull") or row.get("internalSymbolFull")
        if iid is None:
            continue
        if symbol:
            by_id[int(iid)] = str(symbol)
        else:
            # Best-effort placeholder -- still "resolved" (a real, if
            # synthetic, symbol string), never silently dropped. A position
            # against an id that lands here is correctly EXCLUDED (not
            # unresolved) as long as the placeholder is not in
            # managed_symbols, which it never is.
            by_id.setdefault(int(iid), f"UNKNOWN_INSTRUMENT_{iid}")

    if managed_instrument_ids:
        for symbol, iid in managed_instrument_ids.items():
            by_id[int(iid)] = symbol

    return [Instrument(symbol=symbol, instrument_id=iid) for iid, symbol in sorted(by_id.items())]


# ---------------------------------------------------------------------------
# Network half: Tiingo only (reachable from this machine; eToro is not).
# ---------------------------------------------------------------------------


def fetch_spy_history(
    *,
    config_dir: Path | None = None,
    start_date: str = FULL_HISTORY_START_DATE,
    end_date: str | None = None,
) -> list[EodBar]:
    """``TiingoClient.get_eod_history('SPY', ...)`` -- the ONLY network call
    in this file. Reachable from the dev PC (confirmed live 2026-09-05,
    unlike eToro, board nightwatch/handoff-2026-09-04-market-data.md §2.8).
    """
    host = DesktopHost(root=config_dir) if config_dir else DesktopHost()
    config = load_config(host=host)
    client = TiingoClient(config.tiingo)
    try:
        return client.get_eod_history("SPY", start_date=start_date, end_date=end_date)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# The correctness question this task cares about most: does every position's
# entry date resolve to a real SPY bar?
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenDateCoverage:
    """Per-position classification of whether its entry date has a real
    Tiingo SPY bar -- computed independently of ``build_adoption_plan``
    (which only ever sees "covered" vs "not"), so a dry-run report can show
    *which* positions would be unresolved and *why*, not just a count.
    """

    position_id: str
    open_date: str | None
    trading_date: str | None
    covered: bool
    reason: str


def verify_open_dates_covered(
    positions: Sequence[Position], spy_bars: Sequence[EodBar]
) -> list[OpenDateCoverage]:
    """Classify every position's entry date against the fetched SPY series.

    Uses adoption's shared date resolution: weekends and holidays roll to
    the preceding NYSE trading day, whose exact SPY bar is required. A
    missing trading-day bar or a date outside history remains unresolved;
    there is no nearest-available-bar fallback. The report names any date
    resolution without changing the raw broker open date.
    """
    index = build_spy_context_index(spy_bars)
    window_start = min(index) if index else None
    results: list[OpenDateCoverage] = []
    for position in positions:
        if not position.open_date:
            results.append(
                OpenDateCoverage(
                    position_id=position.position_id,
                    open_date=None,
                    trading_date=None,
                    covered=False,
                    reason="no openDateTime on this position at all",
                )
            )
            continue
        trading_date = _entry_trading_date(position.open_date)
        resolution = (
            f"open date {position.open_date[:10]} -> preceding NYSE trading date {trading_date}; "
            if trading_date != position.open_date[:10] else ""
        )
        if trading_date in index:
            results.append(
                OpenDateCoverage(
                    position_id=position.position_id,
                    open_date=position.open_date,
                    trading_date=trading_date,
                    covered=True,
                    reason=resolution + "exact SPY bar found for this trading date",
                )
            )
        elif window_start is not None and trading_date < window_start:
            results.append(
                OpenDateCoverage(
                    position_id=position.position_id,
                    open_date=position.open_date,
                    trading_date=trading_date,
                    covered=False,
                    reason=resolution + f"predates the fetched window (earliest bar {window_start})",
                )
            )
        else:
            results.append(
                OpenDateCoverage(
                    position_id=position.position_id,
                    open_date=position.open_date,
                    trading_date=trading_date,
                    covered=False,
                    reason=resolution + (
                        "no SPY bar for this exact trading date; no fallback to a "
                        "nearby available bar -- this position is UNRESOLVED and "
                        "halts the whole migration"
                    ),
                )
            )
    return results


# ---------------------------------------------------------------------------
# Resolution: either side, but run here against a fresh/copied db.
# ---------------------------------------------------------------------------


def run_dry_run(
    positions: Sequence[Position],
    instruments: Sequence[Instrument],
    spy_bars: Sequence[EodBar],
    *,
    db_path: Path,
) -> AdoptionPlan:
    """Open (and migrate) the sqlite db at ``db_path``, upsert
    ``instruments``, and run the §4.7 backfill as a dry run.

    ``dry_run=True`` is hardcoded here -- there is no parameter on this
    function that can change it. ``db_path`` is the caller's
    responsibility: pass a path to a COPY of a live db, or a path in a fresh
    temp directory -- this function does not decide which; see ``main``'s
    own docstring for how it decided, given no live db existed on the
    device tonight.
    """
    repo = Repo.open(db_path)
    try:
        for instrument in instruments:
            repo.upsert_instrument(instrument)
        return run_adoption_backfill(repo, positions, spy_bars, dry_run=True)
    finally:
        repo.conn.close()


# ---------------------------------------------------------------------------
# CLI glue
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch-dir",
        type=Path,
        required=True,
        help="Directory containing adopt_portfolio_raw.json / "
        "adopt_managed_instrument_ids.json / adopt_instruments_meta_raw.json "
        "(pulled from the device via adb).",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="Directory with config/config.toml + config/.secrets.env for the "
        "Tiingo fetch (defaults to this repo's own config/).",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to a sqlite db to open/migrate for the dry run. Defaults to "
        "a fresh file under a temp directory (no live db existed on the "
        "device as of this run -- see the task report).",
    )
    args = parser.parse_args(argv)

    portfolio_raw = json.loads((args.fetch_dir / "adopt_portfolio_raw.json").read_text(encoding="utf-8"))
    managed_ids_path = args.fetch_dir / "adopt_managed_instrument_ids.json"
    managed_ids = json.loads(managed_ids_path.read_text(encoding="utf-8")) if managed_ids_path.exists() else {}
    meta_path = args.fetch_dir / "adopt_instruments_meta_raw.json"
    meta_rows = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else []

    positions = load_positions_from_raw(portfolio_raw)
    instruments = load_instruments_from_meta(meta_rows, managed_instrument_ids=managed_ids)
    print(f"loaded {len(positions)} positions, {len(instruments)} instruments from {args.fetch_dir}")

    print(f"fetching SPY EOD history from {FULL_HISTORY_START_DATE} to present (Tiingo, reachable from this PC)...")
    spy_bars = fetch_spy_history(config_dir=args.config_dir)
    print(f"fetched {len(spy_bars)} SPY bars, {min(b.date for b in spy_bars)} .. {max(b.date for b in spy_bars)}")

    coverage = verify_open_dates_covered(positions, spy_bars)
    uncovered = [c for c in coverage if not c.covered]
    print(f"\nopen-date SPY coverage: {len(coverage) - len(uncovered)}/{len(coverage)} covered")
    for c in uncovered:
        print(f"  UNCOVERED position_id={c.position_id} open_date={c.open_date!r}: {c.reason}")

    if args.db_path is not None:
        db_path = args.db_path
        print(f"\nusing explicit db path: {db_path}")
    else:
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="nightwatch_adopt_dryrun_"))
        db_path = tmp_dir / "nightwatch.db"
        print(f"\nno --db-path given: using a FRESH temp db at {db_path}")

    try:
        plan = run_dry_run(positions, instruments, spy_bars, db_path=db_path)
    except AdoptionError as e:
        print("\nAdoptionError raised -- migration would halt, writing NOTHING:")
        print(str(e))
        return 1

    print("\n" + plan.report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
