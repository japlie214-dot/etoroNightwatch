-- Keep filled buys unresolved until their verified annotation is durable.
PRAGMA defer_foreign_keys = ON;
CREATE TABLE intents_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    request_id    TEXT    NOT NULL UNIQUE,
    sequence      INTEGER NOT NULL,
    symbol        TEXT    NOT NULL,
    instrument_id INTEGER,
    side          TEXT    NOT NULL CHECK (side IN ('buy', 'close')),
    book          TEXT    CHECK (book IN ('catapult', 'reentry', 'core')),
    amount_usd    REAL,
    units         REAL,
    position_id   TEXT,
    lot_id        INTEGER REFERENCES lots (id),
    bucket_id     TEXT    REFERENCES buckets (bucket_id),
    reason        TEXT,
    status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'sent', 'filled', 'rejected',
                                    'recovered', 'abandoned', 'filled_unannotated')),
    created_at    TEXT    NOT NULL,
    sent_at       TEXT,
    resolved_at   TEXT,
    UNIQUE (session_id, sequence)
);
INSERT INTO intents_new SELECT * FROM intents;
-- Retain deleted ids' high-water mark as well as all referenced rows.
INSERT INTO sqlite_sequence (name, seq)
SELECT 'intents_new', seq FROM sqlite_sequence
WHERE name = 'intents'
  AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'intents_new');
UPDATE sqlite_sequence
SET seq = MAX(seq, COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'intents'), 0))
WHERE name = 'intents_new';
DROP TABLE intents;
ALTER TABLE intents_new RENAME TO intents;
CREATE INDEX idx_intents_session ON intents (session_id);
CREATE INDEX idx_intents_status ON intents (status);
-- Rebuilding a referenced parent leaves SQLite's deferred violation counter set.
-- Validate the restored graph before clearing that counter for commit.
CREATE TEMP TABLE filled_unannotated_fk_check (violations INTEGER CHECK (violations = 0));
INSERT INTO filled_unannotated_fk_check SELECT COUNT(*) FROM pragma_foreign_key_check;
DROP TABLE filled_unannotated_fk_check;
PRAGMA defer_foreign_keys = OFF;
