"""Offline tests for ``tools/adopt_backfill_runner.py`` -- the §4.7
adoption-migration dry-run runner (DESIGN_market_data.md §4.7, §10.9,
§10.10).

Entirely offline: no eToro call, no Tiingo call, no device. Exercises the
parsing/classification halves against data shaped exactly like what a real
on-device run produced (see this task's own report for the field names --
``symbolFull``/``instrumentID`` confirmed against a real
``get_instruments`` response, 2026-09-05), plus ``run_dry_run`` against a
fresh temp sqlite db (never a live one -- this suite never touches
``config/`` or any device path).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from adopt_backfill_runner import (  # noqa: E402
    load_instruments_from_meta,
    load_positions_from_raw,
    run_dry_run,
    verify_open_dates_covered,
)

from nightwatch.market.adopt import AdoptionError  # noqa: E402
from nightwatch.market.source import EodBar  # noqa: E402


def _raw_position(
    position_id: int,
    instrument_id: int,
    *,
    units: float,
    open_rate: float,
    amount: float,
    open_date: str,
) -> dict:
    """Shaped exactly like a real ``dict(p.raw)`` from the on-device probe
    (see ``adopt_portfolio_raw.json``'s real example in this task's report)
    -- ``positionID``/``instrumentID``/``units``/``openRate``/``amount``
    are the fields ``EtoroClient._parse_position`` requires.
    """
    return {
        "positionID": position_id,
        "instrumentID": instrument_id,
        "openDateTime": open_date,
        "openRate": open_rate,
        "units": units,
        "amount": amount,
        "isBuy": True,
        "leverage": 1,
    }


SCHG_ID = 1084
UPRO_ID = 3244
AAPL_ID = 200001


def _spy_bar(date: str, close: float) -> EodBar:
    return EodBar(date=date, close=close, adj_close=close, split_factor=1.0, div_cash=0.0)


SPY_BARS = [
    _spy_bar("2024-01-02", 100.0),
    _spy_bar("2024-01-03", 101.0),
    _spy_bar("2024-01-04", 99.0),
    _spy_bar("2024-01-05", 102.0),
]


# -- load_positions_from_raw ---------------------------------------------------


def test_load_positions_from_raw_parses_real_shaped_payload() -> None:
    raw = [
        _raw_position(1, SCHG_ID, units=7.233273, open_rate=27.65, amount=200.0, open_date="2024-01-02T14:30:41.173Z"),
        _raw_position(2, UPRO_ID, units=1.0, open_rate=50.0, amount=50.0, open_date="2024-01-03T10:00:00.000Z"),
    ]
    positions = load_positions_from_raw(raw)
    assert len(positions) == 2
    assert positions[0].position_id == "1"
    assert positions[0].instrument_id == SCHG_ID
    assert positions[0].amount == 200.0
    assert positions[1].instrument_id == UPRO_ID


def test_load_positions_from_raw_raises_on_missing_required_field() -> None:
    from nightwatch.etoro.errors import EtoroValidationError

    raw = [{"positionID": 1, "instrumentID": SCHG_ID, "openDateTime": "2024-01-02T00:00:00Z"}]
    with pytest.raises(EtoroValidationError):
        load_positions_from_raw(raw)


# -- load_instruments_from_meta -------------------------------------------------


def test_load_instruments_from_meta_uses_symbol_full_field() -> None:
    """``symbolFull`` is the field confirmed against a real
    ``get_instruments`` response (this task's report); ``instrumentID``
    (capital ID) is the id field on that same payload.
    """
    meta_rows = [
        {"instrumentID": SCHG_ID, "symbolFull": "SCHG"},
        {"instrumentID": UPRO_ID, "symbolFull": "UPRO"},
        {"instrumentID": AAPL_ID, "symbolFull": "AAPL"},
    ]
    instruments = load_instruments_from_meta(meta_rows)
    by_id = {i.instrument_id: i.symbol for i in instruments}
    assert by_id == {SCHG_ID: "SCHG", UPRO_ID: "UPRO", AAPL_ID: "AAPL"}


def test_load_instruments_from_meta_falls_back_to_placeholder_when_symbol_missing() -> None:
    meta_rows = [{"instrumentID": 999999}]
    instruments = load_instruments_from_meta(meta_rows)
    assert instruments[0].symbol == "UNKNOWN_INSTRUMENT_999999"
    assert instruments[0].instrument_id == 999999


def test_load_instruments_from_meta_managed_ids_win_over_meta_row() -> None:
    """``adopt_managed_instrument_ids.json`` (from ``resolve_instrument_id``,
    the authoritative symbol->id search) must win a conflict with a display
    -metadata row for the same id.
    """
    meta_rows = [{"instrumentID": SCHG_ID, "symbolFull": "WRONG"}]
    instruments = load_instruments_from_meta(meta_rows, managed_instrument_ids={"SCHG": SCHG_ID})
    assert instruments[0].symbol == "SCHG"


# -- verify_open_dates_covered --------------------------------------------------


def test_verify_open_dates_covered_all_covered() -> None:
    positions = [
        EtoroPositionStub("1", "2024-01-02T10:00:00Z"),
        EtoroPositionStub("2", "2024-01-03T10:00:00Z"),
    ]
    results = verify_open_dates_covered(positions, SPY_BARS)
    assert all(r.covered for r in results)


def test_verify_open_dates_covered_flags_non_trading_day_with_no_fallback() -> None:
    """The central finding this task asked to verify: adopt.py has NO
    fallback to a nearby bar for a date the SPY series has no exact bar
    for. 2024-01-06 is a Saturday, absent from ``SPY_BARS`` (which only has
    weekday bars 01-02..01-05) but well after ``window_start`` -- this must
    be reported UNCOVERED, not silently rounded to the nearest prior bar.
    """
    positions = [EtoroPositionStub("weekend-open", "2024-01-06T10:00:00Z")]
    results = verify_open_dates_covered(positions, SPY_BARS)
    assert len(results) == 1
    assert results[0].covered is False
    assert "non-trading day" in results[0].reason
    assert "NO fallback" in results[0].reason  # reason names the real risk explicitly


def test_verify_open_dates_covered_flags_before_window() -> None:
    positions = [EtoroPositionStub("pre-window", "2023-12-01T10:00:00Z")]
    results = verify_open_dates_covered(positions, SPY_BARS)
    assert results[0].covered is False
    assert "predates the fetched window" in results[0].reason


def test_verify_open_dates_covered_flags_missing_open_date() -> None:
    positions = [EtoroPositionStub("no-date", None)]
    results = verify_open_dates_covered(positions, SPY_BARS)
    assert results[0].covered is False
    assert "no openDateTime" in results[0].reason


class EtoroPositionStub:
    """Minimal stand-in exposing only what ``verify_open_dates_covered``
    reads (``position_id``/``open_date``) -- avoids depending on every
    other required field ``Position`` demands for a test that is purely
    about date classification.
    """

    def __init__(self, position_id: str, open_date: str | None) -> None:
        self.position_id = position_id
        self.open_date = open_date


# -- run_dry_run -----------------------------------------------------------


def test_run_dry_run_against_fresh_temp_db_never_writes(tmp_path: Path) -> None:
    """Mirrors the real dry run: a fresh temp db (never a live one), and
    ``dry_run=True`` throughout -- confirms zero rows land in
    ``lot_annotations`` even though the plan resolves cleanly.
    """
    raw = [
        _raw_position(1, SCHG_ID, units=0.09, open_rate=111.1, amount=10.0, open_date="2024-01-02T10:00:00Z"),
        _raw_position(2, UPRO_ID, units=1.0, open_rate=50.0, amount=50.0, open_date="2024-01-03T10:00:00Z"),
        _raw_position(3, AAPL_ID, units=1.0, open_rate=180.0, amount=180.0, open_date="2024-01-03T10:00:00Z"),
    ]
    positions = load_positions_from_raw(raw)
    instruments = load_instruments_from_meta(
        [
            {"instrumentID": SCHG_ID, "symbolFull": "SCHG"},
            {"instrumentID": UPRO_ID, "symbolFull": "UPRO"},
            {"instrumentID": AAPL_ID, "symbolFull": "AAPL"},
        ]
    )
    db_path = tmp_path / "nightwatch.db"
    plan = run_dry_run(positions, instruments, SPY_BARS, db_path=db_path)

    assert len(plan.to_adopt) == 2  # SCHG + UPRO
    assert len(plan.excluded) == 1  # AAPL
    assert not plan.unresolved

    # The hard safety check: dry_run=True means nothing landed in the db.
    from nightwatch.db.repo import Repo

    repo = Repo.open(db_path)
    try:
        assert repo.list_lot_annotations() == []
    finally:
        repo.conn.close()


def test_run_dry_run_raises_and_writes_nothing_when_unresolved(tmp_path: Path) -> None:
    """A position whose open date the SPY window does not cover halts the
    ENTIRE migration (AdoptionError), even in a dry run -- and even the
    positions that WOULD have resolved cleanly must not be written.
    """
    raw = [
        _raw_position(1, SCHG_ID, units=0.09, open_rate=111.1, amount=10.0, open_date="2024-01-02T10:00:00Z"),
        # Saturday -- no SPY bar, no fallback.
        _raw_position(2, SCHG_ID, units=0.1, open_rate=100.0, amount=10.0, open_date="2024-01-06T10:00:00Z"),
    ]
    positions = load_positions_from_raw(raw)
    instruments = load_instruments_from_meta([{"instrumentID": SCHG_ID, "symbolFull": "SCHG"}])
    db_path = tmp_path / "nightwatch.db"

    with pytest.raises(AdoptionError):
        run_dry_run(positions, instruments, SPY_BARS, db_path=db_path)

    from nightwatch.db.repo import Repo

    repo = Repo.open(db_path)
    try:
        assert repo.list_lot_annotations() == []
    finally:
        repo.conn.close()
