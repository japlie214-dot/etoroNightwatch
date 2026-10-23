-- Migration 009: per-phase decisions and durable app-owned close evidence.
CREATE TABLE decisions_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    trading_date  TEXT    NOT NULL,
    cone_z        REAL,
    spy_close     REAL,
    spy_ath       REAL,
    drawdown      REAL,
    tier          INTEGER,
    budget        REAL,
    order_size    REAL,
    cash_target   REAL,
    equity        REAL,
    cash          REAL,
    intents_json  TEXT    NOT NULL DEFAULT '[]',
    reason        TEXT,
    created_at    TEXT    NOT NULL,
    phase         TEXT    NOT NULL DEFAULT 'single' CHECK (phase IN ('A','B','single')),
    UNIQUE (session_id, phase)
);
INSERT INTO decisions_new (
    id, session_id, trading_date, cone_z, spy_close, spy_ath, drawdown, tier,
    budget, order_size, cash_target, equity, cash, intents_json, reason,
    created_at, phase
)
SELECT id, session_id, trading_date, cone_z, spy_close, spy_ath, drawdown, tier,
       budget, order_size, cash_target, equity, cash, intents_json, reason,
       created_at, 'single'
FROM decisions;
-- Preserve the high-water mark even when the highest old ids were deleted.
INSERT INTO sqlite_sequence (name, seq)
SELECT 'decisions_new', seq FROM sqlite_sequence
WHERE name = 'decisions'
  AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'decisions_new');
UPDATE sqlite_sequence
SET seq = MAX(seq, COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'decisions'), 0))
WHERE name = 'decisions_new';
DROP TABLE decisions;
ALTER TABLE decisions_new RENAME TO decisions;

CREATE TABLE close_observations (
    id              INTEGER PRIMARY KEY,
    intent_id       INTEGER NOT NULL REFERENCES intents(id),
    order_row_id    INTEGER REFERENCES orders(id),
    order_id        TEXT,
    position_id     TEXT NOT NULL,
    observed_status TEXT NOT NULL CHECK (observed_status IN (
        'confirmed_full_close', 'confirmed_closed', 'still_open', 'unresolved'
    )),
    units_closed    REAL,
    close_price     REAL,
    closed_at       TEXT,
    raw_json        TEXT NOT NULL DEFAULT '{}',
    observed_at     TEXT NOT NULL,
    applied_at      TEXT
);
CREATE INDEX idx_close_observations_position ON close_observations(position_id);
CREATE UNIQUE INDEX ux_close_obs_applied ON close_observations(position_id)
WHERE applied_at IS NOT NULL;
