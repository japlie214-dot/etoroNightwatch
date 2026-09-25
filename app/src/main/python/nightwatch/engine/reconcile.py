"""Broker-vs-database position reconciliation (DESIGN §1.4 fix #3, §5 gate 11;
DESIGN_market_data.md §4.5's "inverted gate 11"). Built in step 6.

**Design decision, made here rather than left ambiguous:** the task brief and
this module's own former stub both name ``db.repo.Repo.open_lots`` /
``get_lot_by_position_id`` (the ``lots`` table) as the reconciliation source.
That table only ever holds UPRO rungs (``lots.book`` is constrained to
``'catapult'``/``'reentry'``, and DESIGN.md §1.4/HANDOFF §7 confirm the ladder
tracks UPRO per-lot but SCHG only as a scalar) -- it structurally cannot
supply SCHG rows. DESIGN_market_data.md §4.5 (required reading for this task)
supersedes the original stub with the "inverted gate 11": the managed set is
{UPRO, SCHG}, and its sidecar is ``lot_annotations`` / :class:`LotAnnotation`,
not ``lots`` -- confirmed by the live probe reconciling 209 real positions
(202 SCHG + 7 UPRO, board nightwatch/handoff-2026-09-06.md §5) and by
``Repo.open_lot_annotation_position_ids`` already existing with a docstring
naming it "gate 11 invariant 2's side" of exactly this check. Reconciling
UPRO-only against ``lots`` would silently pass while 202 of 209 real managed
positions went unchecked -- so this module compares against
``lot_annotations`` via :class:`LotAnnotation`, not ``Lot``.

**What this compares:** eToro's live open positions (already fetched
elsewhere this session -- this module makes no network calls, matching gate
10's pure-function-over-fetched-data shape) against every ``lot_annotations``
row (open or closed -- a closed row for a still-open broker position is
itself a discrepancy, see below), for the managed set only. Two directions,
both required:

  1. A broker position, in a managed symbol, with no matching
     ``lot_annotations`` row -- opened outside the app, or the sidecar was
     lost. **NEVER fabricate a lot for it** (DESIGN.md §1.4 fix #3): that was
     bug #3 in the original backtest code, and it destroys book/vintage/ATH
     metadata. Surfaced as a discrepancy for preflight gate 11 to fail on,
     nothing more -- this module decides no policy.
  2. A ``lot_annotations`` row marked open with no matching broker position
     -- most likely a close that filled but was never recorded. Also
     surfaced, never silently dropped.

  As a direct extension of (1) that costs nothing extra to check: a broker
  position that IS still open but whose matching ``lot_annotations`` row is
  marked *closed* is exactly as dangerous as no row at all (an "assumed
  complete" close that wasn't, DESIGN_session.md §3.4) -- folded into the
  same ``unmatched_broker_position`` discrepancy kind rather than inventing a
  third one, since the failure mode is identical: no *open* sidecar row for
  an open broker position.

A position in a non-managed symbol is ignored here entirely (display-only,
DESIGN.md §1.4/§6.D) -- it is neither of the two directions above.

``preflight.py``'s gate 11 calls :func:`reconcile` and fails loudly on any
non-empty result -- this module does not decide policy, only detects and
reports.

**Auto-adopt path added 2026-09-06 (DESIGN_session.md §3.4, the operator's
"no forced human-in-the-loop" decision):** a persistently non-terminal order
is, after a config-driven polling horizon, marked abandoned and resolved
against observed account state rather than escalated to a human forever
(there is no cancel endpoint -- ``etoro/client.py`` grep-verified -- so
state-based resolution is the only automatic option). The one thing that
would otherwise force a human back in: if an abandoned order later fills,
direction 1 above sees an unmatched broker position and fails the gate
closed. :func:`resolve_abandoned_intent_adoptions` is the narrow exception:
an unmatched broker position that matches one of THIS APP'S OWN persisted,
abandoned :class:`~nightwatch.db.repo.Intent` rows (by instrument and size --
see that function) is adopted from that record instead of failing.

**THE CRITICAL DISTINCTION from the fabricate-a-lot bug above (DESIGN.md
§1.4 fix #3): provenance is the entire difference.** Bug #3 invented a lot
from nothing, losing book/vintage/ATH metadata, for ANY unmatched broker
position. This path holds a genuine :class:`~nightwatch.db.repo.Intent` this
process itself persisted before ever submitting the order (DESIGN §5's
idempotency anchor) -- it is not reachable for a bare unmatched position with
no such record; see the function's own docstring for how that is made
structural rather than conventional. A bare unmatched broker position with
no matching intent still fails this gate exactly as before -- this is not a
weakening of direction 1 by one inch, only an additional, narrowly-gated
resolution for the one case where genuine provenance exists.

Governing principle: halts are reserved for money-losing ambiguity, never
for bookkeeping ambiguity. A single, unambiguous provenance match is
bookkeeping-clear and resolved automatically; more than one candidate match
is genuine money-losing ambiguity about which of our own orders this is,
and is refused exactly like no match at all (see
:func:`resolve_abandoned_intent_adoptions`).

**The mirror-image gap, closed 2026-09-06:** the paragraph above only ever
resolves direction 1 (:func:`resolve_abandoned_intent_adoptions` handles an
abandoned BUY intent that later fills). That function's own docstring was
explicit that a stuck CLOSE resolves via direction 2 instead -- but until
now direction 2 (``unmatched_open_lot``: an annotation still says open, but
the broker position is gone) had no auto-resolution at all, so an abandoned
close that later filled left the same forced-human-in-the-loop outcome the
2026-09-06 decision was meant to remove everywhere.
:func:`resolve_abandoned_close_intents` is the symmetric counterpart: an
``unmatched_open_lot`` discrepancy that matches one of THIS APP'S OWN
persisted, abandoned CLOSE :class:`~nightwatch.db.repo.Intent` rows (by
``position_id``, ``symbol``, and ``book`` -- see that function) is resolved
by marking the existing annotation closed, via
:meth:`~nightwatch.db.repo.Repo.update_lot_annotation`'s existing in-place
path, instead of failing the gate.

The provenance rule is exactly as structural here as on the buy side, not
merely conventional: no matching abandoned close intent of ours, no
resolution -- the discrepancy still fails the gate exactly as before. In
particular this must never resolve a position that vanished for any OTHER
reason (a broker-side liquidation, or the operator closing something by
hand in the eToro app) -- those have no matching close intent of ours and
correctly keep failing the gate; see :func:`resolve_abandoned_close_intents`
and its dedicated tests for how that is enforced. Marked permanently
auditable via ``lot_annotations.exit_context_source`` (migration 007) --
the mirror of ``entry_context_source``'s ``'adopted_from_abandoned_intent'``
but on the exit side, since ``entry_context_source`` is immutable once
stamped at open and this event happens strictly later, at close time.

TODO (DESIGN_session.md §3.4/§6, board nightwatch/handoff-2026-09-06.md §7 -- partial closes,
NOT built here): once ``close_position`` can send a real ``UnitsToDeduct``,
a partially-closed position stays OPEN with reduced units/amount instead of
closing. That needs a THIRD invariant -- the sidecar's recorded
units/amount for a still-open position must match the broker's own (now
smaller) figure -- and an update-in-place path on ``lot_annotations``, which
today only ever inserts once at open (:meth:`Repo.insert_lot_annotation`) and
has no column to compare against (:class:`LotAnnotation` stores
``entry_amount``, stamped once, never the broker's current reduced amount).
Do not add a units/amount mismatch check against ``entry_amount`` as a
stand-in for this -- ``entry_amount`` is deliberately immutable order-entry
provenance (§4.4), not a running balance, and comparing it against a
post-partial-close broker figure would misfire on every legitimate partial
close. Left unbuilt here, as directed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence

from ..db.repo import Intent, LotAnnotation
from ..etoro.models import Position

__all__ = [
    "Discrepancy",
    "Adoption",
    "Resolution",
    "reconcile",
    "resolve_abandoned_intent_adoptions",
    "resolve_abandoned_close_intents",
]

_KINDS = ("unmatched_broker_position", "unmatched_open_lot")

#: The distinct ``lot_annotations.entry_context_source`` value stamped on a
#: lot built by :func:`resolve_abandoned_intent_adoptions` -- never
#: ``'derived'``, so this lot's provenance stays greppable and auditable
#: forever after (migration 006; see :class:`~nightwatch.db.repo.LotAnnotation`).
ADOPTED_FROM_ABANDONED_INTENT = "adopted_from_abandoned_intent"

#: The distinct ``lot_annotations.exit_context_source`` value stamped on an
#: annotation closed by :func:`resolve_abandoned_close_intents` -- the
#: mirror of :data:`ADOPTED_FROM_ABANDONED_INTENT` on the exit side, so this
#: annotation's close provenance stays greppable and auditable forever after
#: (migration 007; see :class:`~nightwatch.db.repo.LotAnnotation`).
RESOLVED_FROM_ABANDONED_INTENT = "resolved_from_abandoned_intent"


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """One reconciliation mismatch between eToro and the sidecar.

    ``kind`` is one of:

    * ``"unmatched_broker_position"`` -- a live, managed-symbol broker
      position with no matching *open* ``lot_annotations`` row (either no
      row at all, or one present but marked closed).
    * ``"unmatched_open_lot"`` -- a ``lot_annotations`` row marked open with
      no matching live broker position.

    Deliberately smaller than a full :class:`~nightwatch.db.repo.Lot`/
    :class:`~nightwatch.db.repo.LotAnnotation` row -- it carries only what a
    preflight failure needs to name the problem and alert on it (mirrors
    :class:`~nightwatch.engine.preflight.GateResult`'s ``reason``/``details``
    split for the same reason: a human-readable message and machine-usable
    fields, not one parsed out of the other).
    """

    kind: str
    position_id: str
    symbol: str | None
    reason: str
    details: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(
                f"Discrepancy.kind must be one of {_KINDS!r}, got {self.kind!r}"
            )


def reconcile(
    positions: Sequence[Position],
    annotations: Sequence[LotAnnotation],
    *,
    managed_symbols: Sequence[str],
    instrument_symbols: Mapping[int, str],
) -> list[Discrepancy]:
    """Compare eToro's live open ``positions`` against every sidecar
    ``lot_annotations`` row (``annotations`` -- open and closed both; see
    the module docstring for why closed rows matter too), for the managed
    set only. Detects, never decides: never fabricates a lot, never closes
    one, never picks a winner -- that is the caller's (gate 11's) job.

    Args:
        positions: Every currently open broker position, already fetched
            this session (this module makes no network calls).
        annotations: Every ``lot_annotations`` row, open or closed -- e.g.
            ``Repo.list_lot_annotations()`` with no filter. Passing only the
            open subset would silently blind direction 1's
            open-broker-position/closed-sidecar-row case.
        managed_symbols: The managed set (DESIGN.md §1.4) -- normally
            ``SymbolConfig().traded`` (SCHG, UPRO). A position outside this
            set is display-only and never produces a discrepancy.
        instrument_symbols: ``instrument_id -> symbol`` for every instrument
            this app has resolved (``Repo.get_instrument_by_id`` per id, or
            equivalently ``{i.instrument_id: i.symbol for i in
            repo.list_instruments()}``) -- a broker :class:`Position` itself
            carries no symbol (§4.2). An ``instrument_id`` missing from this
            mapping is treated as non-managed, not as a failure: gate 8 (the
            Instruments gate) already guarantees every managed symbol is
            resolved and cached before gate 11 ever runs, so a genuine
            managed-symbol miss here cannot happen without gate 8 itself
            having already failed the session first.

    Returns:
        A list of :class:`Discrepancy`, empty when broker and sidecar fully
        agree on membership for the managed set.
    """
    managed = set(managed_symbols)
    by_position_id: dict[str, LotAnnotation] = {a.position_id: a for a in annotations}
    broker_position_ids = {p.position_id for p in positions}

    discrepancies: list[Discrepancy] = []

    for position in positions:
        symbol = instrument_symbols.get(position.instrument_id)
        if symbol is None or symbol not in managed:
            # Non-managed holding, or an instrument this app has never
            # resolved -- display-only, never a discrepancy (DESIGN.md
            # §1.4, §6.D; DESIGN_market_data.md §4.5).
            continue

        annotation = by_position_id.get(position.position_id)
        if annotation is None:
            discrepancies.append(
                Discrepancy(
                    kind="unmatched_broker_position",
                    position_id=position.position_id,
                    symbol=symbol,
                    reason=(
                        f"open broker position {position.position_id} ({symbol}) "
                        "has no lot_annotations row -- opened outside the app, "
                        "or the sidecar was lost. Refusing to fabricate one "
                        "(DESIGN.md §1.4 fix #3)."
                    ),
                )
            )
        elif not annotation.is_open:
            discrepancies.append(
                Discrepancy(
                    kind="unmatched_broker_position",
                    position_id=position.position_id,
                    symbol=symbol,
                    reason=(
                        f"broker position {position.position_id} ({symbol}) is "
                        "still open, but its lot_annotations row is marked "
                        "closed -- an assumed-complete close that wasn't "
                        "(DESIGN_session.md §3.4)."
                    ),
                )
            )

    for annotation in annotations:
        if not annotation.is_open:
            continue
        if annotation.position_id not in broker_position_ids:
            discrepancies.append(
                Discrepancy(
                    kind="unmatched_open_lot",
                    position_id=annotation.position_id,
                    symbol=annotation.symbol,
                    reason=(
                        f"lot_annotations row for {annotation.position_id} "
                        f"({annotation.symbol}) is marked open, but no matching "
                        "broker position was found -- most likely closed "
                        "outside the app and never recorded."
                    ),
                )
            )

    return discrepancies


@dataclass(frozen=True, slots=True)
class Adoption:
    """One broker position resolved from :func:`reconcile`'s
    ``unmatched_broker_position`` discrepancy list against this app's own
    persisted, abandoned :class:`~nightwatch.db.repo.Intent` record, rather
    than left to fail gate 11 closed.

    ``intent`` is carried alongside ``annotation`` (not discarded once the
    match is made) deliberately: it is the auditable proof this adoption had
    genuine provenance, and the caller needs it to update the intent's own
    status once the annotation is actually persisted (this module makes no
    DB writes -- see :func:`resolve_abandoned_intent_adoptions`).

    There is no constructor path for this dataclass that does not require a
    real :class:`~nightwatch.db.repo.Intent` in hand -- ``intent`` is a
    required, non-optional field -- and the only place that builds one
    (:func:`resolve_abandoned_intent_adoptions`) only ever does so from an
    entry drawn from the ``abandoned_intents`` sequence its own caller
    supplied. Provenance is enforced by the type, not by a comment.
    """

    position_id: str
    intent: Intent
    annotation: LotAnnotation


def _match_abandoned_intent(
    position: Position,
    *,
    symbol: str,
    abandoned_intents: Sequence[Intent],
    size_tolerance_pct: float,
) -> Intent | None:
    """Find the one persisted, abandoned buy :class:`Intent` that accounts
    for ``position``, or ``None`` if there is no such record -- structurally
    the only way :func:`resolve_abandoned_intent_adoptions` can ever adopt a
    position, since it is the sole source of the ``Intent`` an
    :class:`Adoption` requires.

    A candidate must satisfy every one of:

    * ``status == "abandoned"`` -- filtered here, not trusted from the
      caller, so passing a broader set of intents (e.g. every intent this
      session ever wrote) is safe rather than a footgun.
    * ``side == "buy"`` -- only a buy creates a NEW broker position; a
      stuck close resolves via the ``unmatched_open_lot`` direction
      instead, never this one.
    * ``instrument_id`` matches ``position.instrument_id``, and the
      intent's own recorded ``symbol`` matches ``symbol`` (the symbol gate
      11 already resolved for this position) -- both must agree, not just
      one, since a mismatch between them would itself be a sign something
      is wrong with the record, not a reason to trust it more.
    * ``position_id`` is either unset (the ordinary case -- this order was
      never confirmed to reach a position) or already equal to
      ``position.position_id`` -- an intent recorded against some OTHER
      position is never a candidate for this one.
    * ``book`` is a value :class:`~nightwatch.db.repo.LotAnnotation` can
      actually hold (``'catapult'``/``'reentry'`` -- ``intents.book`` also
      permits ``'core'``, which is not a valid lot book and is refused here
      rather than silently coerced).
    * ``amount_usd`` (the requested notional) is within
      ``size_tolerance_pct`` of ``position.amount`` (the broker's actual,
      authoritative cost basis, §4.5) -- tolerates ordinary market-order
      slippage between decision and fill without matching two orders of
      meaningfully different size.

    Exactly one surviving candidate is adopted. Zero candidates means no
    provenance exists for this position -- it stays a discrepancy, exactly
    as it always has. **More than one candidate is refused, not
    arbitrarily resolved**: this is genuine money-losing ambiguity about
    which of our own orders this position actually is, and the governing
    principle (halts are for money-losing ambiguity, never bookkeeping
    ambiguity) puts it on the same side of the line as no match at all.
    """
    candidates: list[Intent] = []
    for intent in abandoned_intents:
        if intent.status != "abandoned":
            continue
        if intent.side != "buy":
            continue
        if intent.instrument_id != position.instrument_id:
            continue
        if intent.symbol != symbol:
            continue
        if intent.position_id is not None and intent.position_id != position.position_id:
            continue
        if intent.book not in ("catapult", "reentry"):
            continue
        if intent.amount_usd is None or intent.amount_usd <= 0:
            continue
        deviation = abs(position.amount - intent.amount_usd) / intent.amount_usd
        if deviation > size_tolerance_pct:
            continue
        candidates.append(intent)

    if len(candidates) != 1:
        return None
    return candidates[0]


def _build_adopted_annotation(
    position: Position, intent: Intent, *, symbol: str
) -> LotAnnotation | None:
    """Build the sidecar row for an adoption, sourced ONLY from ``position``
    (the broker's own authoritative facts, exactly like any other lot) and
    ``intent`` (this app's own persisted record) -- never guessed. Returns
    ``None`` if either source lacks something a valid
    :class:`~nightwatch.db.repo.LotAnnotation` requires, in which case the
    caller must NOT adopt (falls back to the ordinary fail-closed
    discrepancy).

    Deliberately conservative beyond what :func:`_match_abandoned_intent`
    already checked: refuses a short or leveraged position outright (DESIGN
    §6.A -- this app never shorts, never leverages above 1) even though
    gate 11's ordinary direction-1 check does not itself inspect
    ``is_buy``/``leverage`` today -- an auto-adopted lot is exactly the
    place to hold that line most tightly, since nothing else double-checks
    it before this annotation is persisted.

    Entry-SPY-context fields (``entry_reference_spy``, ``entry_spy_ath``,
    etc.) are left ``None`` -- deriving them from history the way
    ``market.adopt`` does for a pre-existing position is out of scope here
    (this task touches only ``engine/reconcile.py``/``engine/preflight.py``)
    and leaving them unset is honest, not a fabrication: every other
    optional column on this dataclass already tolerates ``None``.
    """
    if not position.is_buy or position.leverage != 1:
        return None
    if position.open_date is None:
        return None
    return LotAnnotation(
        position_id=position.position_id,
        symbol=symbol,
        book=intent.book,  # validated by _match_abandoned_intent
        entry_date=position.open_date,
        entry_amount=position.amount,
        origin="app",
        entry_context_source=ADOPTED_FROM_ABANDONED_INTENT,
        bucket_id=intent.bucket_id,
    )


def resolve_abandoned_intent_adoptions(
    positions: Sequence[Position],
    annotations: Sequence[LotAnnotation],
    discrepancies: Sequence[Discrepancy],
    abandoned_intents: Sequence[Intent],
    *,
    size_tolerance_pct: float,
) -> tuple[list[Discrepancy], list[Adoption]]:
    """Resolve as many ``unmatched_broker_position`` entries in
    ``discrepancies`` (:func:`reconcile`'s own output) as have genuine
    provenance in ``abandoned_intents``, per the module docstring's
    "auto-adopt path". Returns ``(remaining_discrepancies, adoptions)`` --
    every discrepancy not resolved this way passes through unchanged,
    ``unmatched_open_lot`` entries always pass through unchanged (this path
    only ever addresses direction 1), and an empty ``abandoned_intents``
    (the default everywhere this is called from) makes this function a
    no-op that returns ``discrepancies`` verbatim.

    Only the "no row at all" shape of ``unmatched_broker_position`` is
    eligible -- NOT the "row exists but marked closed" shape (an
    assumed-complete close that wasn't, DESIGN_session.md §3.4): that is a
    different anomaly (a sidecar row already claims a definite, contradicted
    answer) and auto-adopting over it would paper over a genuine
    discrepancy rather than fill an honest gap. Distinguished here by
    looking the position back up in ``annotations`` directly, the same way
    :func:`reconcile` itself does.

    This function makes no DB writes -- like :func:`reconcile` and gate 11
    itself, it only detects and reports; persisting each :class:`Adoption`'s
    ``annotation`` (and marking its ``intent`` resolved) is the caller's
    job, once one exists (``engine/session.py``, explicitly out of scope
    here).
    """
    positions_by_id = {p.position_id: p for p in positions}
    annotations_by_id = {a.position_id: a for a in annotations}

    remaining: list[Discrepancy] = []
    adoptions: list[Adoption] = []

    for discrepancy in discrepancies:
        if discrepancy.kind != "unmatched_broker_position":
            remaining.append(discrepancy)
            continue
        if annotations_by_id.get(discrepancy.position_id) is not None:
            # A row exists but is marked closed -- the "assumed-complete
            # close that wasn't" shape, not "no provenance at all". Not
            # eligible; fails closed exactly as before.
            remaining.append(discrepancy)
            continue
        position = positions_by_id.get(discrepancy.position_id)
        if position is None or discrepancy.symbol is None:
            # Cannot happen given how reconcile() builds this discrepancy
            # kind, but defensive rather than assumed.
            remaining.append(discrepancy)
            continue

        intent = _match_abandoned_intent(
            position,
            symbol=discrepancy.symbol,
            abandoned_intents=abandoned_intents,
            size_tolerance_pct=size_tolerance_pct,
        )
        if intent is None:
            remaining.append(discrepancy)
            continue

        annotation = _build_adopted_annotation(position, intent, symbol=discrepancy.symbol)
        if annotation is None:
            remaining.append(discrepancy)
            continue

        adoptions.append(
            Adoption(position_id=position.position_id, intent=intent, annotation=annotation)
        )

    return remaining, adoptions


@dataclass(frozen=True, slots=True)
class Resolution:
    """One :func:`reconcile`'s ``unmatched_open_lot`` discrepancy resolved
    against this app's own persisted, abandoned CLOSE :class:`~nightwatch
    .db.repo.Intent` record, rather than left to fail gate 11 closed --
    the exact mirror of :class:`Adoption` on the close side.

    ``annotation`` here is the vanished position's EXISTING sidecar row,
    updated only to ``is_open=False`` and ``exit_context_source`` stamped
    (never re-created from scratch the way :class:`Adoption`'s annotation
    is -- there is nothing to adopt on this side, only an already-known lot
    to mark closed). ``intent`` is carried alongside it for the same reason
    as :class:`Adoption`'s: it is the auditable proof this resolution had
    genuine provenance, and the caller needs it to update the intent's own
    status once the annotation is actually persisted (this module makes no
    DB writes -- see :func:`resolve_abandoned_close_intents`).

    There is no constructor path for this dataclass that does not require a
    real :class:`~nightwatch.db.repo.Intent` in hand -- ``intent`` is a
    required, non-optional field -- and the only place that builds one
    (:func:`resolve_abandoned_close_intents`) only ever does so from an
    entry drawn from the ``abandoned_intents`` sequence its own caller
    supplied. Provenance is enforced by the type, not by a comment.
    """

    position_id: str
    intent: Intent
    annotation: LotAnnotation


def _match_abandoned_close_intent(
    annotation: LotAnnotation,
    *,
    abandoned_intents: Sequence[Intent],
) -> Intent | None:
    """Find the one persisted, abandoned CLOSE :class:`Intent` that accounts
    for ``annotation``'s vanished broker position, or ``None`` if there is
    no such record -- structurally the only way
    :func:`resolve_abandoned_close_intents` can ever resolve an
    ``unmatched_open_lot`` discrepancy, since it is the sole source of the
    ``Intent`` a :class:`Resolution` requires. The mirror of
    :func:`_match_abandoned_intent`, adjusted for what is actually
    available on this side: there is no live broker :class:`Position` to
    match against here -- the position is precisely what is missing -- so
    identity is drawn entirely from ``annotation`` itself, never guessed.

    A candidate must satisfy every one of:

    * ``status == "abandoned"`` -- filtered here, not trusted from the
      caller, exactly like the buy side.
    * ``side == "close"`` -- only a close order can make a position vanish;
      an abandoned BUY intent resolves via
      :func:`resolve_abandoned_intent_adoptions` instead, never this one
      (the exact mirror of the buy side's own ``side == "buy"`` filter).
    * ``position_id`` matches ``annotation.position_id`` exactly. Unlike a
      buy intent (whose ``position_id`` starts unset until a fill confirms
      it), a close intent's ``position_id`` is set at creation time --
      schema.sql: "set for side='close'" -- identifying which position it
      targets before the order is ever sent. This is therefore a direct,
      exact join key here, not a fuzzy match, and is REQUIRED (never
      treated as optional the way the buy side tolerates an unset
      ``position_id``).
    * the intent's own recorded ``symbol`` matches ``annotation.symbol`` --
      both must agree, not just one, for the same reason as the buy side:
      a mismatch between them is itself a sign something is wrong with the
      record, not a reason to trust it more.
    * ``book`` matches ``annotation.book`` -- both must agree. Unlike the
      buy side (which validates the intent's ``book`` is a value
      :class:`~nightwatch.db.repo.LotAnnotation` can hold, because it is
      about to build a NEW annotation from it), this path never writes a
      new ``book`` -- the annotation already has one -- so the check here
      is identity agreement, not validity: a genuine record of closing
      THIS lot would record the same book it was opened under.

    Deliberately does **not** compare any size/amount figure: unlike a buy
    (where the broker's actual fill amount can differ slightly from the
    requested amount, needing a tolerance), there is no broker-observed
    quantity left to compare against on this side -- the position is gone,
    and ``lot_annotations`` itself carries no ``units``/``amount`` column
    (see this module's own docstring, and
    :meth:`~nightwatch.db.repo.Repo.update_lot_annotation`'s explicit
    warning against misusing ``entry_amount`` as a stand-in for one). The
    three exact-identity fields above (``position_id``, ``symbol``, ``book``)
    are the correct mirror of the buy side's "instrument_id AND symbol AND
    size-within-tolerance" -- more than one field, all structural, none
    fuzzy -- not a weaker check for having no tolerance parameter.

    Exactly one surviving candidate is resolved. Zero candidates means no
    provenance exists for this vanished position -- it stays a discrepancy,
    exactly as it always has (this is what correctly keeps failing the gate
    for a broker-side liquidation or a hand-close in the eToro app: neither
    has a matching abandoned close intent of ours). **More than one
    candidate is refused, not arbitrarily resolved**: genuine money-losing
    ambiguity about which of our own close orders this vanished position
    actually was, on the same side of the line as no match at all.
    """
    candidates: list[Intent] = []
    for intent in abandoned_intents:
        if intent.status != "abandoned":
            continue
        if intent.side != "close":
            continue
        if intent.position_id != annotation.position_id:
            continue
        if intent.symbol != annotation.symbol:
            continue
        if intent.book != annotation.book:
            continue
        candidates.append(intent)

    if len(candidates) != 1:
        return None
    return candidates[0]


def _build_resolved_annotation(annotation: LotAnnotation, intent: Intent) -> LotAnnotation:
    """Build the closed sidecar row for a resolution, sourced ONLY from
    ``annotation`` (the existing, already-persisted sidecar row -- never
    re-derived or guessed) with ``is_open`` flipped and
    ``exit_context_source`` stamped. Unlike
    :func:`_build_adopted_annotation`, this never constructs a NEW
    :class:`~nightwatch.db.repo.LotAnnotation` from scratch -- there is
    nothing to adopt here, only an already-known lot to mark closed -- so
    every other field (book, symbol, entry-SPY-context, ``bucket_id``, ...)
    passes through completely untouched via :func:`dataclasses.replace`.

    ``intent`` is accepted (and required) purely so the caller cannot build
    this without one in hand -- mirroring :func:`_build_adopted_annotation`'s
    own signature -- even though, unlike the buy side, no field of
    ``intent`` is actually copied onto the annotation (there is nothing left
    to copy: the annotation already carries its own book/symbol/entry
    context, all of it correct and untouched by this close).
    """
    return replace(
        annotation,
        is_open=False,
        exit_context_source=RESOLVED_FROM_ABANDONED_INTENT,
    )


def resolve_abandoned_close_intents(
    annotations: Sequence[LotAnnotation],
    discrepancies: Sequence[Discrepancy],
    abandoned_intents: Sequence[Intent] = (),
) -> tuple[list[Discrepancy], list[Resolution]]:
    """Resolve as many ``unmatched_open_lot`` entries in ``discrepancies``
    (:func:`reconcile`'s own output) as have genuine provenance in
    ``abandoned_intents``, per the module docstring's "mirror-image gap"
    section -- the CLOSE-side counterpart of
    :func:`resolve_abandoned_intent_adoptions`. Returns
    ``(remaining_discrepancies, resolutions)`` -- every discrepancy not
    resolved this way passes through unchanged,
    ``unmatched_broker_position`` entries always pass through unchanged
    (this path only ever addresses direction 2), and an empty
    ``abandoned_intents`` (the default here, and everywhere this is called
    from) makes this function a complete no-op that returns
    ``discrepancies`` verbatim.

    Only an ``unmatched_open_lot`` whose annotation is still ``is_open``
    is eligible (defensive, mirroring the buy side's own defensive check --
    :func:`reconcile` never produces this discrepancy kind for a closed
    annotation in the first place, see
    :func:`test_closed_lot_annotation_with_no_broker_position_is_not_a_discrepancy`
    in this module's test suite).

    This function makes no DB writes -- like :func:`reconcile`,
    :func:`resolve_abandoned_intent_adoptions`, and gate 11 itself, it only
    detects and reports; persisting each :class:`Resolution`'s
    ``annotation`` (via :meth:`~nightwatch.db.repo.Repo.update_lot_annotation`,
    its existing in-place path) and marking its ``intent`` resolved is the
    caller's job, once one exists (``engine/session.py``, explicitly out of
    scope here).
    """
    annotations_by_id = {a.position_id: a for a in annotations}

    remaining: list[Discrepancy] = []
    resolutions: list[Resolution] = []

    for discrepancy in discrepancies:
        if discrepancy.kind != "unmatched_open_lot":
            remaining.append(discrepancy)
            continue
        annotation = annotations_by_id.get(discrepancy.position_id)
        if annotation is None or not annotation.is_open:
            # Cannot happen given how reconcile() builds this discrepancy
            # kind, but defensive rather than assumed.
            remaining.append(discrepancy)
            continue

        intent = _match_abandoned_close_intent(
            annotation, abandoned_intents=abandoned_intents
        )
        if intent is None:
            remaining.append(discrepancy)
            continue

        resolved_annotation = _build_resolved_annotation(annotation, intent)
        resolutions.append(
            Resolution(
                position_id=annotation.position_id,
                intent=intent,
                annotation=resolved_annotation,
            )
        )

    return remaining, resolutions
