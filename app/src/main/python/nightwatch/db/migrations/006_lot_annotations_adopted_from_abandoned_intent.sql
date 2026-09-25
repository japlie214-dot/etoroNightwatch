-- Migration 006: lot_annotations.entry_context_source gains
-- 'adopted_from_abandoned_intent' (DESIGN_session.md §3.4/§14 item 2, the
-- operator's 2026-09-06 "no forced human-in-the-loop" decision;
-- engine/reconcile.py's narrow auto-adopt path).
--
-- A persistently non-terminal order is now marked 'abandoned' (intents.status
-- already allows this value, schema.sql) rather than escalating to a human
-- forever. If that abandoned order later fills, gate 11 sees an unmatched
-- broker position and must not fail closed the way it does for a position
-- with no provenance at all -- it is adopted from OUR OWN persisted, matched
-- intent record. 'adopted_from_abandoned_intent' marks exactly that lot,
-- distinctly and permanently, so it is greppable and auditable forever after
-- and never silently indistinguishable from a normally-recorded row. It is
-- NOT the fabricate-a-lot bug (DESIGN.md §1.4 fix #3): that bug invented a
-- lot from nothing; this path only ever fires when a genuine persisted
-- Intent (instrument, size, our own request_id) is in hand to match against.
--
-- SQLite cannot ALTER a CHECK constraint in place, so the table is rebuilt
-- with the widened constraint, verbatim otherwise -- copied from migration
-- 003's DDL (as amended by migration 005), including its comments, for the
-- same "do not silently drop the reasoning" discipline migration 003 itself
-- states. This block is idempotent under a crash between COMMIT and the
-- schema_version write (db/migrations/__init__.py's contract): the ``SELECT
-- *`` below always copies from whichever table is currently named
-- lot_annotations (already-migrated or not), so re-running it is a no-op on
-- data, and DROP TABLE IF EXISTS makes the staging step safe to repeat.
DROP TABLE IF EXISTS lot_annotations_new_006;

CREATE TABLE lot_annotations_new_006 (
    position_id          TEXT PRIMARY KEY,   -- eToro's id; the join key
    symbol               TEXT NOT NULL,
    book                 TEXT NOT NULL CHECK (book IN ('catapult', 'reentry')),
    entry_reference_spy  REAL,
    entry_spy_ath        REAL,
    entry_spy_close      REAL,
    entry_spy_drawdown   REAL,
    entry_tier           INTEGER,
    bucket_id            TEXT REFERENCES buckets (bucket_id),
    entry_order_id       INTEGER REFERENCES orders (id),
    exit_order_id        INTEGER REFERENCES orders (id),
    -- Re-link key. Stamped once at open, never updated, never read for P&L or
    -- strategy. Present ONLY so a sidecar row can be re-matched to a broker
    -- position if position_id does not survive a corporate action. See
    -- migration 003 for the full reasoning (`amount` survives a split;
    -- `units`/`openRate` do not).
    entry_date           TEXT NOT NULL,
    entry_amount         REAL NOT NULL,
    is_open              INTEGER NOT NULL DEFAULT 1 CHECK (is_open IN (0, 1)),
    -- Provenance, not an access gate (migration 003). 'app'/'adopted' as
    -- before; unchanged by this migration.
    origin               TEXT NOT NULL CHECK (origin IN ('app', 'adopted')),
    -- Second, orthogonal provenance column (migration 005). Widened here to
    -- add 'adopted_from_abandoned_intent' -- still never an access gate: all
    -- three values are equally managed and equally closeable.
    entry_context_source TEXT NOT NULL DEFAULT 'derived'
        CHECK (entry_context_source IN (
            'derived', 'assumed_out_of_window', 'adopted_from_abandoned_intent'
        )),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
) WITHOUT ROWID;

INSERT INTO lot_annotations_new_006
SELECT position_id, symbol, book, entry_reference_spy, entry_spy_ath,
       entry_spy_close, entry_spy_drawdown, entry_tier, bucket_id,
       entry_order_id, exit_order_id, entry_date, entry_amount, is_open,
       origin, entry_context_source, created_at, updated_at
FROM lot_annotations;

DROP TABLE lot_annotations;

ALTER TABLE lot_annotations_new_006 RENAME TO lot_annotations;

CREATE INDEX IF NOT EXISTS idx_lot_annotations_open
    ON lot_annotations (book, is_open);
CREATE INDEX IF NOT EXISTS idx_lot_annotations_symbol
    ON lot_annotations (symbol, is_open);
