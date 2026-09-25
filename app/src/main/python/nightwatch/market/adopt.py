"""The §4.7 adoption migration: backfilling ``lot_annotations`` for
pre-existing SCHG/UPRO positions (DESIGN_market_data.md §4.4, §4.7, §10.9,
§10.10).

**What this is.** 209 real positions (202 SCHG + 7 UPRO, per the live probe
in board nightwatch/handoff-2026-09-04-market-data.md §2, DESIGN_market_data.md §4.5, §4.7)
predate this app and have no ``lot_annotations`` row. Gate 11 invariant 1
(§4.5) halts every session until each one gets a row, so this migration is
mandatory, not optional, before the app can run at all.

**What this is NOT** (§10.10, resolved 2026-09-05): a recurring adopt loop,
an auto-adopt fallback, or a per-fill confirmation flow. The operator's
manual recurring SCHG DCA -- the source of the 202 SCHG positions (§10.9) --
is being switched off *before* this runs; gate 11 invariant 1 stays fully
armed afterwards, and an unannotated open position in a managed symbol
discovered after this migration is exactly what the invariant is supposed to
catch. Do not build a second automated writer here or anywhere else that
this module feeds -- §10.10 is explicit that one would disarm the invariant
this resolution exists to keep armed. The one concession to "one-shot" not
meaning "exactly once, ever" is idempotency (below): a straggler DCA fill
landing between the recurring buy stopping and this migration running is
handled by *re-running* this same function, not by a standing loop.

**Decisions this migration is built to, not free to re-litigate**
(board nightwatch/handoff-2026-09-04-market-data.md §2; DESIGN_market_data.md §4.7, §10.9):

* Adopt, never quarantine -- every open broker position in a managed symbol
  (SCHG, UPRO) gets a fully-managed sidecar row.
* ``book='catapult'`` for all of them (confirmed operator decision,
  2026-09-05).
* ``origin='adopted'`` -- provenance only, never an access gate.
* ``bucket_id`` stays ``NULL`` -- there is no originating sale.
* ``entry_spy_ath``/``entry_spy_close``/``entry_spy_drawdown``/``entry_tier``
  ARE re-derived from history here, unlike for an app-created position
  (§4.3's prohibition is about protecting an already-stamped provisional
  value from being silently overwritten by a different, "official-close"
  number at a discrete field; nothing was ever stamped for an adopted
  position, so there is nothing to protect -- §4.7 spells this out at
  length specifically so this derivation is not mistaken for the same
  mistake §4.3 forbids).

**Two failure classes, handled differently on purpose:**

* :class:`ExcludedPosition` -- a position in an instrument this app simply
  does not manage (the 18 positions across 5 other instruments, §4.5). This
  is not a failure. It is resolved (the symbol is known) and correctly not
  adopted.
* :class:`UnresolvedPosition` -- a position the backfill genuinely cannot
  make sense of: an ``instrumentID`` absent from the ``instruments`` table,
  an ``openDateTime`` the fetched SPY series does not cover (including a
  position that PREDATES the fetched window entirely --
  board nightwatch/handoff-2026-09-05-evening.md §3a Case 3, given its own, more specific
  reason string in :func:`build_adoption_plan`), or a missing required
  broker field. :func:`run_adoption_backfill` treats ANY unresolved
  position as reason to write nothing at all (task brief: "a partial
  backfill that half-satisfies gate 11 invariant 1 is worse than none").
  There is no override path here that assigns a tier to an out-of-window
  position -- HALT is the only behaviour this module implements for that
  case, by design (§3a: "a guessed tier that is indistinguishable from a
  measured one is this project's exact pathology"). If an override is ever
  added, §3a is explicit it must assign tier 0, not tier 1.

**Basis note (board nightwatch/handoff-2026-09-05-evening.md §4, decided 2026-09-05):**
``entry_spy_ath``/``entry_spy_close``/``entry_spy_drawdown``/``entry_tier``
are derived from Tiingo's raw ``close``, not ``adjClose`` -- they are
LEVELS, not returns; see :class:`SpyContext`'s docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..config import SymbolConfig
from ..db.repo import Instrument, LotAnnotation, Repo
from ..etoro.models import Position
from ..strategy.params import tier_for_drawdown
from .source import EodBar

__all__ = [
    "AdoptionError",
    "SpyContext",
    "ExcludedPosition",
    "UnresolvedPosition",
    "AdoptionPlan",
    "DEFAULT_BOOK",
    "DEFAULT_ORIGIN",
    "DEFAULT_ENTRY_CONTEXT_SOURCE",
    "build_spy_context_index",
    "build_adoption_plan",
    "run_adoption_backfill",
]

#: All 209 adopted positions default to catapult (confirmed operator
#: decision, DESIGN_market_data.md §4.7, 2026-09-05) -- nothing on the
#: broker or in history distinguishes which ladder a pre-existing position
#: belongs to, so this is an assignment, not a derivation.
DEFAULT_BOOK = "catapult"

#: Provenance, never an access gate (§4.4's origin column comment, §4.7).
DEFAULT_ORIGIN = "adopted"

#: Every row this module ever writes has measured, not guessed, SPY context
#: -- the only positions it resolves are ones whose openDateTime the fetched
#: SPY window actually covers (board nightwatch/handoff-2026-09-05-evening.md §3a: an
#: out-of-window position is UnresolvedPosition and halts the whole run, it
#: never reaches a write). ``'assumed_out_of_window'`` is the other value
#: ``lot_annotations.entry_context_source`` accepts (migration 005); it is
#: reserved for a possible future override of that halt and is never
#: produced by this module today.
DEFAULT_ENTRY_CONTEXT_SOURCE = "derived"


class AdoptionError(Exception):
    """Raised when the backfill cannot resolve every position it was asked
    to adopt, or when a write did not land the way the plan said it would.

    Task brief: "fail loudly and atomically on anything unresolvable ...
    prefer one transaction that either completes or rolls back." This is
    raised BEFORE :meth:`Repo.insert_lot_annotations` is ever called when
    resolution fails, so a failed adoption run touches the database not at
    all, not partially.
    """


@dataclass(frozen=True, slots=True)
class SpyContext:
    """SPY all-time-high / close / drawdown / tier as of one trading date.

    **Basis: raw ``close``, not ``adjClose``** (board nightwatch/handoff-2026-09-05-evening.md
    §4, decided 2026-09-05, correcting this docstring's earlier claim that
    ``adjClose`` drives ATH/drawdown/tier). ATH/drawdown/tier are LEVELS --
    compared against traded prices, and the entry_tier this produces is
    used exactly like a live-derived one (``market.window``'s
    ``spy_context_from_history``, same basis) -- never RETURNS, so the
    ``adjClose`` reasoning that correctly governs ``cone_5_z`` does not apply
    here. An adopted position's reconstructed SPY context is on the same
    footing as a live one's, not a different, ad hoc calculation -- and now
    on the same *basis* as one too.
    """

    ath: float
    close: float
    drawdown: float
    tier: int


@dataclass(frozen=True, slots=True)
class ExcludedPosition:
    """A broker position in an instrument this app does not manage.

    Resolved, not unresolved: the symbol is known via ``instrument_id``, it
    is simply not SCHG or UPRO. Never gets a sidecar row and is never
    touched (DESIGN.md §1.4 "the managed set"; DESIGN_market_data.md §4.7
    "What does not change").
    """

    position_id: str
    instrument_id: int
    symbol: str


@dataclass(frozen=True, slots=True)
class UnresolvedPosition:
    """A position the backfill could not make sense of at all.

    One of the task brief's three named "fail loudly" cases: an
    ``instrumentID`` not in the ``instruments`` table, a SPY series that
    does not cover the position's ``openDateTime``, or a missing required
    broker field (most commonly a null ``openDateTime`` -- ``Position``
    otherwise already guarantees ``position_id``/``instrument_id``/
    ``units``/``entry_price``/``amount`` via ``_parse_position``'s own
    validation, DESIGN_market_data.md §4.2).
    """

    position_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class AdoptionPlan:
    """The full result of resolving a broker portfolio against the sidecar.

    What a dry run reports, and exactly what :func:`run_adoption_backfill`
    writes (via ``to_adopt``) when it is not a dry run. Every position
    handed to :func:`build_adoption_plan` lands in exactly one of the four
    lists below.
    """

    to_adopt: list[LotAnnotation] = field(default_factory=list)
    already_annotated: list[str] = field(default_factory=list)
    excluded: list[ExcludedPosition] = field(default_factory=list)
    unresolved: list[UnresolvedPosition] = field(default_factory=list)

    @property
    def per_symbol_counts(self) -> dict[str, int]:
        """Count of positions planned for adoption, by symbol."""
        counts: dict[str, int] = {}
        for annotation in self.to_adopt:
            counts[annotation.symbol] = counts.get(annotation.symbol, 0) + 1
        return counts

    @property
    def tier_distribution(self) -> dict[int, int]:
        """Count of positions planned for adoption, by derived ``entry_tier``."""
        distribution: dict[int, int] = {}
        for annotation in self.to_adopt:
            if annotation.entry_tier is not None:
                distribution[annotation.entry_tier] = (
                    distribution.get(annotation.entry_tier, 0) + 1
                )
        return distribution

    def report(self) -> str:
        """Human-readable summary for a dry run (task brief: "report
        per-symbol counts, the derived tier distribution, and anything it
        could not resolve").
        """
        lines = ["lot_annotations adoption plan", "=" * 30]
        lines.append(f"to adopt          : {len(self.to_adopt)}")
        for symbol, count in sorted(self.per_symbol_counts.items()):
            lines.append(f"  {symbol:<6}: {count}")
        lines.append(
            f"already annotated : {len(self.already_annotated)} "
            "(skipped -- idempotent re-run)"
        )
        lines.append(f"excluded (unmanaged instrument): {len(self.excluded)}")
        lines.append(f"unresolved        : {len(self.unresolved)}")
        if self.tier_distribution:
            lines.append("tier distribution (of positions to adopt):")
            for tier in sorted(self.tier_distribution):
                lines.append(f"  tier {tier}: {self.tier_distribution[tier]}")
        if self.unresolved:
            lines.append("UNRESOLVED -- the backfill halts on these, writes nothing:")
            for item in self.unresolved:
                lines.append(f"  position_id={item.position_id!r}: {item.reason}")
        return "\n".join(lines)


def build_spy_context_index(spy_bars: Sequence[EodBar]) -> dict[str, SpyContext]:
    """Build a ``'YYYY-MM-DD' -> SpyContext`` map from a Tiingo SPY EOD series.

    ``spy_bars`` need not be pre-sorted -- this sorts by date before the
    running-ATH pass.

    **Basis: raw ``close``** (board nightwatch/handoff-2026-09-05-evening.md §4) -- see
    :class:`SpyContext`'s own docstring for why.

    **Inclusive convention, stated explicitly because DESIGN_market_data.md
    does not spell it out for this specific reconstruction:** the ATH as of
    a given date is the maximum raw ``close`` over every bar up to and
    INCLUDING that date, so a new-ATH day itself has ``drawdown == 0.0``,
    not the day after. This is the natural reading of "entry_spy_ath ...
    computed from openDateTime plus a long Tiingo SPY series" (§4.7), but it
    is a judgment call, not a verified spec detail -- flagged in this task's
    report.
    """
    ordered = sorted(spy_bars, key=lambda bar: bar.date)
    index: dict[str, SpyContext] = {}
    running_ath = float("-inf")
    for bar in ordered:
        running_ath = max(running_ath, bar.close)
        drawdown = (bar.close - running_ath) / running_ath
        index[bar.date] = SpyContext(
            ath=running_ath,
            close=bar.close,
            drawdown=drawdown,
            tier=tier_for_drawdown(drawdown),
        )
    return index


def _entry_trading_date(open_date: str) -> str:
    """Extract the ``'YYYY-MM-DD'`` trading date from a broker ``openDateTime``."""
    return open_date[:10]


def build_adoption_plan(
    positions: Sequence[Position],
    *,
    instruments_by_id: Mapping[int, Instrument],
    spy_context: Mapping[str, SpyContext],
    managed_symbols: Sequence[str],
    already_annotated: Sequence[str] = (),
    book: str = DEFAULT_BOOK,
    origin: str = DEFAULT_ORIGIN,
) -> AdoptionPlan:
    """Resolve broker positions into planned ``lot_annotations`` rows.

    Pure function -- no database or network access, everything it needs is
    passed in -- so it is directly testable against a synthetic portfolio
    and a synthetic SPY series (task brief: "a test that passes because
    nothing happened is not a pass"; this is built to be exercised against a
    populated fixture, not a demo-account-shaped empty one).

    **Never raises.** Every position lands in exactly one of
    ``to_adopt``/``already_annotated``/``excluded``/``unresolved`` so a dry
    run can show the operator the whole picture in one pass, including
    positions that would make a real run halt. It is
    :func:`run_adoption_backfill` that turns a non-empty ``unresolved`` into
    a hard failure before any write.

    Args:
        positions: Every open broker position (``EtoroClient.get_portfolio()``
            -- not fetched here; this module never touches the network).
        instruments_by_id: ``instrument_id -> Instrument``, e.g.
            ``{i.instrument_id: i for i in repo.list_instruments()}``. A
            position whose ``instrument_id`` is absent from this map is
            unresolved, never silently skipped -- DESIGN_market_data.md
            §4.2: there is no symbol string anywhere on a position, so an
            unknown ``instrument_id`` means the symbol genuinely cannot be
            determined, not merely that it is probably unmanaged.
        spy_context: ``'YYYY-MM-DD' -> SpyContext``, from
            :func:`build_spy_context_index`.
        managed_symbols: The managed set (DESIGN.md §1.4) -- normally
            ``SymbolConfig().traded`` (``('SCHG', 'UPRO')``), passed
            explicitly rather than imported here so this stays a pure
            function of its arguments.
        already_annotated: ``position_id``s that already have a sidecar row
            -- from ``{a.position_id for a in repo.list_lot_annotations()}``.
            Makes a re-run idempotent at the planning stage, not only at the
            database's ``INSERT OR IGNORE`` -- a dry run correctly reports
            these as already done rather than as new adoptions.
        book: Ladder for every adopted row (§4.7: assigned, not derived).
        origin: Provenance tag (§4.4's ``origin`` column).
    """
    already = set(already_annotated)
    managed = set(managed_symbols)
    plan = AdoptionPlan()
    # Earliest date the fetched SPY window actually covers -- used only to
    # give a "predates the window" halt (below) a more specific reason than
    # the generic "does not cover" message (board nightwatch/handoff-2026-09-05-evening.md
    # §3a Case 3). Not used for any resolution decision -- `spy_context.get`
    # below is still the sole source of truth for whether a date resolves.
    window_start = min(spy_context) if spy_context else None

    for position in positions:
        instrument = instruments_by_id.get(position.instrument_id)
        if instrument is None:
            plan.unresolved.append(
                UnresolvedPosition(
                    position_id=position.position_id,
                    reason=(
                        f"instrumentID {position.instrument_id} is not in the "
                        "instruments table -- cannot resolve a symbol "
                        "(DESIGN_market_data.md §4.2: no symbol string exists "
                        "on a broker position, so this cannot be recovered "
                        "any other way)"
                    ),
                )
            )
            continue

        if instrument.symbol not in managed:
            plan.excluded.append(
                ExcludedPosition(
                    position_id=position.position_id,
                    instrument_id=position.instrument_id,
                    symbol=instrument.symbol,
                )
            )
            continue

        if position.position_id in already:
            plan.already_annotated.append(position.position_id)
            continue

        if not position.open_date:
            plan.unresolved.append(
                UnresolvedPosition(
                    position_id=position.position_id,
                    reason=(
                        "position has no openDateTime -- cannot derive "
                        "entry_date or look up its SPY context"
                    ),
                )
            )
            continue

        trading_date = _entry_trading_date(position.open_date)
        context = spy_context.get(trading_date)
        if context is None:
            if window_start is not None and trading_date < window_start:
                # board nightwatch/handoff-2026-09-05-evening.md §3a Case 3: a position opened
                # before the fetched SPY window even starts. HALT LOUDLY --
                # do not silently assign a tier. A guessed tier that is
                # indistinguishable from a measured one is this project's
                # exact recurring pathology. There is no override path built
                # here: if the operator later adds one, the assigned tier
                # MUST be 0, not 1 -- a position bought above every price in
                # the window was, by definition, bought at a high (entry
                # drawdown ~0, which is tier 0); asserting tier 1 would claim
                # a -10%-to--15% entry drawdown the data actively
                # contradicts. Recorded as an explicit disagreement with an
                # earlier "just lump it to tier 1" suggestion, not a default
                # to fall into silently.
                #
                # **Near-unreachable as of 2026-09-05** (operator decision,
                # same day, overriding the 819-bar window this halt was
                # originally written against): the live fetch now starts at
                # ``market.window.FULL_HISTORY_START_DATE`` ("2010-01-01"),
                # the ETF-inception floor for both managed symbols, so this
                # branch fires only if a position's own ``openDateTime``
                # somehow predates 2010 -- effectively impossible for a real
                # SCHG/UPRO position. It is intentionally KEPT, not deleted:
                # with a fixed 2010 floor this is no longer an expected path
                # (it was, briefly, under the superseded 819-bar/39-month
                # window, where a long-held SCHG lot could plausibly age
                # past the window) -- it is now a genuine invariant guard
                # against a data anomaly (a corrupt/garbled openDateTime, or
                # a future change that narrows the fetch again without
                # updating this comment). "Widen the fetch window" is no
                # longer actionable advice once the window is already full
                # history; the reason string below says so.
                reason = (
                    f"SPY series does not cover {trading_date!r} -- "
                    f"position {position.position_id!r} opened at "
                    f"{position.open_date!r}, which PREDATES the fetched "
                    f"SPY window (earliest bar {window_start!r}, expected "
                    "to be at or near market.window.FULL_HISTORY_START_DATE "
                    "\"2010-01-01\"). Refusing to fabricate entry_spy_ath/"
                    "entry_spy_close/entry_spy_drawdown/entry_tier for a "
                    "position with no measured context -- this should be "
                    "unreachable for a real SCHG/UPRO position given the "
                    "2010 fetch floor; treat this as a data anomaly "
                    "(corrupt openDateTime, or a fetch window narrower than "
                    "expected) rather than widening the window further."
                )
            else:
                reason = (
                    f"SPY series does not cover {trading_date!r} "
                    f"(openDateTime={position.open_date!r}) -- cannot "
                    "derive entry_spy_ath/entry_spy_close/"
                    "entry_spy_drawdown/entry_tier"
                )
            plan.unresolved.append(
                UnresolvedPosition(position_id=position.position_id, reason=reason)
            )
            continue

        plan.to_adopt.append(
            LotAnnotation(
                position_id=position.position_id,
                symbol=instrument.symbol,
                book=book,
                entry_date=position.open_date,
                entry_amount=position.amount,
                origin=origin,
                # Resolved via measured history, never guessed -- the only
                # path that reaches this line (board nightwatch/handoff-2026-09-05-evening.md
                # §3a: 'assumed_out_of_window' is reserved for a possible
                # future override of the halt above, which this function
                # does not implement).
                entry_context_source=DEFAULT_ENTRY_CONTEXT_SOURCE,
                entry_reference_spy=None,
                entry_spy_ath=context.ath,
                entry_spy_close=context.close,
                entry_spy_drawdown=context.drawdown,
                entry_tier=context.tier,
                bucket_id=None,
                is_open=True,
            )
        )

    return plan


def run_adoption_backfill(
    repo: Repo,
    positions: Sequence[Position],
    spy_bars: Sequence[EodBar],
    *,
    managed_symbols: Sequence[str] | None = None,
    book: str = DEFAULT_BOOK,
    origin: str = DEFAULT_ORIGIN,
    dry_run: bool = True,
) -> AdoptionPlan:
    """The §4.7 one-shot backfill (DESIGN_market_data.md §4.7, §10.9, §10.10).

    Reads the current sidecar and instrument cache from ``repo``, resolves
    every position via :func:`build_adoption_plan`, and -- only when
    ``dry_run`` is ``False`` and resolution succeeded completely -- writes
    the plan's ``to_adopt`` rows in one transaction via
    :meth:`Repo.insert_lot_annotations`.

    **Neither this function nor its caller performs any network I/O.**
    ``positions`` (from ``EtoroClient.get_portfolio()``) and ``spy_bars``
    (from ``TiingoClient.get_eod_history('SPY', ...)``) must both already be
    fetched by the caller. This keeps the migration itself trivially
    testable offline and keeps this module honest about the hard
    constraint it was built under: **no network call to eToro from this
    codepath, ever** -- the dev machine cannot reach eToro at all
    (board nightwatch/handoff-2026-09-04-market-data.md §2.8), and actually running this
    backfill against the live account is an explicit, separate,
    operator-supervised on-device step, not something this function or its
    tests do.

    Args:
        repo: An open, migrated :class:`Repo`.
        positions: Every open broker position, already fetched.
        spy_bars: A SPY EOD history window covering every position's
            ``openDateTime``, already fetched. Must be long enough -- there
            is no fallback if it is not; an uncovered date is unresolved
            (see :func:`build_adoption_plan`) and halts the whole run.
        managed_symbols: Defaults to ``SymbolConfig().traded`` (SCHG, UPRO).
        book: Passed through to :func:`build_adoption_plan`.
        origin: Passed through to :func:`build_adoption_plan`.
        dry_run: When ``True`` (the default -- deliberately safe-by-default
            given this touches 209 real positions, task brief item 5),
            resolves and returns the plan without writing anything, even if
            resolution succeeds. When ``False``, writes ``to_adopt`` if and
            only if every position resolved.

    Returns:
        The :class:`AdoptionPlan` -- inspect ``.report()`` either way.

    Raises:
        AdoptionError: If any position is unresolved, regardless of
            ``dry_run`` -- task brief: "fail loudly ... atomically on
            anything unresolvable", surfaced even in a dry run so the
            operator sees the failure before choosing to proceed, not only
            when they attempt the real write. No database write happens
            before this is raised.
    """
    managed = tuple(managed_symbols) if managed_symbols is not None else SymbolConfig().traded

    instruments_by_id = {
        instrument.instrument_id: instrument for instrument in repo.list_instruments()
    }
    spy_context = build_spy_context_index(spy_bars)
    already_annotated = {
        annotation.position_id for annotation in repo.list_lot_annotations()
    }

    plan = build_adoption_plan(
        positions,
        instruments_by_id=instruments_by_id,
        spy_context=spy_context,
        managed_symbols=managed,
        already_annotated=already_annotated,
        book=book,
        origin=origin,
    )

    if plan.unresolved:
        raise AdoptionError(
            f"{len(plan.unresolved)} position(s) could not be resolved; "
            "refusing to write any part of the backfill (task brief: fail "
            "loudly and atomically). Detail:\n" + plan.report()
        )

    if not dry_run and plan.to_adopt:
        inserted = repo.insert_lot_annotations(plan.to_adopt)
        if inserted != len(plan.to_adopt):
            # Should be unreachable given the already-annotated filter
            # above -- reaching here means a row for one of these
            # position_ids was written concurrently, between planning and
            # this write. Surfacing that loudly beats silently
            # under-reporting what actually landed.
            raise AdoptionError(
                f"planned to insert {len(plan.to_adopt)} rows but only "
                f"{inserted} were actually new -- a row must have been "
                "written concurrently. insert_lot_annotations is idempotent, "
                "so re-running this backfill is safe and will confirm the "
                "sidecar is consistent."
            )

    return plan
