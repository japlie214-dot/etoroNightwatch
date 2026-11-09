"""Typed persistence layer over the SQLite schema (DESIGN §3).

Every table in §3 is reached through a helper here.  Call sites never write
SQL, and every statement in this module is parameterised — there is no string
interpolation of values anywhere, including in the few places where a column
list is built dynamically (those interpolate *column names* drawn from a
hardcoded allowlist, never values).

Connections are opened in WAL mode with foreign keys ON.  WAL matters on the
phone: the webapp reads while the session writes, and WAL is what stops the
dashboard from blocking a trade.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Mapping, Sequence

if TYPE_CHECKING:
    from ..engine.reconcile import Adoption, Resolution

from ..host import Host, get_host
from . import migrations

__all__ = [
    "Bar",
    "Instrument",
    "Session",
    "PreflightCheck",
    "Decision",
    "Intent",
    "Order",
    "Lot",
    "LotAnnotation",
    "PriceWindowFingerprint",
    "Bucket",
    "StrategyState",
    "BudgetRecord",
    "PortfolioSnapshot",
    "SnapshotPosition",
    "Command",
    "Notification",
    "Repo",
    "connect",
    "open_repo",
    "default_db_path",
]

DB_FILENAME = "nightwatch.db"


def utcnow() -> str:
    """Return the current UTC instant as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_db_path(host: Host | None = None) -> Path:
    """Return the database path for the given (or installed) host."""
    return (host or get_host()).data_dir() / DB_FILENAME


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection configured the way this app requires.

    WAL journaling, foreign keys enforced, ``NORMAL`` synchronous (safe under
    WAL), a row factory that yields :class:`sqlite3.Row`, and a busy timeout so
    a concurrent webapp read never raises "database is locked" at the exact
    moment an order is being recorded.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# ---------------------------------------------------------------------------
# Row types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    """One daily bar.  ``is_provisional`` is the flag DESIGN §4.2 turns on."""

    symbol: str
    date: str
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    is_provisional: bool = True
    source: str = "candles"
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class Instrument:
    """Ticker to eToro instrument id, plus the last eligibility snapshot."""

    symbol: str
    instrument_id: int
    name: str | None = None
    exchange: str | None = None
    currency: str | None = None
    allow_open_position: bool | None = None
    min_position_exposure: float | None = None
    min_position_amount: float | None = None
    allow_partial_close: bool | None = None
    eligibility_checked_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class Session:
    """One trading date's run."""

    id: int
    trading_date: str
    state: str
    mode: str
    environment: str
    armed: bool
    preflight_verdict: str | None = None
    preflight_failed_gate: int | None = None
    started_at: str = ""
    preflight_at: str | None = None
    decision_at: str | None = None
    execution_at: str | None = None
    finished_at: str | None = None
    outcome: str | None = None
    error: str | None = None
    next_run_at: str | None = None
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    """One gate's verdict."""

    session_id: int
    gate: int
    name: str
    passed: bool
    detail: str | None = None
    checked_at: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """What the strategy saw and decided; phases share one derived SPY signal."""

    session_id: int
    trading_date: str
    intents: list[dict[str, Any]] = field(default_factory=list)
    cone_z: float | None = None
    spy_close: float | None = None
    spy_ath: float | None = None
    drawdown: float | None = None
    tier: int | None = None
    budget: float | None = None
    order_size: float | None = None
    cash_target: float | None = None
    equity: float | None = None
    cash: float | None = None
    reason: str | None = None
    id: int | None = None
    created_at: str | None = None
    phase: str = "single"


@dataclass(frozen=True, slots=True)
class Intent:
    """A single intended order, persisted before any HTTP call."""

    session_id: int
    request_id: str
    sequence: int
    symbol: str
    side: str
    instrument_id: int | None = None
    book: str | None = None
    amount_usd: float | None = None
    units: float | None = None
    position_id: str | None = None
    lot_id: int | None = None
    bucket_id: str | None = None
    reason: str | None = None
    status: str = "pending"
    id: int | None = None
    created_at: str | None = None
    sent_at: str | None = None
    resolved_at: str | None = None


@dataclass(frozen=True, slots=True)
class Order:
    """What eToro said in response to an intent."""

    intent_id: int
    session_id: int
    request_id: str
    symbol: str
    side: str
    instrument_id: int | None = None
    order_id: str | None = None
    reference_id: str | None = None
    last_status: int | None = None
    terminal_status: int | None = None
    status_history: list[Any] = field(default_factory=list)
    fill_price: float | None = None
    fill_units: float | None = None
    fill_amount_usd: float | None = None
    position_id: str | None = None
    submitted_at: str | None = None
    terminal_at: str | None = None
    error: str | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Lot:
    """One rung of the ladder.  One lot is exactly one eToro position."""

    book: str
    symbol: str
    units: float
    entry_price: float
    cost_basis: float
    entry_date: str
    position_id: str | None = None
    entry_reference_spy: float | None = None
    entry_spy_ath: float | None = None
    entry_spy_close: float | None = None
    entry_spy_drawdown: float | None = None
    entry_tier: int | None = None
    entry_order_id: int | None = None
    bucket_id: str | None = None
    is_open: bool = True
    exit_date: str | None = None
    exit_price: float | None = None
    exit_proceeds: float | None = None
    exit_order_id: int | None = None
    realized_pnl: float | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class LotAnnotation:
    """One row of ``lot_annotations`` (DESIGN_market_data.md §4.4) -- the
    split-invariant strategy sidecar that replaces ``lots`` for anything
    the broker itself owns.

    eToro is authoritative for the financial facts of a position (units,
    entry price, cost basis); this row carries only what a split does not
    touch: which ladder the position belongs to, its stamped SPY entry
    context, and its re-entry bucket if any (§4.1). ``position_id`` is the
    join key back to the broker, not a local autoincrement id.

    ``entry_date``/``entry_amount`` double as the sidecar's re-link key
    (``(instrument_id, entry_date, entry_amount)`` via the broker's
    ``instrumentID``/``openDateTime``/``amount``) -- NOT ``units`` or
    ``entry_price``, because a live 227-position probe (2026-09-04)
    confirmed eToro rescales both of those on a split while preserving
    ``amount`` exactly (§4.6). Stamped once at open, never updated.

    ``origin`` is provenance only, never an access gate (§4.7): ``'app'``
    for a position this app opened and stamped itself, ``'adopted'`` for a
    pre-existing position whose SPY entry context was reconstructed from
    history because nothing was ever stamped for it. Both are fully
    managed once a row exists.

    ``entry_context_source`` (migration 005, board nightwatch/handoff-2026-09-05-evening.md
    §3a; widened by migration 006) is a second, orthogonal provenance
    column, also never an access gate: ``'derived'`` for context that was
    genuinely computed (from measured history for an adopted lot, or a live
    stamped close for an app-created one) versus ``'assumed_out_of_window'``,
    reserved for a lot whose SPY entry context had to be guessed because its
    open date predates the fetched SPY window entirely. ``market.adopt``
    never produces the latter today -- it halts the whole backfill instead
    of guessing (§3a Case 3) -- the value exists so a future override path,
    if one is ever built, has somewhere honest to record that it guessed.

    ``'adopted_from_abandoned_intent'`` (migration 006, DESIGN_session.md
    §3.4, the operator's 2026-09-06 "no forced human-in-the-loop" decision)
    marks a lot written by :func:`nightwatch.engine.reconcile
    .resolve_abandoned_intent_adoptions`'s narrow auto-adopt path: an
    unmatched broker position that matched one of this app's OWN persisted,
    abandoned :class:`Intent` rows by instrument and size. Distinct from
    ``'derived'`` specifically so this lot's provenance is greppable and
    auditable forever after, never silently indistinguishable from a
    normally-recorded one -- and NOT the fabricate-a-lot bug (DESIGN.md
    §1.4 fix #3): that bug invented a lot from nothing, this one is built
    only from a genuine persisted intent record matched against the broker's
    own position.

    ``exit_context_source`` (migration 007, DESIGN_session.md §3.4, closing
    the mirror-image gap left by migration 006) is the CLOSE-side counterpart
    of ``entry_context_source`` -- and deliberately a separate column rather
    than a value squeezed into that one, because ``entry_context_source`` is
    stamped once at open and never updated again (see
    :meth:`Repo.update_lot_annotation`, which does not touch it), while this
    column is only ever written later, at close time. ``None`` (the default,
    and the value on every row written before migration 007) means "closed
    the ordinary way, or still open" -- no special provenance to record.
    ``'resolved_from_abandoned_intent'`` marks a row closed by
    :func:`nightwatch.engine.reconcile.resolve_abandoned_close_intents`'s
    narrow auto-resolve path: an ``unmatched_open_lot`` discrepancy (the
    broker position vanished) that matched one of this app's OWN persisted,
    abandoned close :class:`Intent` rows by position, symbol, and book. The
    mirror image of ``'adopted_from_abandoned_intent'`` for exactly the
    reason that path's own docstring names: a stuck CLOSE resolves via this
    direction, never the buy-side adopt path, and it must be equally
    greppable and auditable forever after -- never a silent, untraceable
    ``is_open=False``.
    """

    position_id: str
    symbol: str
    book: str
    entry_date: str
    entry_amount: float
    origin: str
    entry_context_source: str = "derived"
    entry_reference_spy: float | None = None
    entry_spy_ath: float | None = None
    entry_spy_close: float | None = None
    entry_spy_drawdown: float | None = None
    entry_tier: int | None = None
    bucket_id: str | None = None
    entry_order_id: int | None = None
    exit_order_id: int | None = None
    exit_context_source: str | None = None
    is_open: bool = True
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class PriceWindowFingerprint:
    """One row of ``price_window_fingerprints`` (DESIGN_market_data.md §3.4,
    §7, migration 004) -- built by
    :func:`nightwatch.market.window.compute_fingerprint`. Never a price
    series: a SHA-256 over one session's fetched ``(date, adjClose)`` window,
    kept so a silent upstream revision of history (undetectable once prices
    are never stored) can still be caught by comparing against the previous
    session's fingerprint for the overlapping range.
    """

    trading_date: str
    symbol: str
    window_start: str
    window_end: str
    bar_count: int
    series_sha256: str
    source: str
    source_fetched_at: str


@dataclass(frozen=True, slots=True)
class Bucket:
    """A deferred re-entry bucket."""

    bucket_id: str
    sale_date: str
    sale_spy_close: float
    proceeds: float
    tranche_amount: float
    created_bar: int
    tranches_bought: int = 0
    last_reference_spy: float | None = None
    is_active: bool = True
    expired_at: str | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyState:
    """The singleton strategy state row."""

    bars: int = 0
    ath: float | None = None
    below_streak: int = 0
    recyclable_cash: float = 0.0
    budget_year: int | None = None
    budget: float | None = None
    order_size: float | None = None
    cash_target: float | None = None
    sale_batch_sequence: int = 0
    sale_batch_date: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class BudgetRecord:
    """One row of ``budget_history``."""

    year: int
    budget: float
    order_size: float
    cash_target: float
    source: str
    note: str | None = None
    id: int | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotPosition:
    """One open position inside a portfolio snapshot.

    Corrected 2026-09-04 (DESIGN_market_data.md §4.2, §10.7): a broker
    position carries no ``invested`` key -- ``amount`` (cost basis) is what
    genuinely exists and survives a split unchanged (§4.5), so this column
    was renamed rather than kept alongside a field that never had a source
    (see migration ``002_position_amount.sql``). ``symbol``,
    ``current_price``, and ``unrealized_pnl`` are still legitimate columns
    *here* -- unlike ``etoro.models.Position``, which no longer carries them
    at all -- but the caller building this row must supply them itself
    (``symbol`` via ``Repo.get_instrument``, ``current_price``/
    ``unrealized_pnl`` via ``etoro.models.mark_position``); nothing upstream
    parses them off the broker any more.
    """

    position_id: str
    symbol: str | None = None
    instrument_id: int | None = None
    units: float | None = None
    entry_price: float | None = None
    current_price: float | None = None
    amount: float | None = None
    leverage: int | None = None
    unrealized_pnl: float | None = None
    open_date: str | None = None


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """The account as eToro reported it at one instant."""

    session_id: int | None
    taken_at: str
    equity: float | None = None
    cash: float | None = None
    available_cash: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    positions: list[SnapshotPosition] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Command:
    """One inbound control message, over FCM or Telegram (DESIGN §6.1, §7.2)."""

    channel: str
    sender_id: str
    command: str
    verdict: str
    chat_id: str | None = None
    counter: int | None = None
    reason: str | None = None
    id: int | None = None
    received_at: str | None = None


@dataclass(frozen=True, slots=True)
class Notification:
    """One Telegram send attempt and how it went."""

    event: str
    body: str
    session_id: int | None = None
    trading_date: str | None = None
    chat_id: str | None = None
    status: str = "pending"
    error: str | None = None
    telegram_message_id: str | None = None
    id: int | None = None
    created_at: str | None = None
    sent_at: str | None = None


def _b(value: Any) -> bool | None:
    """Coerce a SQLite 0/1 to bool, preserving NULL."""
    return None if value is None else bool(value)


def _j(value: Any, fallback: Any) -> Any:
    """Decode a JSON column, tolerating NULL."""
    if value in (None, ""):
        return fallback
    return json.loads(value)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class Repo:
    """Typed helpers over the nightwatch database.

    The repo owns its connection and does not commit implicitly on read.  Write
    helpers commit, because on this host a write that is not durable by the time
    the next HTTP call goes out is a write that may as well not exist — the
    ``intents`` table in particular is only an idempotency anchor if it has hit
    the disk before the request leaves (DESIGN §5).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: Path | str | None = None, host: Host | None = None) -> "Repo":
        """Open (and migrate) the database, returning a ready repo."""
        repo = cls(connect(path or default_db_path(host)))
        repo.migrate()
        return repo

    def migrate(self) -> int:
        """Apply any pending migrations. Returns the resulting version."""
        return migrations.migrate(self.conn)

    def schema_version(self) -> int:
        """Return the currently applied schema version."""
        return migrations.current_version(self.conn)

    def close(self) -> None:
        """Close the underlying connection."""
        self.conn.close()

    def __enter__(self) -> "Repo":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- bars --------------------------------------------------------------

    def upsert_bar(self, bar: Bar) -> None:
        """Insert or replace a daily bar."""
        self.conn.execute(
            """
            INSERT INTO bars (symbol, date, "open", high, low, close, volume,
                              is_provisional, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (symbol, date) DO UPDATE SET
                "open" = excluded."open", high = excluded.high,
                low = excluded.low, close = excluded.close,
                volume = excluded.volume,
                is_provisional = excluded.is_provisional,
                source = excluded.source, updated_at = excluded.updated_at
            """,
            (
                bar.symbol, bar.date, bar.open, bar.high, bar.low, bar.close,
                bar.volume, int(bar.is_provisional), bar.source,
                bar.updated_at or utcnow(),
            ),
        )
        self.conn.commit()

    def upsert_bars(self, bars: Iterable[Bar]) -> int:
        """Insert or replace many bars in one transaction. Returns the count."""
        count = 0
        for bar in bars:
            self.upsert_bar(bar)
            count += 1
        return count

    def get_bar(self, symbol: str, date: str) -> Bar | None:
        """Return one bar, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM bars WHERE symbol = ? AND date = ?", (symbol, date)
        ).fetchone()
        return self._bar(row) if row else None

    def get_bars(
        self, symbol: str, limit: int | None = None, settled_only: bool = False
    ) -> list[Bar]:
        """Return bars for a symbol in ascending date order.

        Args:
            symbol: Ticker.
            limit: Return only the most recent ``limit`` bars (still ascending).
            settled_only: Exclude provisional bars.
        """
        clause = "WHERE symbol = ?" + (" AND is_provisional = 0" if settled_only else "")
        if limit is None:
            rows = self.conn.execute(
                f"SELECT * FROM bars {clause} ORDER BY date ASC", (symbol,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"SELECT * FROM bars {clause} ORDER BY date DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
            rows = list(reversed(rows))
        return [self._bar(r) for r in rows]

    def count_bars(self, symbol: str, settled_only: bool = True) -> int:
        """Count stored bars — preflight gate 10 needs at least 265 settled."""
        clause = "WHERE symbol = ?" + (" AND is_provisional = 0" if settled_only else "")
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM bars {clause}", (symbol,)
        ).fetchone()
        return int(row["n"])

    def provisional_bars(self, before_date: str | None = None) -> list[Bar]:
        """Return provisional bars, optionally only those before a date.

        Used by step [2] of the session (settle yesterday) and by preflight
        gate 10, which fails if anything is still provisional afterwards.
        """
        if before_date is None:
            rows = self.conn.execute(
                "SELECT * FROM bars WHERE is_provisional = 1 ORDER BY symbol, date"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM bars WHERE is_provisional = 1 AND date < ? "
                "ORDER BY symbol, date",
                (before_date,),
            ).fetchall()
        return [self._bar(r) for r in rows]

    def settle_bar(
        self,
        symbol: str,
        date: str,
        close: float,
        open_: float | None = None,
        high: float | None = None,
        low: float | None = None,
        volume: float | None = None,
        source: str = "candles",
    ) -> None:
        """Overwrite a provisional bar with its official close (DESIGN §4.2)."""
        self.conn.execute(
            """
            UPDATE bars
               SET "open" = COALESCE(?, "open"), high = COALESCE(?, high),
                   low = COALESCE(?, low), close = ?, volume = COALESCE(?, volume),
                   is_provisional = 0, source = ?, updated_at = ?
             WHERE symbol = ? AND date = ?
            """,
            (open_, high, low, close, volume, source, utcnow(), symbol, date),
        )
        self.conn.commit()

    @staticmethod
    def _bar(row: sqlite3.Row) -> Bar:
        return Bar(
            symbol=row["symbol"], date=row["date"], close=row["close"],
            open=row["open"], high=row["high"], low=row["low"],
            volume=row["volume"], is_provisional=bool(row["is_provisional"]),
            source=row["source"], updated_at=row["updated_at"],
        )

    # -- instruments -------------------------------------------------------

    def upsert_instrument(self, instrument: Instrument) -> None:
        """Insert or update an instrument and its eligibility snapshot."""
        self.conn.execute(
            """
            INSERT INTO instruments (symbol, instrument_id, name, exchange, currency,
                                     allow_open_position, min_position_exposure,
                                     min_position_amount, allow_partial_close,
                                     eligibility_checked_at, metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (symbol) DO UPDATE SET
                instrument_id = excluded.instrument_id, name = excluded.name,
                exchange = excluded.exchange, currency = excluded.currency,
                allow_open_position = excluded.allow_open_position,
                min_position_exposure = excluded.min_position_exposure,
                min_position_amount = excluded.min_position_amount,
                allow_partial_close = excluded.allow_partial_close,
                eligibility_checked_at = excluded.eligibility_checked_at,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                instrument.symbol, instrument.instrument_id, instrument.name,
                instrument.exchange, instrument.currency,
                None if instrument.allow_open_position is None
                else int(instrument.allow_open_position),
                instrument.min_position_exposure, instrument.min_position_amount,
                None if instrument.allow_partial_close is None
                else int(instrument.allow_partial_close),
                instrument.eligibility_checked_at,
                json.dumps(instrument.metadata),
                instrument.updated_at or utcnow(),
            ),
        )
        self.conn.commit()

    def get_instrument(self, symbol: str) -> Instrument | None:
        """Return a cached instrument by ticker, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM instruments WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None:
            return None
        return Instrument(
            symbol=row["symbol"], instrument_id=row["instrument_id"],
            name=row["name"], exchange=row["exchange"], currency=row["currency"],
            allow_open_position=_b(row["allow_open_position"]),
            min_position_exposure=row["min_position_exposure"],
            min_position_amount=row["min_position_amount"],
            allow_partial_close=_b(row["allow_partial_close"]),
            eligibility_checked_at=row["eligibility_checked_at"],
            metadata=_j(row["metadata_json"], {}),
            updated_at=row["updated_at"],
        )

    def get_instrument_by_id(self, instrument_id: int) -> Instrument | None:
        """Return a cached instrument by eToro ``instrumentID``, or ``None``.

        The join a broker position must use to get a symbol at all
        (DESIGN_market_data.md §4.2): a position carries no symbol string,
        only ``instrumentID``, so this is the only correct way to resolve
        one -- never a string comparison.
        """
        row = self.conn.execute(
            "SELECT * FROM instruments WHERE instrument_id = ?", (instrument_id,)
        ).fetchone()
        if row is None:
            return None
        return Instrument(
            symbol=row["symbol"], instrument_id=row["instrument_id"],
            name=row["name"], exchange=row["exchange"], currency=row["currency"],
            allow_open_position=_b(row["allow_open_position"]),
            min_position_exposure=row["min_position_exposure"],
            min_position_amount=row["min_position_amount"],
            allow_partial_close=_b(row["allow_partial_close"]),
            eligibility_checked_at=row["eligibility_checked_at"],
            metadata=_j(row["metadata_json"], {}),
            updated_at=row["updated_at"],
        )

    def list_instruments(self) -> list[Instrument]:
        """Return every cached instrument, ticker order."""
        rows = self.conn.execute(
            "SELECT * FROM instruments ORDER BY symbol"
        ).fetchall()
        return [
            Instrument(
                symbol=r["symbol"], instrument_id=r["instrument_id"],
                name=r["name"], exchange=r["exchange"], currency=r["currency"],
                allow_open_position=_b(r["allow_open_position"]),
                min_position_exposure=r["min_position_exposure"],
                min_position_amount=r["min_position_amount"],
                allow_partial_close=_b(r["allow_partial_close"]),
                eligibility_checked_at=r["eligibility_checked_at"],
                metadata=_j(r["metadata_json"], {}),
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # -- sessions ----------------------------------------------------------

    def create_session(
        self,
        trading_date: str,
        mode: str,
        environment: str,
        armed: bool,
        state: str = "started",
    ) -> Session:
        """Start a session for a trading date.

        Raises:
            sqlite3.IntegrityError: if a session already exists for that date.
                That is the double-run guard from DESIGN §5 doing its job — the
                safety-net alarm relies on it, so callers should check
                :meth:`get_session_by_date` first rather than catching this.
        """
        now = utcnow()
        cur = self.conn.execute(
            """
            INSERT INTO sessions (trading_date, state, mode, environment, armed,
                                  started_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (trading_date, state, mode, environment, int(armed), now, now),
        )
        self.conn.commit()
        session = self.get_session(int(cur.lastrowid))
        assert session is not None
        return session

    def get_session(self, session_id: int) -> Session | None:
        """Return a session by id."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return self._session(row) if row else None

    def get_session_by_date(self, trading_date: str) -> Session | None:
        """Return the session for a trading date — the "already ran today" flag."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE trading_date = ?", (trading_date,)
        ).fetchone()
        return self._session(row) if row else None

    def latest_session(self) -> Session | None:
        """Return the most recent session by trading date."""
        row = self.conn.execute(
            "SELECT * FROM sessions ORDER BY trading_date DESC, id DESC LIMIT 1"
        ).fetchone()
        return self._session(row) if row else None

    def unfinished_sessions(self) -> list[Session]:
        """Return sessions that never reached a terminal state (gate 5)."""
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE state NOT IN ('finished', 'aborted', 'failed') "
            "ORDER BY trading_date"
        ).fetchall()
        return [self._session(r) for r in rows]

    #: Session columns a caller is allowed to update.  Used to build the SET
    #: clause; values are always bound, never interpolated.
    _SESSION_UPDATABLE = frozenset(
        {
            "state", "preflight_verdict", "preflight_failed_gate", "preflight_at",
            "decision_at", "execution_at", "finished_at", "outcome", "error",
            "next_run_at",
        }
    )

    def update_session(self, session_id: int, **fields: Any) -> None:
        """Update named session columns.

        Only the columns in :data:`_SESSION_UPDATABLE` may be set; anything else
        raises.  Column names come from that frozen allowlist, values are bound.
        """
        unknown = set(fields) - self._SESSION_UPDATABLE
        if unknown:
            raise ValueError(f"not updatable on sessions: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(
            f"UPDATE sessions SET {assignments} WHERE id = ?",
            (*fields.values(), session_id),
        )
        self.conn.commit()

    def finish_session(
        self, session_id: int, state: str, outcome: str | None = None,
        error: str | None = None, next_run_at: str | None = None,
    ) -> None:
        """Mark a session terminal and stamp ``finished_at``."""
        self.update_session(
            session_id, state=state, outcome=outcome, error=error,
            next_run_at=next_run_at, finished_at=utcnow(),
        )

    def finalize_crashed(self, session_id: int, outcome: str) -> None:
        """Finish a crashed session without changing its intents or trading date."""
        if outcome not in ("crashed_recovered", "crashed_unresolved"):
            raise ValueError("invalid crash outcome")
        self.finish_session(session_id, state="finished", outcome=outcome)

    @staticmethod
    def _session(row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"], trading_date=row["trading_date"], state=row["state"],
            mode=row["mode"], environment=row["environment"],
            armed=bool(row["armed"]),
            preflight_verdict=row["preflight_verdict"],
            preflight_failed_gate=row["preflight_failed_gate"],
            started_at=row["started_at"], preflight_at=row["preflight_at"],
            decision_at=row["decision_at"], execution_at=row["execution_at"],
            finished_at=row["finished_at"], outcome=row["outcome"],
            error=row["error"], next_run_at=row["next_run_at"],
            created_at=row["created_at"],
        )

    # -- preflight ---------------------------------------------------------

    def record_preflight_check(self, check: PreflightCheck) -> None:
        """Record (or overwrite) one gate's verdict for a session."""
        self.conn.execute(
            """
            INSERT INTO preflight_checks (session_id, gate, name, passed, detail,
                                          checked_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (session_id, gate) DO UPDATE SET
                name = excluded.name, passed = excluded.passed,
                detail = excluded.detail, checked_at = excluded.checked_at
            """,
            (
                check.session_id, check.gate, check.name, int(check.passed),
                check.detail, check.checked_at or utcnow(),
            ),
        )
        self.conn.commit()

    def get_preflight_checks(self, session_id: int) -> list[PreflightCheck]:
        """Return a session's gate results in gate order."""
        rows = self.conn.execute(
            "SELECT * FROM preflight_checks WHERE session_id = ? ORDER BY gate",
            (session_id,),
        ).fetchall()
        return [
            PreflightCheck(
                session_id=r["session_id"], gate=r["gate"], name=r["name"],
                passed=bool(r["passed"]), detail=r["detail"],
                checked_at=r["checked_at"],
            )
            for r in rows
        ]

    # -- decisions ---------------------------------------------------------

    def insert_decision(self, decision: Decision) -> int:
        """Persist the session's decision. Returns its row id."""
        cur = self.conn.execute(
            """
            INSERT INTO decisions (session_id, trading_date, cone_z, spy_close,
                                   spy_ath, drawdown, tier, budget, order_size,
                                   cash_target, equity, cash, intents_json, reason,
                                   created_at, phase)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.session_id, decision.trading_date, decision.cone_z,
                decision.spy_close, decision.spy_ath, decision.drawdown,
                decision.tier, decision.budget, decision.order_size,
                decision.cash_target, decision.equity, decision.cash,
                json.dumps(decision.intents), decision.reason,
                decision.created_at or utcnow(), decision.phase,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_decision(self, session_id: int, phase: str | None = None) -> Decision | None:
        """Return a phase, or the authoritative decision (B, then single, then A)."""
        if phase is None:
            row = self.conn.execute(
                "SELECT * FROM decisions WHERE session_id = ? "
                "ORDER BY CASE phase WHEN 'B' THEN 0 WHEN 'single' THEN 1 ELSE 2 END "
                "LIMIT 1", (session_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM decisions WHERE session_id = ? AND phase = ?",
                (session_id, phase),
            ).fetchone()
        if row is None:
            return None
        return Decision(
            id=row["id"], session_id=row["session_id"],
            trading_date=row["trading_date"], cone_z=row["cone_z"],
            spy_close=row["spy_close"], spy_ath=row["spy_ath"],
            drawdown=row["drawdown"], tier=row["tier"], budget=row["budget"],
            order_size=row["order_size"], cash_target=row["cash_target"],
            equity=row["equity"], cash=row["cash"],
            intents=_j(row["intents_json"], []), reason=row["reason"],
            created_at=row["created_at"], phase=row["phase"],
        )

    # -- intents -----------------------------------------------------------

    def insert_intent(self, intent: Intent) -> int:
        """Persist an intent and commit it **before** any HTTP call is made.

        This is the idempotency anchor of DESIGN §5: ``request_id`` is the
        ``x-request-id`` UUID we will send and is also eToro's ``referenceId``.
        If the response is lost, recovery looks the order up by that id rather
        than re-sending, which is the difference between a lost response and a
        double buy.  The commit here is not optional.
        """
        cur = self.conn.execute(
            """
            INSERT INTO intents (session_id, request_id, sequence, symbol,
                                 instrument_id, side, book, amount_usd, units,
                                 position_id, lot_id, bucket_id, reason, status,
                                 created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent.session_id, intent.request_id, intent.sequence,
                intent.symbol, intent.instrument_id, intent.side, intent.book,
                intent.amount_usd, intent.units, intent.position_id,
                intent.lot_id, intent.bucket_id, intent.reason, intent.status,
                intent.created_at or utcnow(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_intent_by_request_id(self, request_id: str) -> Intent | None:
        """Look an intent up by its ``x-request-id`` — the recovery path."""
        row = self.conn.execute(
            "SELECT * FROM intents WHERE request_id = ?", (request_id,)
        ).fetchone()
        return self._intent(row) if row else None

    def get_intents(self, session_id: int) -> list[Intent]:
        """Return a session's intents in submission order."""
        rows = self.conn.execute(
            "SELECT * FROM intents WHERE session_id = ? ORDER BY sequence",
            (session_id,),
        ).fetchall()
        return [self._intent(r) for r in rows]

    def reserved_buy_usd(self, trading_date: str) -> float:
        """Buy notional reserved for a date; only rejection frees the cap."""
        row = self.conn.execute(
            """
            SELECT SUM(intents.amount_usd)
              FROM intents JOIN sessions ON intents.session_id = sessions.id
             WHERE sessions.trading_date = ? AND intents.side = 'buy'
               AND intents.status IN ('pending', 'sent', 'filled_unannotated', 'filled', 'recovered', 'abandoned')
            """,
            (trading_date,),
        ).fetchone()
        return float(row[0] or 0.0)

    def unresolved_intents(self, session_id: int | None = None) -> list[Intent]:
        """Orders without a definitive outcome, including abandoned uncertainty."""
        rows = self.conn.execute(
            "SELECT * FROM intents WHERE status IN ('pending', 'sent', 'filled_unannotated', 'unknown', 'abandoned') "
            + ("AND session_id = ? " if session_id is not None else "")
            + "ORDER BY session_id, sequence",
            (session_id,) if session_id is not None else (),
        ).fetchall()
        return [self._intent(r) for r in rows]

    def pending_intents(self) -> list[Intent]:
        """Return intents that were written but never resolved (crash recovery)."""
        rows = self.conn.execute(
            "SELECT * FROM intents WHERE status IN ('pending', 'sent') "
            "ORDER BY session_id, sequence"
        ).fetchall()
        return [self._intent(r) for r in rows]

    def unresolved_buy_intents(self) -> list[tuple[Intent, str]]:
        """Unresolved buys, including fills awaiting their durable annotation."""
        rows = self.conn.execute(
            """
            SELECT i.*, s.trading_date AS trading_date
              FROM intents i JOIN sessions s ON i.session_id = s.id
             WHERE i.side = 'buy' AND i.status IN ('pending', 'sent', 'filled_unannotated')
             ORDER BY i.id
            """
        ).fetchall()
        return [(self._intent(r), str(r["trading_date"])) for r in rows]

    def sessions_since(self, trading_date: str) -> int:
        """Count sessions whose trading date is strictly later than ``trading_date``."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE trading_date > ?", (trading_date,)
        ).fetchone()
        return int(row[0])

    def pending_tp_clears(self) -> list[dict]:
        """Take-profit clear attempts still pending or sent, in id order."""
        rows = self.conn.execute(
            "SELECT * FROM tp_clears WHERE status IN ('pending', 'sent') ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def abandoned_intents(self) -> list[Intent]:
        """Return every intent marked ``'abandoned'`` -- the candidate set
        for :func:`nightwatch.engine.reconcile.resolve_abandoned_intent_adoptions`
        (DESIGN_session.md §3.4, the operator's 2026-09-06 "no forced
        human-in-the-loop" decision).

        A persistently non-terminal order stops being polled after N
        sessions (config-driven, :class:`~nightwatch.config.ReconciliationConfig`)
        and is marked ``'abandoned'`` rather than escalated to a human --
        the session orchestrator that flips an intent to this status (the
        polling loop's own attachment point) is not built yet; this method
        only reads what that future write path produces. If an abandoned
        order later fills, gate 11 sees an unmatched broker position on the
        next session and resolves it against this list, never against a
        guess.
        """
        rows = self.conn.execute(
            "SELECT * FROM intents WHERE status = 'abandoned' "
            "ORDER BY session_id, sequence"
        ).fetchall()
        return [self._intent(r) for r in rows]

    def set_intent_status(
        self, intent_id: int, status: str, resolved: bool = False
    ) -> None:
        """Advance an intent's lifecycle status."""
        now = utcnow()
        self.conn.execute(
            """
            UPDATE intents
               SET status = ?,
                   sent_at = CASE WHEN ? = 'sent' AND sent_at IS NULL THEN ?
                                  ELSE sent_at END,
                   resolved_at = CASE WHEN ? THEN ? ELSE resolved_at END
             WHERE id = ?
            """,
            (status, status, now, int(resolved), now, intent_id),
        )
        self.conn.commit()

    @staticmethod
    def _intent(row: sqlite3.Row) -> Intent:
        return Intent(
            id=row["id"], session_id=row["session_id"],
            request_id=row["request_id"], sequence=row["sequence"],
            symbol=row["symbol"], instrument_id=row["instrument_id"],
            side=row["side"], book=row["book"], amount_usd=row["amount_usd"],
            units=row["units"], position_id=row["position_id"],
            lot_id=row["lot_id"], bucket_id=row["bucket_id"],
            reason=row["reason"], status=row["status"],
            created_at=row["created_at"], sent_at=row["sent_at"],
            resolved_at=row["resolved_at"],
        )

    # -- orders ------------------------------------------------------------

    def insert_order(self, order: Order) -> int:
        """Record eToro's acknowledgement of an intent. Returns the row id."""
        now = utcnow()
        cur = self.conn.execute(
            """
            INSERT INTO orders (intent_id, session_id, request_id, order_id,
                                reference_id, symbol, instrument_id, side,
                                last_status, terminal_status, status_history_json,
                                fill_price, fill_units, fill_amount_usd,
                                position_id, submitted_at, terminal_at, error,
                                created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.intent_id, order.session_id, order.request_id,
                order.order_id, order.reference_id or order.request_id,
                order.symbol, order.instrument_id, order.side,
                order.last_status, order.terminal_status,
                json.dumps(order.status_history), order.fill_price,
                order.fill_units, order.fill_amount_usd, order.position_id,
                order.submitted_at or now, order.terminal_at, order.error,
                now, now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_order(self, order_row_id: int) -> Order | None:
        """Return an order by its local row id."""
        row = self.conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_row_id,)
        ).fetchone()
        return self._order(row) if row else None

    def get_order_by_reference_id(self, reference_id: str) -> Order | None:
        """Return an order by eToro ``referenceId`` — the crash-recovery lookup."""
        row = self.conn.execute(
            "SELECT * FROM orders WHERE reference_id = ?", (reference_id,)
        ).fetchone()
        return self._order(row) if row else None

    def update_order_status(
        self,
        order_row_id: int,
        last_status: int,
        status_history: Sequence[Any] | None = None,
        terminal_status: int | None = None,
        fill_price: float | None = None,
        fill_units: float | None = None,
        fill_amount_usd: float | None = None,
        position_id: str | None = None,
        order_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Record the result of one poll of ``orders:lookup``.

        Every value column uses ``COALESCE(?, column)`` so a poll that returns
        only a status cannot erase a fill price recorded by an earlier one.
        """
        now = utcnow()
        self.conn.execute(
            """
            UPDATE orders
               SET last_status = ?,
                   terminal_status = COALESCE(?, terminal_status),
                   terminal_at = CASE WHEN ? IS NOT NULL AND terminal_at IS NULL
                                      THEN ? ELSE terminal_at END,
                   status_history_json = COALESCE(?, status_history_json),
                   fill_price = COALESCE(?, fill_price),
                   fill_units = COALESCE(?, fill_units),
                   fill_amount_usd = COALESCE(?, fill_amount_usd),
                   position_id = COALESCE(?, position_id),
                   order_id = COALESCE(?, order_id),
                   error = COALESCE(?, error),
                   updated_at = ?
             WHERE id = ?
            """,
            (
                last_status, terminal_status, terminal_status, now,
                None if status_history is None else json.dumps(list(status_history)),
                fill_price, fill_units, fill_amount_usd, position_id, order_id,
                error, now, order_row_id,
            ),
        )
        self.conn.commit()

    @staticmethod
    def _order(row: sqlite3.Row) -> Order:
        return Order(
            id=row["id"], intent_id=row["intent_id"], session_id=row["session_id"],
            request_id=row["request_id"], order_id=row["order_id"],
            reference_id=row["reference_id"], symbol=row["symbol"],
            instrument_id=row["instrument_id"], side=row["side"],
            last_status=row["last_status"], terminal_status=row["terminal_status"],
            status_history=_j(row["status_history_json"], []),
            fill_price=row["fill_price"], fill_units=row["fill_units"],
            fill_amount_usd=row["fill_amount_usd"], position_id=row["position_id"],
            submitted_at=row["submitted_at"], terminal_at=row["terminal_at"],
            error=row["error"],
        )

    # -- SCHG close legs ---------------------------------------------------

    def begin_schg_close_leg(
        self, intent: Intent, *, position_id: str, units_before: float,
        units_deducted: float | None,
    ) -> tuple[int, int]:
        """Commit the intent, pending evidence and sent status atomically before send."""
        now = utcnow()
        with self.conn:
            self.conn.execute("BEGIN")
            cur = self.conn.execute(
                "INSERT INTO intents (session_id, request_id, sequence, symbol, side, "
                "instrument_id, units, position_id, reason, status, created_at, sent_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'sent', ?, ?)",
                (intent.session_id, intent.request_id, intent.sequence, intent.symbol,
                 intent.side, intent.instrument_id, intent.units, position_id,
                 intent.reason, intent.created_at or now, now),
            )
            intent_id = int(cur.lastrowid)
            cur = self.conn.execute(
                "INSERT INTO partial_close_effects (intent_id, position_id, units_before, "
                "units_deducted, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                (intent_id, position_id, units_before, units_deducted, now),
            )
            ledger_id = int(cur.lastrowid)
        return intent_id, ledger_id

    def insert_partial_close(
        self, intent_id: int, *, position_id: str, units_before: float,
        units_deducted: float | None,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO partial_close_effects (intent_id, position_id, units_before, "
                "units_deducted, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                (intent_id, position_id, units_before, units_deducted, utcnow()),
            )
        return int(cur.lastrowid)

    def set_partial_close_order(self, ledger_id: int, order_row_id: int, order_id: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE partial_close_effects SET order_row_id = ?, order_id = ? WHERE id = ?",
                (order_row_id, order_id, ledger_id),
            )

    def _update_partial_close(
        self, ledger_id: int, *, status: str, units_after: float | None = None,
        slice_position_id: str | None = None, close_price: float | None = None,
        proceeds: float | None = None, raw: dict | None = None,
    ) -> int:
        row = self.conn.execute(
            "SELECT intent_id, status FROM partial_close_effects WHERE id = ?", (ledger_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"no partial close id={ledger_id}")
        if row["status"] in ("confirmed", "rejected"):
            raise ValueError("partial close is final")
        self.conn.execute(
            "UPDATE partial_close_effects SET status = ?, units_after = ?, "
            "slice_position_id = ?, close_price = ?, proceeds = ?, raw_json = ?, "
            "confirmed_at = ? WHERE id = ?",
            (status, units_after, slice_position_id, close_price, proceeds,
             json.dumps(raw or {}), utcnow() if status == "confirmed" else None, ledger_id),
        )
        return row["intent_id"]

    def update_partial_close(self, ledger_id: int, *, status: str, **fields: Any) -> None:
        with self.conn:
            self._update_partial_close(ledger_id, status=status, **fields)

    def finish_schg_close_leg(
        self, ledger_id: int, *, status: str, intent_status: str, resolved: bool,
        **fields: Any,
    ) -> None:
        expected = {"confirmed": ("filled", True), "rejected": ("rejected", True),
                    "unresolved": ("sent", False), "unverified": ("sent", False)}
        if expected.get(status) != (intent_status, resolved):
            raise ValueError("inconsistent SCHG close lifecycle")
        with self.conn:
            self.conn.execute("BEGIN")
            intent_id = self._update_partial_close(ledger_id, status=status, **fields)
            self.conn.execute(
                "UPDATE intents SET status = ?, resolved_at = ? WHERE id = ?",
                (intent_status, utcnow() if resolved else None, intent_id),
            )

    def pending_partial_closes(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT p.* FROM partial_close_effects p JOIN intents i ON i.id = p.intent_id "
            "WHERE p.status IN ('pending', 'unresolved', 'unverified') "
            "AND i.resolved_at IS NULL ORDER BY p.id"
        ).fetchall()
        return [dict(row) for row in rows]

    def confirmed_partial_closes(self, position_ids: Iterable[str]) -> list[dict]:
        ids = tuple(position_ids)
        if not ids:
            return []
        rows = self.conn.execute(
            "SELECT * FROM partial_close_effects WHERE status = 'confirmed' "
            "AND units_deducted IS NOT NULL AND position_id IN ("
            + ",".join("?" for _ in ids) + ") ORDER BY id", ids,
        ).fetchall()
        return [dict(row) for row in rows]

    def repair_confirmed_schg_closes(self) -> list[int]:
        """Repair legacy split writes without re-polling or changing final evidence."""
        with self.conn:
            rows = self.conn.execute(
                "SELECT i.id FROM intents i JOIN partial_close_effects p ON p.intent_id = i.id "
                "WHERE i.symbol = 'SCHG' AND i.side = 'close' AND p.status = 'confirmed' "
                "AND (i.resolved_at IS NULL OR i.status != 'filled')"
            ).fetchall()
            ids = [row["id"] for row in rows]
            self.conn.executemany(
                "UPDATE intents SET status = 'filled', resolved_at = ? WHERE id = ?",
                [(utcnow(), intent_id) for intent_id in ids],
            )
        return ids

    # -- close observations ------------------------------------------------

    def record_close_observation(
        self, *, intent_id: int, order_row_id: int | None, order_id: str | None,
        position_id: str, observed_status: str, units_closed: float | None = None,
        close_price: float | None = None, closed_at: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> int:
        """Commit app-owned close evidence; only confirmed_full_close is applicable.

        T036 supplies that status only after FILLED plus a strict portfolio read
        showing the position absent. Other evidence, including partial closes,
        may be recorded but cannot close an annotation.
        """
        cur = self.conn.execute(
            """
            INSERT INTO close_observations (
                intent_id, order_row_id, order_id, position_id, observed_status,
                units_closed, close_price, closed_at, raw_json, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (intent_id, order_row_id, order_id, position_id, observed_status,
             units_closed, close_price, closed_at, json.dumps(raw or {}), utcnow()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def apply_confirmed_close(self, observation_id: int) -> bool:
        """Atomically close a sidecar once, assuming a single session writer.

        Only is_open, exit_order_id and updated_at change on the annotation;
        entry fields and book are untouched. The partial unique index enforces
        one applied observation per position.
        """
        with self.conn:
            self.conn.execute("BEGIN")
            observation = self.conn.execute(
                "SELECT * FROM close_observations WHERE id = ?", (observation_id,),
            ).fetchone()
            if observation is None:
                raise LookupError(f"no close observation id={observation_id}")
            if observation["observed_status"] != "confirmed_full_close":
                raise ValueError("apply_confirmed_close requires confirmed_full_close evidence")
            position_id = observation["position_id"]
            applied = self.conn.execute(
                "SELECT 1 FROM close_observations "
                "WHERE position_id = ? AND applied_at IS NOT NULL", (position_id,),
            ).fetchone()
            if applied:
                return False
            now = utcnow()
            cur = self.conn.execute(
                "UPDATE lot_annotations SET is_open = 0, exit_order_id = ?, updated_at = ? "
                "WHERE position_id = ? AND is_open = 1",
                (observation["order_row_id"], now, position_id),
            )
            if cur.rowcount == 0:
                raise LookupError(f"no open lot annotation for position_id={position_id!r}")
            self.conn.execute(
                "UPDATE close_observations SET applied_at = ? WHERE id = ?",
                (now, observation_id),
            )
        return True

    def list_close_observations(self, position_id: str | None = None) -> list[dict]:
        """Return all close evidence, or one position's evidence, in id order."""
        if position_id is None:
            rows = self.conn.execute("SELECT * FROM close_observations ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM close_observations WHERE position_id = ? ORDER BY id",
                (position_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def unapplied_confirmed_closes(self) -> list[dict]:
        """Return full-close evidence awaiting application, in recovery order."""
        rows = self.conn.execute(
            "SELECT * FROM close_observations "
            "WHERE observed_status = 'confirmed_full_close' AND applied_at IS NULL ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_sale_effects(self, session_id: int, effects: Sequence[dict]) -> None:
        """Commit retryable effects before sending; never replace confirmed proceeds."""
        with self.conn:
            self.conn.execute("BEGIN")
            for effect in effects:
                existing = self.conn.execute(
                    "SELECT s.applied_at, EXISTS (SELECT 1 FROM close_observations c "
                    "WHERE c.position_id = s.position_id "
                    "AND c.observed_status = 'confirmed_full_close') AS confirmed "
                    "FROM sale_effects s WHERE s.position_id = ?", (effect["position_id"],),
                ).fetchone()
                if existing is not None:
                    if existing["applied_at"] is not None:
                        raise ValueError(f"sale effect already applied for position {effect['position_id']}")
                    if existing["confirmed"]:
                        raise ValueError(f"confirmed sale effect must be applied before retry for position {effect['position_id']}")
            self.conn.executemany(
                "INSERT INTO sale_effects (session_id, position_id, gross_proceeds_cents, "
                "classification, bucket_json, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(position_id) DO UPDATE SET session_id = excluded.session_id, "
                "gross_proceeds_cents = excluded.gross_proceeds_cents, "
                "classification = excluded.classification, bucket_json = excluded.bucket_json, "
                "created_at = excluded.created_at WHERE sale_effects.applied_at IS NULL",
                [(session_id, e["position_id"], e["gross_proceeds_cents"],
                  e["classification"], json.dumps(e["bucket"]) if e["bucket"] else None,
                  utcnow()) for e in effects],
            )

    def unapplied_sale_effects(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT position_id FROM sale_effects s WHERE applied_at IS NULL AND EXISTS "
            "(SELECT 1 FROM close_observations c WHERE c.position_id = s.position_id "
            "AND c.observed_status = 'confirmed_full_close') ORDER BY id"
        ).fetchall()
        return [r["position_id"] for r in rows]

    def apply_sale_effect(self, position_id: str) -> bool:
        """Apply confirmed proceeds once; a batch bucket holds confirmed cents only."""
        from ..strategy.ladder import _floor_cents

        with self.conn:
            self.conn.execute("BEGIN")
            effect = self.conn.execute(
                "SELECT * FROM sale_effects s WHERE position_id = ? AND applied_at IS NULL "
                "AND EXISTS (SELECT 1 FROM close_observations c WHERE "
                "c.position_id = s.position_id AND c.observed_status = 'confirmed_full_close')",
                (position_id,),
            ).fetchone()
            if effect is None:
                return False
            now = utcnow()
            if effect["classification"] == "recyclable":
                self.conn.execute(
                    "INSERT OR IGNORE INTO strategy_state (id, updated_at) VALUES (1, ?)", (now,),
                )
                self.conn.execute(
                    "UPDATE strategy_state SET recyclable_cash = ROUND(recyclable_cash + ?, 2), "
                    "updated_at = ? WHERE id = 1",
                    (effect["gross_proceeds_cents"] / 100, now),
                )
            else:
                # One payload per session batch; later confirmations enlarge that same bucket.
                payload_row = self.conn.execute(
                    "SELECT bucket_json FROM sale_effects WHERE session_id = ? "
                    "AND classification = 'bucket' AND bucket_json IS NOT NULL",
                    (effect["session_id"],),
                ).fetchone()
                if payload_row is None:
                    raise LookupError("sale batch has no bucket payload")
                payload = json.loads(payload_row["bucket_json"])
                bucket = payload["bucket"]
                cents = self.conn.execute(
                    "SELECT SUM(gross_proceeds_cents) FROM sale_effects s WHERE session_id = ? "
                    "AND classification = 'bucket' AND EXISTS (SELECT 1 FROM close_observations c "
                    "WHERE c.position_id = s.position_id AND c.observed_status = 'confirmed_full_close')",
                    (effect["session_id"],),
                ).fetchone()[0]
                proceeds = cents / 100
                self.conn.execute(
                    "INSERT INTO buckets (bucket_id, sale_date, sale_spy_close, proceeds, "
                    "tranche_amount, tranches_bought, last_reference_spy, created_bar, is_active, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, ?, 1, ?) ON CONFLICT(bucket_id) DO UPDATE SET "
                    "proceeds = excluded.proceeds, tranche_amount = excluded.tranche_amount",
                    (bucket["bucket_id"], bucket["sale_date"], bucket["sale_spy_close"], proceeds,
                     _floor_cents(proceeds / payload["tranches"]), bucket["last_reference_spy"],
                     bucket["created_bar"], now),
                )
            self.conn.execute(
                "UPDATE sale_effects SET applied_at = ? WHERE id = ?", (now, effect["id"]),
            )
        return True

    def unapplied_bucket_fills(self) -> list[int]:
        rows = self.conn.execute(
            "SELECT i.id FROM intents i WHERE i.status = 'filled' AND i.symbol = 'UPRO' "
            "AND i.side = 'buy' AND i.book = 'reentry' AND i.bucket_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM bucket_fill_applications a WHERE a.intent_id = i.id) "
            "ORDER BY i.id"
        ).fetchall()
        return [r["id"] for r in rows]

    def apply_bucket_fill(self, intent_id: int) -> bool:
        """Advance a directly filled or recovered reentry tranche exactly once."""
        with self.conn:
            self.conn.execute("BEGIN")
            intent = self.conn.execute(
                "SELECT i.*, d.spy_close FROM intents i LEFT JOIN decisions d "
                "ON d.session_id = i.session_id AND d.phase = 'single' "
                "WHERE i.id = ? AND i.status = 'filled' AND i.symbol = 'UPRO' "
                "AND i.side = 'buy' AND i.book = 'reentry' AND i.bucket_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM bucket_fill_applications a WHERE a.intent_id = i.id)",
                (intent_id,),
            ).fetchone()
            if intent is None:
                return False
            if intent["spy_close"] is None:
                raise LookupError(f"reentry intent {intent_id} has no decision SPY reference")
            cur = self.conn.execute(
                "UPDATE buckets SET tranches_bought = tranches_bought + 1, last_reference_spy = ?, "
                "is_active = CASE WHEN tranches_bought + 1 >= 3 THEN 0 ELSE is_active END "
                "WHERE bucket_id = ?",
                (intent["spy_close"], intent["bucket_id"]),
            )
            if not cur.rowcount:
                raise LookupError(f"reentry intent {intent_id} has no bucket")
            self.conn.execute(
                "INSERT INTO bucket_fill_applications (intent_id, bucket_id, applied_at) VALUES (?, ?, ?)",
                (intent_id, intent["bucket_id"], utcnow()),
            )
        return True

    # -- lots --------------------------------------------------------------

    def next_intent_sequence(self, session_id: int) -> int:
        """Return the next free ``sequence`` for a session's intents."""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM intents WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0])

    def insert_tp_clear(
        self, order_row_id: int, position_id: str, request_id: str, attempt: int
    ) -> int:
        """Persist one take-profit clear attempt (status ``pending``) before its PATCH."""
        cur = self.conn.execute(
            """
            INSERT INTO tp_clears (order_id, position_id, request_id, attempt,
                                   status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (order_row_id, position_id, request_id, attempt, utcnow()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_tp_clear_status(
        self, tp_clear_id: int, status: str, detail: str | None = None
    ) -> None:
        """Advance a take-profit clear attempt; ``cleared``/``failed`` stamp ``resolved_at``."""
        resolved = utcnow() if status in ("cleared", "failed") else None
        self.conn.execute(
            """
            UPDATE tp_clears
               SET status = ?,
                   detail = COALESCE(?, detail),
                   resolved_at = COALESCE(?, resolved_at)
             WHERE id = ?
            """,
            (status, detail, resolved, tp_clear_id),
        )
        self.conn.commit()

    def list_tp_clears(self, position_id: str | None = None) -> list[dict]:
        """Return take-profit clear attempts (all, or one position's), in id order."""
        if position_id is None:
            rows = self.conn.execute("SELECT * FROM tp_clears ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM tp_clears WHERE position_id = ? ORDER BY id",
                (position_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_lot(self, lot: Lot) -> int:
        """Persist a new ladder lot. Returns its row id."""
        now = utcnow()
        cur = self.conn.execute(
            """
            INSERT INTO lots (book, symbol, position_id, units, entry_price,
                              cost_basis, entry_date, entry_reference_spy,
                              entry_spy_ath, entry_spy_close, entry_spy_drawdown,
                              entry_tier, entry_order_id, bucket_id, is_open,
                              exit_date, exit_price, exit_proceeds, exit_order_id,
                              realized_pnl, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lot.book, lot.symbol, lot.position_id, lot.units, lot.entry_price,
                lot.cost_basis, lot.entry_date, lot.entry_reference_spy,
                lot.entry_spy_ath, lot.entry_spy_close, lot.entry_spy_drawdown,
                lot.entry_tier, lot.entry_order_id, lot.bucket_id, int(lot.is_open),
                lot.exit_date, lot.exit_price, lot.exit_proceeds, lot.exit_order_id,
                lot.realized_pnl, now, now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_lot_position_id(self, lot_id: int, position_id: str) -> None:
        """Attach the broker position id once the opening order has filled."""
        self.conn.execute(
            "UPDATE lots SET position_id = ?, updated_at = ? WHERE id = ?",
            (position_id, utcnow(), lot_id),
        )
        self.conn.commit()

    def get_lot(self, lot_id: int) -> Lot | None:
        """Return a lot by row id."""
        row = self.conn.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
        return self._lot(row) if row else None

    def get_lot_by_position_id(self, position_id: str) -> Lot | None:
        """Return a lot by broker position id — the gate 11 reconciliation key.

        DESIGN §1.4 fix #3: an unmatched broker position is a preflight failure,
        never a fabricated lot, because a fabricated lot has no book, no vintage
        and no entry ATH.
        """
        row = self.conn.execute(
            "SELECT * FROM lots WHERE position_id = ?", (position_id,)
        ).fetchone()
        return self._lot(row) if row else None

    def open_lots(self, book: str | None = None, symbol: str | None = None) -> list[Lot]:
        """Return open lots, optionally filtered by book and/or symbol."""
        clauses = ["is_open = 1"]
        params: list[Any] = []
        if book is not None:
            clauses.append("book = ?")
            params.append(book)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        rows = self.conn.execute(
            f"SELECT * FROM lots WHERE {' AND '.join(clauses)} "
            "ORDER BY entry_date, id",
            tuple(params),
        ).fetchall()
        return [self._lot(r) for r in rows]

    def close_lot(
        self,
        lot_id: int,
        exit_date: str,
        exit_price: float,
        exit_proceeds: float,
        exit_order_id: int | None = None,
        realized_pnl: float | None = None,
    ) -> None:
        """Mark a lot closed.  Always a full close — one lot is one position."""
        self.conn.execute(
            """
            UPDATE lots
               SET is_open = 0, exit_date = ?, exit_price = ?, exit_proceeds = ?,
                   exit_order_id = ?, realized_pnl = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                exit_date, exit_price, exit_proceeds, exit_order_id,
                realized_pnl if realized_pnl is not None else None,
                utcnow(), lot_id,
            ),
        )
        self.conn.commit()

    @staticmethod
    def _lot(row: sqlite3.Row) -> Lot:
        return Lot(
            id=row["id"], book=row["book"], symbol=row["symbol"],
            position_id=row["position_id"], units=row["units"],
            entry_price=row["entry_price"], cost_basis=row["cost_basis"],
            entry_date=row["entry_date"],
            entry_reference_spy=row["entry_reference_spy"],
            entry_spy_ath=row["entry_spy_ath"],
            entry_spy_close=row["entry_spy_close"],
            entry_spy_drawdown=row["entry_spy_drawdown"],
            entry_tier=row["entry_tier"], entry_order_id=row["entry_order_id"],
            bucket_id=row["bucket_id"], is_open=bool(row["is_open"]),
            exit_date=row["exit_date"], exit_price=row["exit_price"],
            exit_proceeds=row["exit_proceeds"], exit_order_id=row["exit_order_id"],
            realized_pnl=row["realized_pnl"],
        )

    # -- lot annotations (DESIGN_market_data.md §4.4) -----------------------

    def get_lot_annotation(self, position_id: str) -> LotAnnotation | None:
        """Return a sidecar row by broker ``position_id``, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM lot_annotations WHERE position_id = ?", (position_id,)
        ).fetchone()
        return self._lot_annotation(row) if row else None

    def list_lot_annotations(
        self,
        *,
        book: str | None = None,
        symbol: str | None = None,
        origin: str | None = None,
        is_open: bool | None = None,
    ) -> list[LotAnnotation]:
        """Return sidecar rows, optionally filtered.

        Used by gate 11's membership check (DESIGN_market_data.md §4.5) and
        by anything reporting on the adopted set specifically
        (``origin='adopted'``, §4.7).
        """
        clauses: list[str] = []
        params: list[Any] = []
        if book is not None:
            clauses.append("book = ?")
            params.append(book)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if origin is not None:
            clauses.append("origin = ?")
            params.append(origin)
        if is_open is not None:
            clauses.append("is_open = ?")
            params.append(int(is_open))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM lot_annotations {where} ORDER BY entry_date, position_id",
            tuple(params),
        ).fetchall()
        return [self._lot_annotation(r) for r in rows]

    def open_lot_annotation_position_ids(self) -> set[str]:
        """Return every open sidecar row's ``position_id`` — gate 11
        invariant 2's side of the membership check (DESIGN_market_data.md
        §4.5): every open sidecar row must have a live broker position.
        """
        rows = self.conn.execute(
            "SELECT position_id FROM lot_annotations WHERE is_open = 1"
        ).fetchall()
        return {r["position_id"] for r in rows}

    @staticmethod
    def _validate_lot_annotation(annotation: "LotAnnotation") -> None:
        """Raise ``ValueError`` on a ``book``/``origin`` the table's own
        ``CHECK`` constraints would reject.

        This duplicates those two ``CHECK`` clauses in Python deliberately:
        ``INSERT OR IGNORE`` (the write path :meth:`insert_lot_annotations`
        needs for idempotency) resolves a ``CHECK`` violation by silently
        skipping the row -- confirmed against this SQLite build, no
        exception, ``rowcount`` simply 0 -- unlike a ``FOREIGN KEY``
        violation, which still raises even under ``OR IGNORE``. A silent
        skip is exactly the "didn't error, not a pass" failure mode this
        project's own history keeps flagging, so a bad ``book``/``origin``
        must be caught here, in Python, before it ever reaches SQL.
        """
        if annotation.book not in ("catapult", "reentry"):
            raise ValueError(
                f"lot_annotations.book must be 'catapult' or 'reentry', got "
                f"{annotation.book!r} (position_id={annotation.position_id!r})"
            )
        if annotation.origin not in ("app", "adopted"):
            raise ValueError(
                f"lot_annotations.origin must be 'app' or 'adopted', got "
                f"{annotation.origin!r} (position_id={annotation.position_id!r})"
            )
        if annotation.entry_context_source not in (
            "derived", "assumed_out_of_window", "adopted_from_abandoned_intent",
        ):
            raise ValueError(
                "lot_annotations.entry_context_source must be 'derived', "
                "'assumed_out_of_window', or 'adopted_from_abandoned_intent', "
                f"got {annotation.entry_context_source!r} "
                f"(position_id={annotation.position_id!r})"
            )
        if annotation.exit_context_source is not None and (
            annotation.exit_context_source != "resolved_from_abandoned_intent"
        ):
            raise ValueError(
                "lot_annotations.exit_context_source must be None or "
                f"'resolved_from_abandoned_intent', got "
                f"{annotation.exit_context_source!r} "
                f"(position_id={annotation.position_id!r})"
            )

    def insert_lot_annotation(self, annotation: LotAnnotation) -> None:
        """Insert one sidecar row. Raises ``sqlite3.IntegrityError`` if
        ``position_id`` already has one -- annotations are stamped once at
        open and never overwritten (§4.3), so a caller that expects to
        possibly re-run should use :meth:`insert_lot_annotations` instead,
        which is idempotent by construction. Raises ``ValueError`` on an
        invalid ``book``/``origin`` (see :meth:`_validate_lot_annotation`).
        """
        with self.conn:
            self._insert_lot_annotation(annotation)

    def apply_reconciliation(self, changes: Sequence[Adoption | Resolution]) -> None:
        """Commit gate-11 annotations and their resolved intents together or neither."""
        from ..engine.reconcile import Adoption

        if not changes:
            return
        with self.conn:
            self.conn.execute("BEGIN")
            now = utcnow()
            for change in changes:
                intent = self.get_intent_by_request_id(change.intent.request_id)
                if intent is None or intent.id != change.intent.id:
                    raise ValueError("reconciliation requires a persisted intent")
                if intent.status == "recovered" and intent.position_id == change.position_id:
                    continue
                if intent.status != "abandoned":
                    raise ValueError("reconciliation requires an abandoned intent")
                if isinstance(change, Adoption):
                    if intent.sent_at is None:
                        raise ValueError("adoption requires a sent abandoned intent")
                    self._insert_lot_annotation(change.annotation)
                else:
                    cur = self.conn.execute(
                        "UPDATE lot_annotations SET is_open = 0, exit_context_source = ?, "
                        "updated_at = ? WHERE position_id = ? AND is_open = 1",
                        (change.annotation.exit_context_source, now, change.position_id),
                    )
                    if cur.rowcount != 1:
                        raise ValueError("reconciliation requires an open annotation")
                self.conn.execute(
                    "UPDATE intents SET status = 'recovered', position_id = ?, resolved_at = ? "
                    "WHERE id = ?",
                    (change.position_id, now, intent.id),
                )

    def finalize_filled_buy(
        self, intent_id: int, annotation: LotAnnotation, *, status: str = "filled"
    ) -> None:
        """Commit the verified annotation and resolved intent together or neither."""
        if status not in ("filled", "recovered"):
            raise ValueError("invalid filled buy final status")
        with self.conn:
            self.conn.execute("BEGIN")
            self._insert_lot_annotation(annotation)
            cur = self.conn.execute(
                "UPDATE intents SET status = ?, resolved_at = ? "
                "WHERE id = ? AND side = 'buy' AND status = 'filled_unannotated'",
                (status, utcnow(), intent_id),
            )
            if cur.rowcount != 1:
                raise ValueError("filled buy is not awaiting annotation")

    def _insert_lot_annotation(self, annotation: LotAnnotation) -> None:
        self._validate_lot_annotation(annotation)
        now = utcnow()
        self.conn.execute(
            """
            INSERT INTO lot_annotations (
                position_id, symbol, book, entry_reference_spy, entry_spy_ath,
                entry_spy_close, entry_spy_drawdown, entry_tier, bucket_id,
                entry_order_id, exit_order_id, entry_date, entry_amount,
                is_open, origin, entry_context_source, exit_context_source,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                annotation.position_id, annotation.symbol, annotation.book,
                annotation.entry_reference_spy, annotation.entry_spy_ath,
                annotation.entry_spy_close, annotation.entry_spy_drawdown,
                annotation.entry_tier, annotation.bucket_id,
                annotation.entry_order_id, annotation.exit_order_id,
                annotation.entry_date, annotation.entry_amount,
                int(annotation.is_open), annotation.origin,
                annotation.entry_context_source, annotation.exit_context_source,
                annotation.created_at or now, annotation.updated_at or now,
            ),
        )

    def insert_lot_annotations(self, annotations: Iterable[LotAnnotation]) -> int:
        """Insert many sidecar rows in one all-or-nothing transaction.

        Built for the §4.7 one-shot adoption backfill
        (DESIGN_market_data.md §4.7, §10.10), whose task brief requires both
        properties this method gives:

        * **Idempotent.** ``INSERT OR IGNORE`` keyed on ``position_id``
          (the table's primary key) -- an annotation already present is left
          completely untouched, never updated, matching §4.3's "stamp once,
          never overwrite" rule. Running this twice with the same rows
          inserts nothing the second time; §10.10 notes a straggler fill may
          need exactly this re-run.
        * **Atomic.** Every row is written under the connection's one
          implicit transaction (``isolation_level="DEFERRED"``, opened by
          the first ``INSERT`` below) and committed together, or none of
          them are: any exception rolls back the whole batch rather than
          leaving a partially-adopted sidecar, which the task brief calls
          worse than no backfill at all (a half-satisfied gate 11 invariant
          1 halts every future session exactly like a fully-unsatisfied one,
          but with no clean starting point to retry from).

        Returns the number of rows actually inserted (excludes position_ids
        that already had a row).

        Raises:
            ValueError: If ANY row has an invalid ``book``/``origin`` --
                checked for every row before any ``INSERT`` is issued, since
                (see :meth:`_validate_lot_annotation`) that specific mistake
                would otherwise be swallowed silently by ``OR IGNORE``
                rather than raised, which is exactly the failure mode this
                method exists to avoid.
        """
        annotations = list(annotations)
        for annotation in annotations:
            self._validate_lot_annotation(annotation)

        now = utcnow()
        inserted = 0
        try:
            for annotation in annotations:
                cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO lot_annotations (
                        position_id, symbol, book, entry_reference_spy,
                        entry_spy_ath, entry_spy_close, entry_spy_drawdown,
                        entry_tier, bucket_id, entry_order_id, exit_order_id,
                        entry_date, entry_amount, is_open, origin,
                        entry_context_source, exit_context_source,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        annotation.position_id, annotation.symbol,
                        annotation.book, annotation.entry_reference_spy,
                        annotation.entry_spy_ath, annotation.entry_spy_close,
                        annotation.entry_spy_drawdown, annotation.entry_tier,
                        annotation.bucket_id, annotation.entry_order_id,
                        annotation.exit_order_id, annotation.entry_date,
                        annotation.entry_amount, int(annotation.is_open),
                        annotation.origin, annotation.entry_context_source,
                        annotation.exit_context_source,
                        annotation.created_at or now,
                        annotation.updated_at or now,
                    ),
                )
                if cur.rowcount == 1:
                    inserted += 1
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
        return inserted

    def update_lot_annotation(self, annotation: LotAnnotation) -> None:
        """Update an existing sidecar row in place, keyed by ``position_id``.

        The write path a PARTIAL CLOSE needs (board nightwatch/handoff-2026-09-06.md §7):
        unlike a full close, the broker position stays OPEN with reduced
        units, so its `lot_annotations` row must be updated rather than
        reinserted or replaced. Complements, rather than replaces,
        :meth:`insert_lot_annotation`/:meth:`insert_lot_annotations` -- a
        lot's FIRST annotation is still write-once (stamped at open, §4.3);
        this method is for every write after that.

        Does NOT touch the re-link key (`entry_date`/`entry_amount`, §4.4 --
        stamped once at open, immutable by design: eToro rescales `units`/
        `openRate` on a split but preserves `amount`, so re-deriving the
        re-link key from a live row would defeat its whole purpose) or the
        identity/provenance columns (`position_id`, `symbol`, `book`,
        `origin`, `entry_context_source`) -- those are set once at insert
        time and never change here either. Every other column is updatable:
        the entry-SPY-context fields, `entry_tier`, `bucket_id`,
        `entry_order_id`, `exit_order_id`, `exit_context_source`, and
        `is_open`.

        `exit_context_source` (migration 007) is deliberately updatable
        here, unlike `entry_context_source` -- it is CLOSE-time provenance
        (e.g. `engine.reconcile.resolve_abandoned_close_intents`'s narrow
        auto-resolve path stamping `'resolved_from_abandoned_intent'` when
        it marks a vanished position's annotation closed), so it can only
        ever be known at the point this method is called, never at insert
        time.

        Note `lot_annotations` carries no `units`/`amount` column of its own
        -- the broker (`Position`) is the sole system of record for those
        (§4.4) -- so a partial close's unit reduction has nothing here to
        update; this method exists for the sidecar bookkeeping a partial
        close DOES touch (`is_open` staying ``True``, `exit_order_id` once
        the executor records which order performed the close).
        TODO(lot-selection executor, out of scope here): call this after a
        partial close's order reaches terminal -- the executor itself is not
        built yet (board nightwatch/handoff-2026-09-06.md §7).

        Raises:
            ValueError: If no row exists at ``annotation.position_id`` -- a
                partial close can only update a lot that already has an
                annotation, mirroring :meth:`insert_lot_annotation`'s own
                failure mode (there: raises on a row that already exists;
                here: raises on one that doesn't).
        """
        self._validate_lot_annotation(annotation)
        now = utcnow()
        cur = self.conn.execute(
            """
            UPDATE lot_annotations SET
                entry_reference_spy = ?, entry_spy_ath = ?, entry_spy_close = ?,
                entry_spy_drawdown = ?, entry_tier = ?, bucket_id = ?,
                entry_order_id = ?, exit_order_id = ?, exit_context_source = ?,
                is_open = ?, updated_at = ?
            WHERE position_id = ?
            """,
            (
                annotation.entry_reference_spy, annotation.entry_spy_ath,
                annotation.entry_spy_close, annotation.entry_spy_drawdown,
                annotation.entry_tier, annotation.bucket_id,
                annotation.entry_order_id, annotation.exit_order_id,
                annotation.exit_context_source,
                int(annotation.is_open), annotation.updated_at or now,
                annotation.position_id,
            ),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"no lot_annotations row for position_id={annotation.position_id!r} "
                "to update -- update_lot_annotation requires an existing row "
                "(use insert_lot_annotation for a lot's first annotation)"
            )
        self.conn.commit()

    @staticmethod
    def _lot_annotation(row: sqlite3.Row) -> LotAnnotation:
        return LotAnnotation(
            position_id=row["position_id"], symbol=row["symbol"],
            book=row["book"], entry_reference_spy=row["entry_reference_spy"],
            entry_spy_ath=row["entry_spy_ath"],
            entry_spy_close=row["entry_spy_close"],
            entry_spy_drawdown=row["entry_spy_drawdown"],
            entry_tier=row["entry_tier"], bucket_id=row["bucket_id"],
            entry_order_id=row["entry_order_id"],
            exit_order_id=row["exit_order_id"], entry_date=row["entry_date"],
            entry_amount=row["entry_amount"], is_open=bool(row["is_open"]),
            origin=row["origin"],
            entry_context_source=row["entry_context_source"],
            exit_context_source=row["exit_context_source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -- price window fingerprints (DESIGN_market_data.md §3.4, §7) --------

    def record_price_window_fingerprint(self, fingerprint: PriceWindowFingerprint) -> None:
        """Persist one session's window fingerprint (never the price series
        itself). Idempotent per ``(trading_date, symbol)`` -- re-running the
        same session's window assembly overwrites its own fingerprint rather
        than raising, since re-assembly is expected (e.g. a retried
        session), not an error.
        """
        self.conn.execute(
            """
            INSERT INTO price_window_fingerprints (
                trading_date, symbol, window_start, window_end, bar_count,
                series_sha256, source, source_fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (trading_date, symbol) DO UPDATE SET
                window_start = excluded.window_start,
                window_end = excluded.window_end,
                bar_count = excluded.bar_count,
                series_sha256 = excluded.series_sha256,
                source = excluded.source,
                source_fetched_at = excluded.source_fetched_at
            """,
            (
                fingerprint.trading_date, fingerprint.symbol,
                fingerprint.window_start, fingerprint.window_end,
                fingerprint.bar_count, fingerprint.series_sha256,
                fingerprint.source, fingerprint.source_fetched_at,
            ),
        )
        self.conn.commit()

    def get_price_window_fingerprint(
        self, trading_date: str, symbol: str
    ) -> PriceWindowFingerprint | None:
        """Return one session's stored fingerprint for ``symbol``, or
        ``None`` if none was recorded."""
        row = self.conn.execute(
            "SELECT * FROM price_window_fingerprints WHERE trading_date = ? "
            "AND symbol = ?",
            (trading_date, symbol),
        ).fetchone()
        return self._price_window_fingerprint(row) if row else None

    def list_price_window_fingerprints(self, symbol: str) -> list[PriceWindowFingerprint]:
        """Return every stored fingerprint for ``symbol``, most recent
        session first -- used to find the previous session's overlapping
        window to compare a new fingerprint against (§3.4's revision-
        detection recovery)."""
        rows = self.conn.execute(
            "SELECT * FROM price_window_fingerprints WHERE symbol = ? "
            "ORDER BY trading_date DESC",
            (symbol,),
        ).fetchall()
        return [self._price_window_fingerprint(r) for r in rows]

    @staticmethod
    def _price_window_fingerprint(row: sqlite3.Row) -> PriceWindowFingerprint:
        return PriceWindowFingerprint(
            trading_date=row["trading_date"], symbol=row["symbol"],
            window_start=row["window_start"], window_end=row["window_end"],
            bar_count=row["bar_count"], series_sha256=row["series_sha256"],
            source=row["source"], source_fetched_at=row["source_fetched_at"],
        )

    # -- buckets -----------------------------------------------------------

    def upsert_bucket(self, bucket: Bucket) -> None:
        """Insert or update a deferred re-entry bucket."""
        self.conn.execute(
            """
            INSERT INTO buckets (bucket_id, sale_date, sale_spy_close, proceeds,
                                 tranche_amount, tranches_bought,
                                 last_reference_spy, created_bar, is_active,
                                 expired_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (bucket_id) DO UPDATE SET
                sale_date = excluded.sale_date,
                sale_spy_close = excluded.sale_spy_close,
                proceeds = excluded.proceeds,
                tranche_amount = excluded.tranche_amount,
                tranches_bought = excluded.tranches_bought,
                last_reference_spy = excluded.last_reference_spy,
                created_bar = excluded.created_bar,
                is_active = excluded.is_active,
                expired_at = excluded.expired_at
            """,
            (
                bucket.bucket_id, bucket.sale_date, bucket.sale_spy_close,
                bucket.proceeds, bucket.tranche_amount, bucket.tranches_bought,
                bucket.last_reference_spy, bucket.created_bar,
                int(bucket.is_active), bucket.expired_at,
                bucket.created_at or utcnow(),
            ),
        )
        self.conn.commit()

    def get_bucket(self, bucket_id: str) -> Bucket | None:
        """Return a bucket by id."""
        row = self.conn.execute(
            "SELECT * FROM buckets WHERE bucket_id = ?", (bucket_id,)
        ).fetchone()
        return self._bucket(row) if row else None

    def active_buckets(self) -> list[Bucket]:
        """Return live buckets in creation order (oldest first)."""
        rows = self.conn.execute(
            "SELECT * FROM buckets WHERE is_active = 1 ORDER BY created_bar, bucket_id"
        ).fetchall()
        return [self._bucket(r) for r in rows]

    def expire_bucket(self, bucket_id: str) -> None:
        """Retire a bucket that has passed ``bucket_expiry_bars``."""
        self.conn.execute(
            "UPDATE buckets SET is_active = 0, expired_at = ? WHERE bucket_id = ?",
            (utcnow(), bucket_id),
        )
        self.conn.commit()

    @staticmethod
    def _bucket(row: sqlite3.Row) -> Bucket:
        return Bucket(
            bucket_id=row["bucket_id"], sale_date=row["sale_date"],
            sale_spy_close=row["sale_spy_close"], proceeds=row["proceeds"],
            tranche_amount=row["tranche_amount"],
            tranches_bought=row["tranches_bought"],
            last_reference_spy=row["last_reference_spy"],
            created_bar=row["created_bar"], is_active=bool(row["is_active"]),
            expired_at=row["expired_at"], created_at=row["created_at"],
        )

    # -- strategy state (singleton) ---------------------------------------

    def get_strategy_state(self) -> StrategyState:
        """Return the singleton state row, creating it on first access.

        Returning a default rather than ``None`` is deliberate: every caller
        needs a state, and "no row yet" and "a fresh install" are the same
        thing.
        """
        row = self.conn.execute(
            "SELECT * FROM strategy_state WHERE id = 1"
        ).fetchone()
        if row is None:
            state = StrategyState()
            self.save_strategy_state(state)
            return state
        return StrategyState(
            bars=row["bars"], ath=row["ath"], below_streak=row["below_streak"],
            recyclable_cash=row["recyclable_cash"], budget_year=row["budget_year"],
            budget=row["budget"], order_size=row["order_size"],
            cash_target=row["cash_target"],
            sale_batch_sequence=row["sale_batch_sequence"],
            sale_batch_date=row["sale_batch_date"], updated_at=row["updated_at"],
        )

    def save_market_state(
        self, *, bars: int, ath: float | None, below_streak: int,
        expired_bucket_ids: Sequence[str], trading_date: str,
    ) -> None:
        """Checkpoint observed facts without committing simulated spending or fills."""
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.execute(
                "UPDATE strategy_state SET bars = ?, ath = ?, below_streak = ?, "
                "updated_at = ? WHERE id = 1",
                (bars, ath, below_streak, utcnow()),
            )
            self.conn.executemany(
                "UPDATE buckets SET is_active = 0, expired_at = ? "
                "WHERE bucket_id = ? AND is_active = 1",
                [(trading_date, bucket_id) for bucket_id in expired_bucket_ids],
            )

    def save_strategy_state(self, state: StrategyState) -> None:
        """Write the singleton state row.

        ``bars`` is persisted here specifically so a freshly constructed
        strategy does not restart at 1 and sit in warmup forever
        (DESIGN §1.4).
        """
        self.conn.execute(
            """
            INSERT INTO strategy_state (id, bars, ath, below_streak,
                                        recyclable_cash, budget_year, budget,
                                        order_size, cash_target,
                                        sale_batch_sequence, sale_batch_date,
                                        updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                bars = excluded.bars, ath = excluded.ath,
                below_streak = excluded.below_streak,
                recyclable_cash = excluded.recyclable_cash,
                budget_year = excluded.budget_year, budget = excluded.budget,
                order_size = excluded.order_size,
                cash_target = excluded.cash_target,
                sale_batch_sequence = excluded.sale_batch_sequence,
                sale_batch_date = excluded.sale_batch_date,
                updated_at = excluded.updated_at
            """,
            (
                state.bars, state.ath, state.below_streak, state.recyclable_cash,
                state.budget_year, state.budget, state.order_size,
                state.cash_target, state.sale_batch_sequence,
                state.sale_batch_date, state.updated_at or utcnow(),
            ),
        )
        self.conn.commit()

    # -- SCHG daily values and budget --------------------------------------

    def upsert_schg_value(self, date: str, value: float) -> None:
        """Record SCHG's market value for a date (feeds next year's budget)."""
        self.conn.execute(
            """
            INSERT INTO schg_daily_values (date, value, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT (date) DO UPDATE SET value = excluded.value
            """,
            (date, value, utcnow()),
        )
        self.conn.commit()

    def mean_schg_value(self, year: int) -> float | None:
        """Return the mean SCHG value over a calendar year, or ``None``.

        ``None`` is the empty-prior-year case that DESIGN §1.4 fix #4 makes the
        caller handle with a ``core_fraction × equity`` fallback instead of a
        crash on January 2.
        """
        row = self.conn.execute(
            "SELECT AVG(value) AS m, COUNT(*) AS n FROM schg_daily_values "
            "WHERE date >= ? AND date <= ?",
            (f"{year:04d}-01-01", f"{year:04d}-12-31"),
        ).fetchone()
        if row is None or not row["n"]:
            return None
        return float(row["m"])

    def insert_budget_record(self, record: BudgetRecord) -> int:
        """Append to ``budget_history``. Returns the row id."""
        cur = self.conn.execute(
            """
            INSERT INTO budget_history (year, budget, order_size, cash_target,
                                        source, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.year, record.budget, record.order_size, record.cash_target,
                record.source, record.note, record.created_at or utcnow(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def latest_budget(self, year: int | None = None) -> BudgetRecord | None:
        """Return the most recent budget record, optionally for one year."""
        if year is None:
            row = self.conn.execute(
                "SELECT * FROM budget_history ORDER BY id DESC LIMIT 1"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM budget_history WHERE year = ? ORDER BY id DESC LIMIT 1",
                (year,),
            ).fetchone()
        if row is None:
            return None
        return BudgetRecord(
            id=row["id"], year=row["year"], budget=row["budget"],
            order_size=row["order_size"], cash_target=row["cash_target"],
            source=row["source"], note=row["note"], created_at=row["created_at"],
        )

    # -- portfolio snapshots ----------------------------------------------

    def insert_snapshot(self, snapshot: PortfolioSnapshot) -> int:
        """Persist a portfolio snapshot and its position rows. Returns the id."""
        try:
            cur = self.conn.execute(
                """
                INSERT INTO portfolio_snapshots (session_id, taken_at, equity, cash,
                                                 available_cash, unrealized_pnl,
                                                 realized_pnl, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.session_id, snapshot.taken_at, snapshot.equity,
                    snapshot.cash, snapshot.available_cash, snapshot.unrealized_pnl,
                    snapshot.realized_pnl, json.dumps(snapshot.raw),
                ),
            )
            snapshot_id = int(cur.lastrowid)
            for position in snapshot.positions:
                self.conn.execute(
                    """
                    INSERT INTO portfolio_snapshot_positions
                        (snapshot_id, position_id, symbol, instrument_id, units,
                         entry_price, current_price, amount, leverage,
                         unrealized_pnl, open_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id, position.position_id, position.symbol,
                        position.instrument_id, position.units, position.entry_price,
                        position.current_price, position.amount, position.leverage,
                        position.unrealized_pnl, position.open_date,
                    ),
                )
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()
        return snapshot_id

    def get_snapshot(self, snapshot_id: int) -> PortfolioSnapshot | None:
        """Return a snapshot with its position rows attached."""
        row = self.conn.execute(
            "SELECT * FROM portfolio_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            return None
        positions = [
            SnapshotPosition(
                position_id=p["position_id"], symbol=p["symbol"],
                instrument_id=p["instrument_id"], units=p["units"],
                entry_price=p["entry_price"], current_price=p["current_price"],
                amount=p["amount"], leverage=p["leverage"],
                unrealized_pnl=p["unrealized_pnl"], open_date=p["open_date"],
            )
            for p in self.conn.execute(
                "SELECT * FROM portfolio_snapshot_positions WHERE snapshot_id = ? "
                "ORDER BY id",
                (snapshot_id,),
            ).fetchall()
        ]
        return PortfolioSnapshot(
            id=row["id"], session_id=row["session_id"], taken_at=row["taken_at"],
            equity=row["equity"], cash=row["cash"],
            available_cash=row["available_cash"],
            unrealized_pnl=row["unrealized_pnl"], realized_pnl=row["realized_pnl"],
            positions=positions, raw=_j(row["raw_json"], {}),
        )

    def latest_snapshot(self) -> PortfolioSnapshot | None:
        """Return the most recent snapshot, or ``None``."""
        row = self.conn.execute(
            "SELECT id FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else self.get_snapshot(int(row["id"]))

    # -- notifications -----------------------------------------------------

    def insert_notification(self, notification: Notification) -> int:
        """Record a Telegram message before sending it. Returns the row id."""
        cur = self.conn.execute(
            """
            INSERT INTO notifications (session_id, trading_date, event, body,
                                       chat_id, status, error,
                                       telegram_message_id, created_at, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                notification.session_id, notification.trading_date,
                notification.event, notification.body, notification.chat_id,
                notification.status, notification.error,
                notification.telegram_message_id,
                notification.created_at or utcnow(), notification.sent_at,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_notification_status(
        self,
        notification_id: int,
        status: str,
        error: str | None = None,
        telegram_message_id: str | None = None,
    ) -> None:
        """Record delivery success or failure.

        A *failed* alert has to be visible in its own right (DESIGN §7.1), which
        is why failure is stored rather than swallowed.
        """
        self.conn.execute(
            """
            UPDATE notifications
               SET status = ?, error = ?,
                   telegram_message_id = COALESCE(?, telegram_message_id),
                   sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END
             WHERE id = ?
            """,
            (
                status, error, telegram_message_id, status, utcnow(),
                notification_id,
            ),
        )
        self.conn.commit()

    def get_notification(self, notification_id: int) -> Notification | None:
        """Return one notification row."""
        row = self.conn.execute(
            "SELECT * FROM notifications WHERE id = ?", (notification_id,)
        ).fetchone()
        if row is None:
            return None
        return Notification(
            id=row["id"], session_id=row["session_id"],
            trading_date=row["trading_date"], event=row["event"],
            body=row["body"], chat_id=row["chat_id"], status=row["status"],
            error=row["error"], telegram_message_id=row["telegram_message_id"],
            created_at=row["created_at"], sent_at=row["sent_at"],
        )

    def pending_notifications(self) -> list[Notification]:
        """Return notifications that have not been delivered."""
        rows = self.conn.execute(
            "SELECT id FROM notifications WHERE status = 'pending' ORDER BY id"
        ).fetchall()
        return [n for n in (self.get_notification(int(r["id"])) for r in rows) if n]

    # -- commands (inbound control messages) --------------------------------

    def last_command_counter(self, channel: str, sender_id: str) -> int | None:
        """Return the highest accepted HMAC counter for a (channel, sender).

        Used to reject replays: an inbound counter that is not strictly
        greater than this must be rejected before the command is actioned
        (DESIGN §6.1, §7.2).
        """
        row = self.conn.execute(
            "SELECT MAX(counter) AS n FROM commands "
            "WHERE channel = ? AND sender_id = ? AND verdict = 'accepted'",
            (channel, sender_id),
        ).fetchone()
        return None if row is None or row["n"] is None else int(row["n"])

    def is_replay(self, channel: str, sender_id: str, counter: int | None) -> bool:
        """Return True if ``counter`` is not strictly newer than the last
        accepted counter for this (channel, sender) pair.

        A ``None`` counter (a channel/command shape that carries none) is
        never treated as a replay by this check alone — callers that require a
        counter should validate its presence separately.
        """
        if counter is None:
            return False
        last = self.last_command_counter(channel, sender_id)
        return last is not None and counter <= last

    def insert_command(self, command: Command) -> int:
        """Record one inbound command and its verdict. Returns the row id."""
        cur = self.conn.execute(
            """
            INSERT INTO commands (channel, sender_id, chat_id, command, counter,
                                  verdict, reason, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                command.channel, command.sender_id, command.chat_id,
                command.command, command.counter, command.verdict,
                command.reason, command.received_at or utcnow(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_command(self, command_id: int) -> Command | None:
        """Return one command row by id."""
        row = self.conn.execute(
            "SELECT * FROM commands WHERE id = ?", (command_id,)
        ).fetchone()
        return self._command(row) if row else None

    def get_commands(
        self, channel: str | None = None, sender_id: str | None = None, limit: int = 50
    ) -> list[Command]:
        """Return recent commands, most recent first, optionally filtered."""
        clauses = []
        params: list[Any] = []
        if channel is not None:
            clauses.append("channel = ?")
            params.append(channel)
        if sender_id is not None:
            clauses.append("sender_id = ?")
            params.append(sender_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM commands {where} ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self._command(r) for r in rows]

    @staticmethod
    def _command(row: sqlite3.Row) -> Command:
        return Command(
            id=row["id"], channel=row["channel"], sender_id=row["sender_id"],
            chat_id=row["chat_id"], command=row["command"], counter=row["counter"],
            verdict=row["verdict"], reason=row["reason"],
            received_at=row["received_at"],
        )

    # -- diagnostics -------------------------------------------------------

    def table_names(self) -> list[str]:
        """Return every user table in the database, sorted."""
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]


def open_repo(path: Path | str | None = None, host: Host | None = None) -> Repo:
    """Open and migrate the database. Convenience wrapper over :meth:`Repo.open`."""
    return Repo.open(path, host)
