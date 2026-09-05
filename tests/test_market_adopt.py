"""Tests for the §4.7 adoption backfill (DESIGN_market_data.md §4.4, §4.7,
§10.9, §10.10; nightwatch.market.adopt).

Entirely offline -- no eToro call (the dev PC cannot reach eToro at all,
HANDOFF_2026-09-04_market_data.md §2.8) and no Tiingo call (a fixed,
synthetic SPY series is used instead of a live fetch, so this suite is not
market-hours-dependent). ``positions`` fixtures are built directly as
``etoro.models.Position`` objects -- i.e. as if ``_parse_position`` had
already run -- since this module's job starts after parsing, not at it.

The fixture below is deliberately populated, not empty: it carries the three
real pre-split SCHG rows from DESIGN_market_data.md §4.6 (verified against a
live account, 2024-09-03/2024-09-06, pre the 2024-10-11 4:1 split), several
more synthetic SCHG positions spanning different SPY drawdown tiers, one
UPRO position, and one position in an instrument this app does not manage --
the "a test that passes because nothing happened is not a pass" rule this
project's own history keeps citing (HANDOFF.md §7 rule 1, this task's
brief) means a fixture with zero positions or one that never exercises the
exclusion path would prove nothing.
"""

from __future__ import annotations

import pytest

from nightwatch.db.repo import Instrument, Repo
from nightwatch.etoro.models import Position
from nightwatch.market.adopt import (
    AdoptionError,
    DEFAULT_BOOK,
    DEFAULT_ORIGIN,
    build_adoption_plan,
    build_spy_context_index,
    run_adoption_backfill,
)
from nightwatch.market.source import EodBar

SCHG_ID = 100001
UPRO_ID = 100002
AAPL_ID = 200001  # unrelated instrument -- must never be touched


def _spy_bar(date: str, adj_close: float) -> EodBar:
    return EodBar(date=date, close=adj_close, adj_close=adj_close, split_factor=1.0, div_cash=0.0)


#: A small, hand-computed SPY series (§2.7: adjClose is what §3.1 derives
#: ath/drawdown/tier from). Values are synthetic, not real SPY prices --
#: chosen so each date below produces a specific, hand-checked tier:
#:
#:   2024-01-02  100.0  -> new ATH 100.0,  dd=0.0                  tier 0
#:   2024-03-01  110.0  -> new ATH 110.0,  dd=0.0                  tier 0
#:   2024-06-03   90.0  -> ATH stays 110,  dd=-0.1818...           tier 2
#:   2024-09-03  104.5  -> ATH stays 110,  dd=-0.05                tier 0
#:   2024-09-06  103.95 -> ATH stays 110,  dd=-0.055               tier 0
#:   2024-12-02   80.0  -> ATH stays 110,  dd=-0.2727...           tier 4
#:   2025-02-10   60.0  -> ATH stays 110,  dd=-0.4545...           tier 5
SPY_BARS = [
    _spy_bar("2024-01-02", 100.0),
    _spy_bar("2024-03-01", 110.0),
    _spy_bar("2024-06-03", 90.0),
    _spy_bar("2024-09-03", 104.5),
    _spy_bar("2024-09-06", 103.95),
    _spy_bar("2024-12-02", 80.0),
    _spy_bar("2025-02-10", 60.0),
]


def _position(
    position_id: str,
    instrument_id: int,
    *,
    units: float,
    entry_price: float,
    amount: float,
    open_date: str,
    is_buy: bool = True,
    leverage: int = 1,
) -> Position:
    return Position(
        position_id=position_id,
        instrument_id=instrument_id,
        units=units,
        entry_price=entry_price,
        amount=amount,
        leverage=leverage,
        is_buy=is_buy,
        open_date=open_date,
        raw={},
    )


def _populated_positions() -> list[Position]:
    return [
        # The three real pre-split SCHG rows (DESIGN_market_data.md §4.6,
        # verified live 2024-09-04): amount == units * openRate == 10.0,
        # both units and openRate already post-split-rescaled by eToro.
        _position(
            "SCHG-PRESPLIT-1", SCHG_ID, units=0.3964, entry_price=25.2275,
            amount=10.0, open_date="2024-09-03T10:15:00+00:00",
        ),
        _position(
            "SCHG-PRESPLIT-2", SCHG_ID, units=0.4000, entry_price=24.9975,
            amount=10.0, open_date="2024-09-03T14:00:00+00:00",
        ),
        _position(
            "SCHG-PRESPLIT-3", SCHG_ID, units=0.4082, entry_price=24.4975,
            amount=10.0, open_date="2024-09-06T09:31:00+00:00",
        ),
        # More SCHG positions spanning other tiers -- a realistic DCA stack
        # opens on many different dates, not just the split window.
        _position(
            "SCHG-4", SCHG_ID, units=0.09, entry_price=111.1,
            amount=10.0, open_date="2024-01-02T10:00:00+00:00",
        ),  # tier 0, ATH day itself
        _position(
            "SCHG-5", SCHG_ID, units=0.11, entry_price=90.9,
            amount=10.0, open_date="2024-06-03T10:00:00+00:00",
        ),  # tier 2
        _position(
            "SCHG-6", SCHG_ID, units=0.12, entry_price=125.0,
            amount=15.0, open_date="2024-12-02T10:00:00+00:00",
        ),  # tier 4
        _position(
            "SCHG-7", SCHG_ID, units=0.2, entry_price=100.0,
            amount=20.0, open_date="2025-02-10T10:00:00+00:00",
        ),  # tier 5
        # UPRO -- the other managed symbol.
        _position(
            "UPRO-1", UPRO_ID, units=1.0, entry_price=50.0,
            amount=50.0, open_date="2024-03-01T10:00:00+00:00",
        ),  # tier 0
        # Unrelated instrument -- must be excluded, never adopted.
        _position(
            "AAPL-1", AAPL_ID, units=1.0, entry_price=180.0,
            amount=180.0, open_date="2024-06-03T10:00:00+00:00",
        ),
    ]


def _seed_instruments(repo: Repo) -> None:
    repo.upsert_instrument(Instrument(symbol="SCHG", instrument_id=SCHG_ID))
    repo.upsert_instrument(Instrument(symbol="UPRO", instrument_id=UPRO_ID))
    repo.upsert_instrument(Instrument(symbol="AAPL", instrument_id=AAPL_ID))


# -- build_spy_context_index --------------------------------------------------


def test_spy_context_index_ath_and_drawdown() -> None:
    index = build_spy_context_index(SPY_BARS)

    assert index["2024-01-02"].ath == 100.0
    assert index["2024-01-02"].drawdown == 0.0
    assert index["2024-01-02"].tier == 0

    assert index["2024-03-01"].ath == 110.0
    assert index["2024-03-01"].drawdown == 0.0

    ctx = index["2024-06-03"]
    assert ctx.ath == 110.0
    assert ctx.close == 90.0
    assert ctx.drawdown == pytest.approx((90.0 - 110.0) / 110.0)
    assert ctx.tier == 2

    ctx = index["2024-12-02"]
    assert ctx.ath == 110.0
    assert ctx.drawdown == pytest.approx((80.0 - 110.0) / 110.0)
    assert ctx.tier == 4

    ctx = index["2025-02-10"]
    assert ctx.drawdown == pytest.approx((60.0 - 110.0) / 110.0)
    assert ctx.tier == 5

    # ATH never regresses even after a lower close.
    assert index["2024-09-03"].ath == 110.0
    assert index["2024-09-06"].ath == 110.0


def test_spy_context_index_unsorted_input_still_correct() -> None:
    shuffled = list(reversed(SPY_BARS))
    index = build_spy_context_index(shuffled)
    assert index["2024-12-02"].ath == 110.0


def test_spy_context_index_uses_raw_close_not_adj_close() -> None:
    """HANDOFF_2026-09-05_evening.md §4: ATH/drawdown/tier are LEVELS and
    must be derived from raw ``close``, never ``adjClose`` -- unlike this
    module's earlier docstring, which (before this fix) claimed adjClose."""
    bars = [
        EodBar(date="2024-01-02", close=100.0, adj_close=80.0, split_factor=1.0, div_cash=0.0),
    ]
    index = build_spy_context_index(bars)
    assert index["2024-01-02"].ath == 100.0  # raw close, not the lower adjClose
    assert index["2024-01-02"].close == 100.0
    assert index["2024-01-02"].drawdown == 0.0


# -- build_adoption_plan / run_adoption_backfill: the happy path -------------


def test_dry_run_plans_every_managed_position_and_excludes_unmanaged(repo: Repo) -> None:
    _seed_instruments(repo)
    positions = _populated_positions()

    plan = run_adoption_backfill(repo, positions, SPY_BARS, dry_run=True)

    assert not plan.unresolved
    # 7 SCHG + 1 UPRO = 8 managed positions; AAPL excluded.
    assert len(plan.to_adopt) == 8
    assert plan.per_symbol_counts == {"SCHG": 7, "UPRO": 1}
    assert len(plan.excluded) == 1
    assert plan.excluded[0].position_id == "AAPL-1"
    assert plan.excluded[0].symbol == "AAPL"

    # Tier distribution: SCHG-PRESPLIT-1/2/3 (tier 0), SCHG-4 (tier 0),
    # SCHG-5 (tier 2), SCHG-6 (tier 4), SCHG-7 (tier 5), UPRO-1 (tier 0).
    assert plan.tier_distribution == {0: 5, 2: 1, 4: 1, 5: 1}

    # Dry run must not write anything.
    assert repo.list_lot_annotations() == []

    report = plan.report()
    assert "to adopt          : 8" in report
    assert "SCHG  : 7" in report
    assert "UPRO  : 1" in report
    assert "excluded (unmanaged instrument): 1" in report
    assert "unresolved        : 0" in report


def test_apply_writes_exactly_the_planned_rows_with_correct_fields(repo: Repo) -> None:
    _seed_instruments(repo)
    positions = _populated_positions()

    plan = run_adoption_backfill(repo, positions, SPY_BARS, dry_run=False)
    assert len(plan.to_adopt) == 8

    rows = repo.list_lot_annotations()
    assert len(rows) == 8
    assert {r.position_id for r in rows} == {
        "SCHG-PRESPLIT-1", "SCHG-PRESPLIT-2", "SCHG-PRESPLIT-3",
        "SCHG-4", "SCHG-5", "SCHG-6", "SCHG-7", "UPRO-1",
    }

    row = repo.get_lot_annotation("SCHG-PRESPLIT-1")
    assert row is not None
    assert row.book == DEFAULT_BOOK == "catapult"
    assert row.origin == DEFAULT_ORIGIN == "adopted"
    assert row.bucket_id is None
    assert row.is_open is True
    # Re-link key: entry_date is the RAW openDateTime, not the trading-day
    # slice used for the SPY lookup -- §4.6's re-link key is
    # (instrument_id, openDateTime, amount), all three exactly as the
    # broker gave them.
    assert row.entry_date == "2024-09-03T10:15:00+00:00"
    assert row.entry_amount == 10.0
    # Derived SPY context for 2024-09-03: ATH 110.0, close 104.5, dd -0.05,
    # tier 0 (see SPY_BARS docstring above).
    assert row.entry_spy_ath == 110.0
    assert row.entry_spy_close == 104.5
    assert row.entry_spy_drawdown == pytest.approx(-0.05)
    assert row.entry_tier == 0
    # migration 005, HANDOFF_2026-09-05_evening.md §3a: every position this
    # module resolves has MEASURED context -- an out-of-window position
    # halts the whole run instead (see the predates-window tests below).
    assert row.entry_context_source == "derived"

    # AAPL must never get a row.
    assert repo.get_lot_annotation("AAPL-1") is None


# -- idempotency / re-runnability (§10.10: "a straggler fill may need a re-run") --


def test_backfill_is_idempotent_on_rerun(repo: Repo) -> None:
    _seed_instruments(repo)
    positions = _populated_positions()

    first = run_adoption_backfill(repo, positions, SPY_BARS, dry_run=False)
    assert len(first.to_adopt) == 8
    first_rows = {r.position_id: r for r in repo.list_lot_annotations()}

    # Re-run against the exact same portfolio: nothing new to adopt, no
    # duplicate rows, and the existing rows are untouched (not merely
    # equal -- literally not rewritten; created_at proves it).
    second = run_adoption_backfill(repo, positions, SPY_BARS, dry_run=False)
    assert second.to_adopt == []
    assert sorted(second.already_annotated) == sorted(p.position_id for p in positions if p.instrument_id in (SCHG_ID, UPRO_ID))

    second_rows = {r.position_id: r for r in repo.list_lot_annotations()}
    assert set(first_rows) == set(second_rows)
    for position_id, row in first_rows.items():
        assert second_rows[position_id].created_at == row.created_at


def test_backfill_handles_a_straggler_fill_landing_after_the_first_run(repo: Repo) -> None:
    """§10.10's exact scenario: the recurring DCA should be off before this
    runs, but if one more fill lands anyway, re-running the backfill (never
    a standing loop) must pick up only the straggler."""
    _seed_instruments(repo)
    positions = _populated_positions()

    run_adoption_backfill(repo, positions, SPY_BARS, dry_run=False)
    assert len(repo.list_lot_annotations()) == 8

    straggler = _position(
        "SCHG-STRAGGLER", SCHG_ID, units=0.05, entry_price=200.0,
        amount=10.0, open_date="2024-12-02T10:00:00+00:00",
    )
    plan = run_adoption_backfill(repo, [*positions, straggler], SPY_BARS, dry_run=False)

    assert [a.position_id for a in plan.to_adopt] == ["SCHG-STRAGGLER"]
    assert len(repo.list_lot_annotations()) == 9
    assert repo.get_lot_annotation("SCHG-STRAGGLER") is not None


# -- fail loudly and atomically -----------------------------------------------


def test_unknown_instrument_id_is_unresolved_and_halts_the_whole_run(repo: Repo) -> None:
    _seed_instruments(repo)
    positions = [
        *_populated_positions(),
        _position(
            "MYSTERY-1", 999999, units=1.0, entry_price=1.0, amount=1.0,
            open_date="2024-03-01T10:00:00+00:00",
        ),
    ]

    with pytest.raises(AdoptionError, match="999999"):
        run_adoption_backfill(repo, positions, SPY_BARS, dry_run=False)

    # Atomic: nothing from the otherwise-valid 8 positions was written either.
    assert repo.list_lot_annotations() == []


def test_unknown_instrument_id_is_unresolved_even_in_a_dry_run(repo: Repo) -> None:
    """A dry run must surface a fatal problem too (task brief item 5 says
    dry-run reports "anything it could not resolve"; it must not report a
    falsely clean plan when the real run would halt)."""
    _seed_instruments(repo)
    positions = [
        *_populated_positions(),
        _position(
            "MYSTERY-1", 999999, units=1.0, entry_price=1.0, amount=1.0,
            open_date="2024-03-01T10:00:00+00:00",
        ),
    ]

    with pytest.raises(AdoptionError):
        run_adoption_backfill(repo, positions, SPY_BARS, dry_run=True)

    assert repo.list_lot_annotations() == []


def test_spy_series_not_covering_open_date_halts(repo: Repo) -> None:
    _seed_instruments(repo)
    positions = [
        *_populated_positions(),
        _position(
            "SCHG-UNCOVERED", SCHG_ID, units=1.0, entry_price=1.0, amount=1.0,
            # Not one of the dates in SPY_BARS.
            open_date="2023-01-01T10:00:00+00:00",
        ),
    ]

    with pytest.raises(AdoptionError, match="SPY series does not cover"):
        run_adoption_backfill(repo, positions, SPY_BARS, dry_run=False)

    assert repo.list_lot_annotations() == []


def test_position_predating_the_fetched_window_halts_with_a_specific_reason(repo: Repo) -> None:
    """HANDOFF_2026-09-05_evening.md §3a Case 3: a position whose open_date
    predates the fetched SPY window entirely must HALT LOUDLY, name the
    offending position, and write NOTHING -- never silently assign a tier.
    This is the same underlying "SPY series does not cover" mechanism as
    the general-gap case above, but this position's open date is before
    SPY_BARS's very first bar (2024-01-02), so it gets the more specific
    "predates the fetched SPY window" reason, not merely a generic gap."""
    _seed_instruments(repo)
    positions = [
        *_populated_positions(),
        _position(
            "SCHG-PREDATES-WINDOW", SCHG_ID, units=1.0, entry_price=1.0,
            amount=1.0, open_date="2020-01-01T10:00:00+00:00",
        ),
    ]

    with pytest.raises(AdoptionError, match="PREDATES") as excinfo:
        run_adoption_backfill(repo, positions, SPY_BARS, dry_run=False)
    assert "SCHG-PREDATES-WINDOW" in str(excinfo.value)
    # Named per §3a's explicit disagreement: if an override is ever added,
    # the assumed tier must be 0, never 1.
    assert "tier 0" in str(excinfo.value) or "tier 1" in str(excinfo.value)

    # Atomic: nothing at all was written, including the otherwise-valid 8
    # positions.
    assert repo.list_lot_annotations() == []


def test_dry_run_also_surfaces_a_predates_window_halt(repo: Repo) -> None:
    """§3a's halt must be visible in a dry run too (task brief item 5: a
    dry run must not report a falsely clean plan when the real run would
    halt) -- mirrors the existing unknown-instrument dry-run test above."""
    _seed_instruments(repo)
    positions = [
        *_populated_positions(),
        _position(
            "SCHG-PREDATES-WINDOW", SCHG_ID, units=1.0, entry_price=1.0,
            amount=1.0, open_date="2020-01-01T10:00:00+00:00",
        ),
    ]

    with pytest.raises(AdoptionError, match="PREDATES"):
        run_adoption_backfill(repo, positions, SPY_BARS, dry_run=True)

    assert repo.list_lot_annotations() == []


def test_build_adoption_plan_gives_predates_window_positions_a_specific_reason() -> None:
    """Lower-level check on the pure function's reason string itself,
    independent of the atomic-write behaviour covered above."""
    instruments_by_id = {SCHG_ID: Instrument(symbol="SCHG", instrument_id=SCHG_ID)}
    spy_context = build_spy_context_index(SPY_BARS)  # earliest bar 2024-01-02

    positions = [
        _position(
            "SCHG-PREDATES", SCHG_ID, units=1.0, entry_price=1.0, amount=1.0,
            open_date="2020-01-01T10:00:00+00:00",
        ),
    ]
    plan = build_adoption_plan(
        positions,
        instruments_by_id=instruments_by_id,
        spy_context=spy_context,
        managed_symbols=("SCHG", "UPRO"),
    )

    assert len(plan.unresolved) == 1
    assert plan.unresolved[0].position_id == "SCHG-PREDATES"
    assert "PREDATES" in plan.unresolved[0].reason
    assert "2024-01-02" in plan.unresolved[0].reason  # names the window's earliest bar


def test_missing_open_date_is_unresolved(repo: Repo) -> None:
    _seed_instruments(repo)
    positions = [
        *_populated_positions(),
        _position(
            "SCHG-NO-DATE", SCHG_ID, units=1.0, entry_price=1.0, amount=1.0,
            open_date=None,  # type: ignore[arg-type]
        ),
    ]

    with pytest.raises(AdoptionError, match="openDateTime"):
        run_adoption_backfill(repo, positions, SPY_BARS, dry_run=False)

    assert repo.list_lot_annotations() == []


def test_build_adoption_plan_never_raises_reports_unresolved_instead() -> None:
    """The lower-level pure function is the one place that must never raise
    -- so a caller building its own report (e.g. a richer CLI) can see every
    problem at once rather than stopping at the first one."""
    instruments_by_id = {SCHG_ID: Instrument(symbol="SCHG", instrument_id=SCHG_ID)}
    spy_context = build_spy_context_index(SPY_BARS)
    positions = [
        _position(
            "UNKNOWN-1", 424242, units=1.0, entry_price=1.0, amount=1.0,
            open_date="2024-03-01T10:00:00+00:00",
        ),
        _position(
            "SCHG-OK", SCHG_ID, units=1.0, entry_price=1.0, amount=1.0,
            open_date="2024-03-01T10:00:00+00:00",
        ),
    ]

    plan = build_adoption_plan(
        positions,
        instruments_by_id=instruments_by_id,
        spy_context=spy_context,
        managed_symbols=("SCHG", "UPRO"),
    )

    assert len(plan.unresolved) == 1
    assert plan.unresolved[0].position_id == "UNKNOWN-1"
    assert len(plan.to_adopt) == 1
    assert plan.to_adopt[0].position_id == "SCHG-OK"
