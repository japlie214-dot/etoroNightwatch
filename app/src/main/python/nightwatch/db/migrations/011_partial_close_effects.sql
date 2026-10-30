CREATE TABLE partial_close_effects (
    id INTEGER PRIMARY KEY,
    intent_id INTEGER NOT NULL UNIQUE REFERENCES intents(id),
    order_row_id INTEGER REFERENCES orders(id),
    order_id TEXT,
    position_id TEXT NOT NULL,
    units_before REAL NOT NULL,
    units_deducted REAL,
    units_after REAL,
    slice_position_id TEXT,
    close_price REAL,
    proceeds REAL,
    status TEXT NOT NULL CHECK (status IN ('pending','confirmed','rejected','unresolved','unverified')),
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE INDEX idx_partial_close_effects_position ON partial_close_effects(position_id);
