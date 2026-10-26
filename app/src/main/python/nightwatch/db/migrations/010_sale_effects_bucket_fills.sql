-- Sale effects precede sends; confirmations and fill applications survive restarts.
CREATE TABLE sale_effects (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    position_id TEXT NOT NULL UNIQUE,
    gross_proceeds_cents INTEGER NOT NULL CHECK (gross_proceeds_cents >= 0),
    classification TEXT NOT NULL CHECK (classification IN ('bucket','recyclable')),
    bucket_json TEXT,
    created_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE TABLE bucket_fill_applications (
    intent_id INTEGER PRIMARY KEY REFERENCES intents(id),
    bucket_id TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
