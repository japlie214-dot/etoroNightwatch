"""Bar seeding, provisional-write, and settle (DESIGN §1.4, §4.2). Built in step 3.

# TODO(step 3): Implement:
#   - seed(symbol, count=400) -> int   Fetch OneDay candles via etoro.client and
#     upsert via db.repo.Repo.upsert_bars, source='seed'. DESIGN §1.4: seed 400
#     bars so the 260-bar cone window is full from day one.
#   - write_provisional(symbol, date, live_rate) -> None   Step [4] of the
#     session: write today's bar from the live rate, is_provisional=1,
#     source='live_rate' (DESIGN §4.1, §4.2).
#   - settle_previous(symbol) -> bool   Step [2] of the session: fetch the
#     official OneDay candle for the last trading date and call
#     db.repo.Repo.settle_bar, flipping is_provisional to 0 (DESIGN §4.2). Any
#     bar still provisional afterward is a preflight gate 10 failure — that
#     check itself lives in engine/preflight.py, this module just performs the
#     settle.
# All of this is a thin layer over db.repo.Repo + etoro.client — no SQL here.
"""
