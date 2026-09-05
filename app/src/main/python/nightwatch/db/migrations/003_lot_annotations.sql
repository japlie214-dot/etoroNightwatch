-- Migration 003: lot_annotations, the split-invariant strategy sidecar
-- (DESIGN_market_data.md §4.4, §4.7).
--
-- eToro is the system of record for the financial facts of a position
-- (units, entry price, cost basis); the local store keeps only the
-- strategy annotations a split does not touch, keyed by eToro's
-- position_id (§4.1). This table does not replace `lots` in this
-- migration -- `lots`/`bars` are dropped in a later step (DESIGN §7, §12
-- step 3); this migration only adds the sidecar so the §4.7 adoption
-- backfill has somewhere to write.
--
-- DDL below is copied verbatim from DESIGN_market_data.md §4.4, including
-- its comments -- do not strip the reasoning, especially the re-link-key
-- comment: a future reader who "simplifies" entry_date/entry_amount away
-- would silently reintroduce the exact bug §4.6 found (eToro rescales
-- units and openRate on a split; amount is what survives).
CREATE TABLE IF NOT EXISTS lot_annotations (
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
    -- position if position_id does not survive a corporate action.
    --
    -- This is `amount` (cost basis), not units or entry price, because a live
    -- 227-position probe (2026-09-04) proved units and openRate are NOT
    -- immutable: eToro retroactively rescales both on a split (15 pre-split
    -- SCHG positions, opened before the 2024-10-11 4:1, now carry post-split
    -- units and openRate). `amount` is preserved exactly across the same
    -- split -- verified `amount == units * openRate` on 227 of 227 live
    -- positions (§4.5) -- making it the only one of the three quantities that
    -- actually survives a corporate action. The original design used
    -- `entry_units_at_open` + `entry_price_at_open` as the "immutable" keys;
    -- that was wrong (§4.6) and is corrected here.
    entry_date           TEXT NOT NULL,
    entry_amount         REAL NOT NULL,
    is_open              INTEGER NOT NULL DEFAULT 1 CHECK (is_open IN (0, 1)),
    -- Provenance, not an access gate. Records whether the strategy annotations
    -- on this row were stamped at entry by the app ('app') or reconstructed
    -- from history for a position that predates the app ('adopted') -- see
    -- §4.7. Both kinds are equally managed and equally closeable; this column
    -- exists only so a future reader can tell derived values from stamped
    -- ones, not to restrict what the executor may do.
    origin               TEXT NOT NULL CHECK (origin IN ('app', 'adopted')),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
) WITHOUT ROWID;

-- Mirrors the `lots` table's own indexes (schema.sql) -- gate 11's
-- membership check and the ladder's book/symbol queries filter the same way
-- once lot_annotations is what they read from.
CREATE INDEX IF NOT EXISTS idx_lot_annotations_open
    ON lot_annotations (book, is_open);
CREATE INDEX IF NOT EXISTS idx_lot_annotations_symbol
    ON lot_annotations (symbol, is_open);
