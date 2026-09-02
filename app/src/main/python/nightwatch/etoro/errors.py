"""eToro client error hierarchy (DESIGN §1.1, §5).

# TODO(step 2): Define an exception hierarchy that preflight and the executor
# can catch specifically, at least:
#   - EtoroError (base)
#   - EtoroAuthError        (gate 6/7: GET /balances not 200, or env mismatch)
#   - EtoroRateLimitError   (per-user-key 1-min window exceeded; carry
#                            retry-after if the API supplies it)
#   - EtoroNetworkError     (gate 3: DNS/HTTPS unreachable)
#   - EtoroOrderRejected    (terminal status 4)
#   - EtoroEligibilityError (gate 8: allowOpenPosition false, or minimum size
#                            exceeds order_size — block and alert, never resize)
# Every raise site in etoro/client.py should raise one of these, never a bare
# requests/httpx exception, so engine/preflight.py and engine/executor.py do
# not need to know which HTTP library is underneath.
"""
