-- T073: durable evidence for the annual budget and for the daily SCHG sampling.
-- Both tables are written by the engine at its own decision point; the dashboard
-- projects them as stored and never recomputes the rule or parses reason text.

CREATE TABLE budget_evaluations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        INTEGER NOT NULL UNIQUE REFERENCES sessions (id),
    trading_date      TEXT    NOT NULL,
    evaluated_at      TEXT    NOT NULL,
    source_path       TEXT    NOT NULL
                      CHECK (source_path IN ('bootstrap', 'prior_year_mean',
                                             'missing_history_fallback', 'retained')),
    state_budget_year INTEGER,
    target_year       INTEGER NOT NULL,
    history_year      INTEGER NOT NULL,
    sample_count      INTEGER NOT NULL CHECK (sample_count >= 0),
    sample_mean       REAL,
    sleeve_equity     REAL    NOT NULL,
    core_fraction     REAL    NOT NULL,
    rounding_step     REAL    NOT NULL,
    sizing_floor      REAL    NOT NULL,
    order_divisor     REAL    NOT NULL,
    budget            REAL    NOT NULL,
    order_size        REAL    NOT NULL,
    cash_target       REAL    NOT NULL
);
CREATE INDEX idx_budget_evaluations_date ON budget_evaluations (trading_date);

-- One row per trading date: recorded (with the value) or skipped (with a fixed-vocabulary
-- reason). A skip never writes a zero and never replaces an earlier valid row.
CREATE TABLE schg_sampling_log (
    trading_date TEXT NOT NULL PRIMARY KEY,
    session_id   INTEGER REFERENCES sessions (id),
    status       TEXT NOT NULL CHECK (status IN ('recorded', 'skipped')),
    reason       TEXT,
    value        REAL,
    recorded_at  TEXT NOT NULL
) WITHOUT ROWID;
