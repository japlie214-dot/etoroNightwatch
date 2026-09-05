"""Bridge between ``db.repo.StrategyState`` and :class:`nightwatch.strategy.
ladder.LadderState` (DESIGN.md §1.4).

**The warmup trap, and what this module actually fixes.** ``strategy/
ladder.py``'s :func:`~nightwatch.strategy.ladder.decide` is a pure function:
it takes a :class:`~nightwatch.strategy.ladder.LadderState` in and returns a
new one out, incrementing ``bars`` by exactly one each call. It has no
memory of its own between calls -- by design, so ``DESIGN_sandbox.md`` §2's
runner (which constructs a *fresh* strategy object every simulated day) can
exercise it identically to how a live session would. That means the ENTIRE
burden of "does ``bars`` actually persist across restarts" falls on whoever
calls :func:`decide` -- for the live app, that is :func:`load`/:func:`save`
below. If a caller calls :func:`~nightwatch.strategy.ladder.decide` every
session without first calling :func:`load` (or without calling :func:`save`
afterward), ``state.bars`` is 1 on every single call forever, no matter how
many real sessions have actually run -- see
``tests/test_strategy_state.py``'s
``test_missing_save_between_sessions_replays_bar_one_forever``.

``config.StrategyConfig.warmup_bars`` is 0 in live config, so the bar-count
gate itself (``bars <= warmup_bars``) is already a permanent no-op in
production -- but ``strategy/cone.py``'s ``cone_5_z`` still returns ``None``
until 265 real bars of SPY history exist, which is what actually gates
trading in the live app (``market/window.py`` supplies that fresh every
session, unrelated to this module). This module's persistence of ``bars``
therefore matters for two things that are NOT the live no-op case: (1) any
future run with ``warmup_bars > 0`` (a sandbox scenario, or a config change),
and (2) diagnostics/parity -- a ``bars`` counter that silently resets on every
restart is itself a symptom worth catching even where nothing currently
reads it as a gate.

Everything else this module bridges (``recyclable_cash``, the annual budget
figures, ``below_streak``, the sale-batch sequence/date, ``ath``) is
genuinely load-bearing regardless of ``warmup_bars`` -- e.g. losing
``recyclable_cash`` on a restart would silently forget cash the strategy had
already earmarked, and losing ``ath`` would make the very next session
mis-detect whether today is a new-ATH bar (``ladder.decide``'s
``new_ath_bar`` check compares against the PRIOR session's persisted
``ath``, not a value it can recompute itself -- see ``ladder.py``'s own
comment on why: ``market/window.py`` recomputes the full-history ATH fresh
every session, so there is no "yesterday's ath" available except through
this persisted value).

``strategy_state`` is a singleton row (``db.repo.get_strategy_state``/
``save_strategy_state``) -- it does NOT carry open lots (``lot_annotations``,
a separate table, already reconciled by the caller before ``decide`` is ever
called -- fix #3, ``ladder.py``'s module docstring) or re-entry buckets
(``buckets``, also a separate table, read/written directly via
``db.repo.Repo``'s own bucket methods). Only the fields
:class:`~nightwatch.strategy.ladder.LadderState` and
``db.repo.StrategyState`` share are bridged here.
"""

from __future__ import annotations

from ..strategy.ladder import LadderState

__all__ = ["to_ladder_state", "from_ladder_state", "load", "save"]


def to_ladder_state(row: object) -> LadderState:
    """Map a ``db.repo.StrategyState`` row onto a
    :class:`~nightwatch.strategy.ladder.LadderState`.

    Duck-typed (reads attributes by name) rather than importing
    ``db.repo.StrategyState`` as a type annotation, so this function alone
    has no hard dependency on the exact dataclass -- only :func:`load`/
    :func:`save` below import ``db.repo`` for real, keeping the mapping
    itself trivially unit-testable with a plain namespace/mock.

    ``trail_state`` has no ``db.repo.StrategyState`` column -- ``exit_mode``
    is ``"fixed"`` in live config (DESIGN.md §1.4), so it is empty in
    production today. It round-trips as ``{}`` here; a future migration
    would need its own column if ``exit_mode="trailing"`` is ever armed
    live.
    """
    return LadderState(
        bars=row.bars,  # type: ignore[attr-defined]
        ath=row.ath,  # type: ignore[attr-defined]
        below_streak=row.below_streak,  # type: ignore[attr-defined]
        recyclable_cash=row.recyclable_cash,  # type: ignore[attr-defined]
        budget_year=row.budget_year,  # type: ignore[attr-defined]
        budget=row.budget,  # type: ignore[attr-defined]
        order_size=row.order_size,  # type: ignore[attr-defined]
        cash_target=row.cash_target,  # type: ignore[attr-defined]
        sale_batch_sequence=row.sale_batch_sequence,  # type: ignore[attr-defined]
        sale_batch_date=row.sale_batch_date,  # type: ignore[attr-defined]
        trail_state={},
    )


def from_ladder_state(state: LadderState, *, strategy_state_cls: type) -> object:
    """Map a :class:`~nightwatch.strategy.ladder.LadderState` onto a fresh
    ``db.repo.StrategyState`` (passed in as ``strategy_state_cls`` so this
    function, too, has no import-time dependency on ``db.repo`` -- only
    :func:`save` below wires the real class in).
    """
    return strategy_state_cls(
        bars=state.bars, ath=state.ath, below_streak=state.below_streak,
        recyclable_cash=state.recyclable_cash, budget_year=state.budget_year,
        budget=state.budget, order_size=state.order_size,
        cash_target=state.cash_target,
        sale_batch_sequence=state.sale_batch_sequence,
        sale_batch_date=state.sale_batch_date,
    )


def load(repo: object) -> LadderState:
    """Load the live strategy state from SQLite (``db.repo.Repo.
    get_strategy_state``), for the caller to pass into
    ``strategy.ladder.decide`` as this session's starting state.

    Must be called exactly once per session, BEFORE the first
    ``ladder.decide`` call of that session -- calling ``decide`` with a
    fresh, un-loaded ``LadderState()`` is precisely the warmup trap this
    module exists to prevent (see module docstring).
    """
    return to_ladder_state(repo.get_strategy_state())  # type: ignore[attr-defined]


def save(repo: object, state: LadderState) -> None:
    """Persist ``state`` (the ``new_state`` a ``ladder.decide`` call just
    returned) via ``db.repo.Repo.save_strategy_state``.

    Must be called exactly once per session, AFTER the session's
    ``ladder.decide`` call -- skipping this call is the other half of the
    warmup trap: ``load`` would then read back the PREVIOUS session's state
    forever, ``bars`` would never advance past whatever it last was saved
    at, and (worse, for anything reading it) ``recyclable_cash``/``ath``/
    the budget figures would silently go stale.
    """
    from ..db.repo import StrategyState  # local import: only save() needs the real type

    repo.save_strategy_state(from_ladder_state(state, strategy_state_cls=StrategyState))  # type: ignore[attr-defined]
