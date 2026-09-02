"""The eToro HTTP client (DESIGN §1.1, §4.3). Built in step 2, against demo.

# TODO(step 2): Implement a client wrapping every endpoint this design uses:
#   - GET  /balances                                   (gate 6 auth probe)
#   - POST /api/v2/trading/info/eligibility             (gate 8)
#   - GET  /api/v1/market-data/instruments/search        (resolve instrument ids)
#   - GET  /api/v1/market-data/instruments/{id}/history/candles/{dir}/{interval}/{count}
#   - GET  /api/v1/market-data/instruments/rates?instrumentIds=...
#   - POST /api/v2/trading/execution/orders  and  .../demo/orders   (DESIGN §4.3)
#   - POST /api/v1/trading/execution/market-close-orders/positions/{positionId}
#   - GET  /api/v2/trading/info/orders:lookup            (poll to terminal status)
#   - GET  /trading/info/trade/history                   (webapp trade history,
#          capped at just under 1 year per request per DESIGN §8 — page + cache)
# Requirements carried over from config.py's design:
#   - Never build a URL by hand-concatenating "demo"; use
#     EtoroConfig.orders_path() / .path_segment so demo/real cannot drift from
#     the bound credential set (DESIGN §1.1).
#   - Attach headers via etoro/auth.py; rate-limit via etoro/ratelimit.py;
#     raise only etoro/errors.py exceptions.
#   - HTTP 200 means accepted, not filled — every order call needs a
#     poll-to-terminal helper backed by orders:lookup (DESIGN §1.1).
#   - Compare the response's Date header against local time for preflight
#     gate 4 (clock skew > 60s).
# Prefer stdlib urllib or a single pinned dependency (see requirements.txt) —
# whatever is chosen must run unmodified under Chaquopy.
"""
