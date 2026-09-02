"""The ported Std Band Ladder Cash Recycle strategy (DESIGN §1.4). Built in
step 4, against etoro_trader fixtures, with the four approved bug fixes:

# TODO(step 4): Port
# `etoro_trader/harness/strategies/std_band_ladder_cash_recycle.py` with:
#   1. Merge the duplicate `_buy_if_allowed` (keep the L143 gate logic, but
#      apply the G-F budget ceiling, call the parent gate, THEN stamp
#      entry_spy_ath/entry_spy_close/entry_spy_drawdown and decrement
#      _recyclable_cash) — this is what makes re-entry buckets get created at
#      all.
#   2. Rewrite `_process_reentry_buckets` as a single pass: drop expired, fill
#      at most one, then filter — the original duplicates buckets on a fill
#      bar.
#   3. Replace `_reconcile_lots`: load persisted lot rows from db.repo (never
#      fabricate a lot for an unmatched broker position — that is a preflight
#      gate 11 failure, surfaced by engine/reconcile.py, not papered over
#      here).
#   4. `_ensure_budget`: on an empty prior year, fall back to
#      core_fraction x current equity with a loud Telegram warning instead of
#      raising (a live app restarting Jan 2 must not crash).
# Dropped, do not port: CASH_BAND_FRACTION, MARTINGALE_RATIO (both inert;
# DESIGN §1.4 "Dropped parameters"). Tier caps come from strategy/params.py.
# decide(ctx) -> list[OrderIntent] is the entry point engine/session.py calls.
"""
