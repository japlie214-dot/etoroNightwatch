-- Migration 008: tp_clears (S7-3a, T028). One row per take-profit clear
-- attempt on a position the buy executor opened. Each row is committed
-- before its PATCH is sent (persist before send); status moves
-- pending -> sent -> cleared | failed. Read-back confirms 'cleared'.
CREATE TABLE IF NOT EXISTS tp_clears (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  position_id TEXT NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','sent','cleared','failed')),
  detail TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tp_clears_position ON tp_clears(position_id);
