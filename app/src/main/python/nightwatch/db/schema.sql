-- etoroNightwatch baseline schema (DESIGN §3).
--
-- Applied as migration version 1 by nightwatch.db.migrations.  Never edit this
-- file to change an existing deployment: add a numbered file under
-- db/migrations/ instead.  The runner is forward-only.
--
-- Conventions:
--   * dates are ISO-8601 TEXT.  Trading dates are 'YYYY-MM-DD' in
--     America/New_York; timestamps are UTC 'YYYY-MM-DDTHH:MM:SS+00:00'.
--   * booleans are INTEGER 0/1 with a CHECK constraint.
--   * money is REAL USD; units are REAL.

-- --------------------------------------------------------------------------
-- Daily bars.  is_provisional is the key column (DESIGN §4.2): today's bar is
-- written from the live rate at T-10min, and the NEXT session overwrites it
-- with the official OneDay candle and flips the flag to 0.  A bar left
-- provisional after settlement is a preflight gate 10 failure.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bars (
    symbol          TEXT    NOT NULL,
    date            TEXT    NOT NULL,              -- 'YYYY-MM-DD', ET trading date
    "open"          REAL,
    high            REAL,
    low             REAL,
    close           REAL    NOT NULL,
    volume          REAL,
    is_provisional  INTEGER NOT NULL DEFAULT 1 CHECK (is_provisional IN (0, 1)),
    source          TEXT    NOT NULL,              -- 'candles' | 'live_rate' | 'seed'
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (symbol, date)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_bars_provisional
    ON bars (symbol, is_provisional);
CREATE INDEX IF NOT EXISTS idx_bars_date ON bars (date);

-- --------------------------------------------------------------------------
-- Instruments: ticker -> immutable eToro instrument id, plus the last
-- eligibility snapshot that preflight gate 8 reads.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instruments (
    symbol                  TEXT    NOT NULL PRIMARY KEY,
    instrument_id           INTEGER NOT NULL UNIQUE,
    name                    TEXT,
    exchange                TEXT,
    currency                TEXT,
    -- eligibility snapshot (POST /trading/info/eligibility)
    allow_open_position     INTEGER CHECK (allow_open_position IN (0, 1)),
    min_position_exposure   REAL,
    min_position_amount     REAL,
    allow_partial_close     INTEGER CHECK (allow_partial_close IN (0, 1)),
    eligibility_checked_at  TEXT,
    metadata_json           TEXT,
    updated_at              TEXT    NOT NULL
) WITHOUT ROWID;

-- --------------------------------------------------------------------------
-- Sessions: one row per trading date.  UNIQUE on trading_date is what makes a
-- double run impossible, and is what the daily safety-net alarm relies on
-- (DESIGN §5, §6.2).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date          TEXT    NOT NULL UNIQUE,
    state                 TEXT    NOT NULL,   -- started|preflight|settled|decided|
                                              -- executing|reporting|finished|aborted|failed
    mode                  TEXT    NOT NULL,   -- 'dry_run' | 'live'
    environment           TEXT    NOT NULL,   -- 'demo' | 'real'
    armed                 INTEGER NOT NULL DEFAULT 0 CHECK (armed IN (0, 1)),
    preflight_verdict     TEXT,               -- 'pass' | 'fail'
    preflight_failed_gate INTEGER,
    started_at            TEXT    NOT NULL,
    preflight_at          TEXT,
    decision_at           TEXT,
    execution_at          TEXT,
    finished_at           TEXT,
    outcome               TEXT,               -- short human summary
    error                 TEXT,
    next_run_at           TEXT,               -- what step [11] rescheduled
    created_at            TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_state ON sessions (state);
CREATE INDEX IF NOT EXISTS idx_sessions_date  ON sessions (trading_date DESC);

-- --------------------------------------------------------------------------
-- Preflight: one row per gate per session — the full audit of why a run
-- aborted (DESIGN §5).  UNIQUE(session_id, gate): a gate is evaluated once.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS preflight_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    gate        INTEGER NOT NULL,             -- 1..12
    name        TEXT    NOT NULL,
    passed      INTEGER NOT NULL CHECK (passed IN (0, 1)),
    detail      TEXT,
    checked_at  TEXT    NOT NULL,
    UNIQUE (session_id, gate)
);

CREATE INDEX IF NOT EXISTS idx_preflight_session ON preflight_checks (session_id);

-- --------------------------------------------------------------------------
-- Decisions: what the strategy saw and what it wanted to do.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL UNIQUE REFERENCES sessions (id) ON DELETE CASCADE,
    trading_date  TEXT    NOT NULL,
    cone_z        REAL,                       -- NULL when the indicator has no value yet
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
    created_at    TEXT    NOT NULL
);

-- --------------------------------------------------------------------------
-- Buckets: deferred re-entry tranches (DESIGN §1.4 bug fix #1 and #2).
-- Declared before lots because lots references bucket_id.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS buckets (
    bucket_id           TEXT    NOT NULL PRIMARY KEY,
    sale_date           TEXT    NOT NULL,
    sale_spy_close      REAL    NOT NULL,
    proceeds            REAL    NOT NULL,
    tranche_amount      REAL    NOT NULL,
    tranches_bought     INTEGER NOT NULL DEFAULT 0,
    last_reference_spy  REAL,
    created_bar         INTEGER NOT NULL,     -- strategy_state.bars at creation
    is_active           INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    expired_at          TEXT,
    created_at          TEXT    NOT NULL
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_buckets_active ON buckets (is_active);

-- --------------------------------------------------------------------------
-- Lots: the ladder book.  One lot == one eToro position (DESIGN §1.1, §4.3),
-- which is why a close is always a full close.  position_id is UNIQUE: it is
-- the key preflight gate 11 reconciles against the broker.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    book                  TEXT    NOT NULL CHECK (book IN ('catapult', 'reentry')),
    symbol                TEXT    NOT NULL,
    position_id           TEXT    UNIQUE,     -- NULL only between submit and fill
    units                 REAL    NOT NULL,
    entry_price           REAL    NOT NULL,
    cost_basis            REAL    NOT NULL,
    entry_date            TEXT    NOT NULL,
    entry_reference_spy   REAL,
    entry_spy_ath         REAL,               -- stamped by the merged _buy_if_allowed
    entry_spy_close       REAL,
    entry_spy_drawdown    REAL,
    entry_tier            INTEGER,
    entry_order_id        INTEGER REFERENCES orders (id),
    bucket_id             TEXT    REFERENCES buckets (bucket_id),
    is_open               INTEGER NOT NULL DEFAULT 1 CHECK (is_open IN (0, 1)),
    exit_date             TEXT,
    exit_price            REAL,
    exit_proceeds         REAL,
    exit_order_id         INTEGER REFERENCES orders (id),
    realized_pnl          REAL,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lots_open   ON lots (book, is_open);
CREATE INDEX IF NOT EXISTS idx_lots_symbol ON lots (symbol, is_open);

-- --------------------------------------------------------------------------
-- Intents: THE idempotency anchor (DESIGN §5).  A row is written, and
-- committed, BEFORE any HTTP call.  request_id is the x-request-id UUID we
-- send, and doubles as eToro's referenceId.  If the response is lost we look
-- the order up by referenceId instead of re-sending it.  UNIQUE on request_id
-- is therefore a correctness constraint, not hygiene.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS intents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    request_id    TEXT    NOT NULL UNIQUE,    -- x-request-id UUID == referenceId
    sequence      INTEGER NOT NULL,           -- order within the session
    symbol        TEXT    NOT NULL,
    instrument_id INTEGER,
    side          TEXT    NOT NULL CHECK (side IN ('buy', 'close')),
    book          TEXT    CHECK (book IN ('catapult', 'reentry', 'core')),
    amount_usd    REAL,                       -- opens are USD-denominated
    units         REAL,                       -- closes are unit-denominated
    position_id   TEXT,                       -- set for side='close'
    lot_id        INTEGER REFERENCES lots (id),
    bucket_id     TEXT    REFERENCES buckets (bucket_id),
    reason        TEXT,
    status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'sent', 'filled', 'rejected',
                                    'recovered', 'abandoned')),
    created_at    TEXT    NOT NULL,
    sent_at       TEXT,
    resolved_at   TEXT,
    UNIQUE (session_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_intents_session ON intents (session_id);
CREATE INDEX IF NOT EXISTS idx_intents_status  ON intents (status);

-- --------------------------------------------------------------------------
-- Orders: what eToro said back.  HTTP 200 means accepted, not filled
-- (DESIGN §1.1), so status is polled to a terminal value:
--   3 = Filled, 4 = Rejected, 5 = PartiallyFilled, 11 = WaitingForMarket.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id           INTEGER NOT NULL UNIQUE REFERENCES intents (id),
    session_id          INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    request_id          TEXT    NOT NULL,     -- mirrors intents.request_id
    order_id            TEXT,                 -- eToro orderId
    reference_id        TEXT,                 -- eToro referenceId (== request_id)
    symbol              TEXT    NOT NULL,
    instrument_id       INTEGER,
    side                TEXT    NOT NULL CHECK (side IN ('buy', 'close')),
    last_status         INTEGER,
    terminal_status     INTEGER,
    status_history_json TEXT    NOT NULL DEFAULT '[]',
    fill_price          REAL,
    fill_units          REAL,
    fill_amount_usd     REAL,
    position_id         TEXT,
    submitted_at        TEXT,
    terminal_at         TEXT,
    error               TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_reference ON orders (reference_id);
CREATE INDEX IF NOT EXISTS idx_orders_orderid   ON orders (order_id);
CREATE INDEX IF NOT EXISTS idx_orders_session   ON orders (session_id);

-- --------------------------------------------------------------------------
-- Strategy state: a singleton row.  CHECK (id = 1) is what makes it one.
-- `bars` is persisted here because a fresh instance each day would otherwise
-- leave _bars stuck at 1 and the strategy stuck in warmup (DESIGN §1.4).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_state (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    bars                 INTEGER NOT NULL DEFAULT 0,
    ath                  REAL,
    below_streak         INTEGER NOT NULL DEFAULT 0,
    recyclable_cash      REAL    NOT NULL DEFAULT 0.0,
    budget_year          INTEGER,
    budget               REAL,
    order_size           REAL,
    cash_target          REAL,
    sale_batch_sequence  INTEGER NOT NULL DEFAULT 0,
    sale_batch_date      TEXT,
    updated_at           TEXT    NOT NULL
);

-- --------------------------------------------------------------------------
-- SCHG daily market value — feeds next year's budget (DESIGN §4.5).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schg_daily_values (
    date       TEXT NOT NULL PRIMARY KEY,
    value      REAL NOT NULL,
    created_at TEXT NOT NULL
) WITHOUT ROWID;

-- --------------------------------------------------------------------------
-- Budget history: every budget the app ever used and where it came from.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS budget_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    year        INTEGER NOT NULL,
    budget      REAL    NOT NULL,
    order_size  REAL    NOT NULL,
    cash_target REAL    NOT NULL,
    source      TEXT    NOT NULL CHECK (source IN ('auto', 'override', 'fallback')),
    note        TEXT,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_budget_history_year ON budget_history (year);

-- --------------------------------------------------------------------------
-- Portfolio snapshots: the account as eToro reported it, per session.
-- available_cash uses eToro's documented formula (credit - pending amounts),
-- because totalBalance is portfolio value, not cash (DESIGN §8).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER REFERENCES sessions (id) ON DELETE CASCADE,
    taken_at       TEXT    NOT NULL,
    equity         REAL,
    cash           REAL,
    available_cash REAL,
    unrealized_pnl REAL,
    realized_pnl   REAL,
    raw_json       TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshots_session ON portfolio_snapshots (session_id);

-- Per-position rows of a snapshot (DESIGN §3 "per-position rows").
CREATE TABLE IF NOT EXISTS portfolio_snapshot_positions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id    INTEGER NOT NULL REFERENCES portfolio_snapshots (id)
                   ON DELETE CASCADE,
    position_id    TEXT    NOT NULL,
    symbol         TEXT,
    instrument_id  INTEGER,
    units          REAL,
    entry_price    REAL,
    current_price  REAL,
    invested       REAL,
    leverage       INTEGER,
    unrealized_pnl REAL,
    open_date      TEXT,
    UNIQUE (snapshot_id, position_id)
);

-- --------------------------------------------------------------------------
-- Notifications: the Telegram send log.  A FAILED alert must itself be
-- visible, which is the whole point of storing status (DESIGN §7.1).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER REFERENCES sessions (id) ON DELETE SET NULL,
    trading_date        TEXT,
    event               TEXT    NOT NULL,   -- session_start|preflight|decision|fill|
                                            -- session_end|abort|heartbeat|command
    body                TEXT    NOT NULL,
    chat_id             TEXT,
    status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
    error               TEXT,
    telegram_message_id TEXT,
    created_at          TEXT    NOT NULL,
    sent_at             TEXT
);

CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications (status);
CREATE INDEX IF NOT EXISTS idx_notifications_date   ON notifications (trading_date);

-- --------------------------------------------------------------------------
-- Commands: every inbound control message received over either channel
-- (DESIGN §6.1 NightwatchMessagingService / §7.2 TelegramPollReceiver).
-- FCM commands carry an HMAC counter; Telegram commands are authorized by
-- chat id only (T019). This table is the full audit of what was received and why it was accepted or
-- rejected, independent of whether it was ultimately actioned.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS commands (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel      TEXT    NOT NULL CHECK (channel IN ('fcm', 'telegram')),
    sender_id    TEXT    NOT NULL,   -- FCM: sending principal; Telegram: chat id
    chat_id      TEXT,               -- Telegram chat id (duplicated for querying)
    command      TEXT    NOT NULL,   -- raw command text, e.g. '/halt'
    counter      INTEGER,            -- HMAC replay counter carried by the message
    verdict      TEXT    NOT NULL CHECK (verdict IN ('accepted', 'rejected')),
    reason       TEXT,               -- rejection reason, or the operator reason for /halt and /resume
    received_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commands_sender
    ON commands (channel, sender_id, counter);
CREATE INDEX IF NOT EXISTS idx_commands_received ON commands (received_at);
