-- Migration 004: price_window_fingerprints + corporate_actions
-- (DESIGN_market_data.md §3.4, §7; build step 3/4, §12).
--
-- Neither table stores a price series. `bars`/`lots` are NOT dropped by
-- this migration (DESIGN_market_data.md §12 step 3's "drop bars" is not
-- executed in this change -- see this task's final report for why: it is a
-- separate, cross-cutting change touching engine/session.py and several
-- existing test suites, out of scope for the window-assembly build this
-- migration accompanies). This migration only ADDS the two new tables §7
-- specifies, so it is purely additive and safe alongside the still-present
-- legacy tables.
--
-- price_window_fingerprints (§3.4): the cheap recovery for the property
-- fetch-derive-discard otherwise gives up -- "we cannot compare today's
-- fetch against yesterday's, so a silent upstream revision of history
-- becomes undetectable." A SHA-256 over the (date, adjClose) series, a
-- few dozen bytes, restores revision detection without storing prices.
-- Written by market.window.compute_fingerprint's caller, never by this
-- migration itself.
CREATE TABLE IF NOT EXISTS price_window_fingerprints (
    trading_date      TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    window_start      TEXT NOT NULL,
    window_end        TEXT NOT NULL,
    bar_count         INTEGER NOT NULL,
    series_sha256     TEXT NOT NULL,
    source            TEXT NOT NULL,
    source_fetched_at TEXT NOT NULL,
    PRIMARY KEY (trading_date, symbol)
) WITHOUT ROWID;

-- corporate_actions (§2.2, §4.6, §7): tripwire only, NOT a price- or
-- lot-adjustment input. Dividends and splits are first-class equals here --
-- the generalization activity_server never made (§2.2) -- and this table's
-- only job is signalling a session to expect position_id churn (§4.6b) and
-- feeding the ex-dividend seam correction (§8). Neither of those consumers
-- is built by this task (they belong to gate 10 item 4, DESIGN_market_data
-- .md §12 step 5); the table is added now, empty and unread, because §7
-- specifies it alongside price_window_fingerprints under the same "Added"
-- heading and there is no reason to defer purely additive DDL that a later
-- step will need regardless.
CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol        TEXT NOT NULL,
    date          TEXT NOT NULL,
    split_factor  REAL NOT NULL DEFAULT 1,
    div_cash      REAL NOT NULL DEFAULT 0,
    source        TEXT NOT NULL,
    detected_at   TEXT NOT NULL,
    processed_at  TEXT,
    PRIMARY KEY (symbol, date)
) WITHOUT ROWID;
