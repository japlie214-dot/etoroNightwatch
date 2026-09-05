-- Migration 005: lot_annotations.entry_context_source
-- (HANDOFF_2026-09-05_evening.md §3a, DESIGN_market_data.md §4.4, §4.7).
--
-- §3a's ATH-floor/adopt-halt work distinguishes a lot whose SPY entry
-- context was genuinely measured (every row market.adopt writes today --
-- an out-of-window position is refused before any write, never adopted
-- with a guess) from one whose context would have been ASSUMED because its
-- open_date predates the fetched SPY window entirely (§3a Case 3: HALT is
-- the only behaviour built for that case today; this column exists so a
-- future override, if one is ever added, has somewhere honest to record
-- that it guessed -- and per §3a's own reasoning, such an override must
-- assign tier 0, not tier 1).
--
-- Provenance only, exactly like the existing `origin` column (§4.4) -- never
-- an access gate. Every row written before this migration (there are none
-- live yet; the adoption migration has never been run against the real
-- account, HANDOFF_2026-09-05_evening.md §6 item 8) and every row `origin
-- ='app'`/`'adopted'` writes today gets 'derived': a value genuinely
-- computed from measured history or a live stamped close, never guessed.
-- 'assumed_out_of_window' is reserved for the not-yet-built override path
-- and is never produced by this codebase today.
ALTER TABLE lot_annotations
    ADD COLUMN entry_context_source TEXT NOT NULL DEFAULT 'derived'
        CHECK (entry_context_source IN ('derived', 'assumed_out_of_window'));
