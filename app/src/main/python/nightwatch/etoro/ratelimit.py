"""Client-side rate limiting (DESIGN §1.1).

# TODO(step 2): eToro's documented per-user-key, 1-minute windows are:
#   reads 60/min, writes 20/min, market-data 120/min, eligibility 20/min
#   (dedicated bucket).
# Implement a simple token-bucket or sliding-window limiter keyed by request
# class ('read'|'write'|'market_data'|'eligibility'), used by
# etoro/client.py before every call. Also the mechanism the webapp's
# read-only key is throttled through during the trading window (DESIGN §8),
# so the interface should support an external caller checking "would this be
# allowed right now" without necessarily consuming a slot.
"""
