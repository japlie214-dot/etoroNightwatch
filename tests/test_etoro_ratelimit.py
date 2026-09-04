"""Tests for nightwatch.etoro.ratelimit (DESIGN §1.1, §6.H)."""

from __future__ import annotations

from nightwatch.etoro.ratelimit import RATE_LIMITS, RateLimiter, TokenBucket

# -- TokenBucket: local bucket behavior --------------------------------------


def test_bucket_starts_full() -> None:
    bucket = TokenBucket(capacity=5, window_seconds=60.0, clock=lambda: 0.0)
    assert bucket.available() == 5


def test_bucket_try_acquire_consumes_a_token() -> None:
    bucket = TokenBucket(capacity=2, window_seconds=60.0, clock=lambda: 0.0)
    assert bucket.try_acquire() is True
    assert bucket.available() == 1
    assert bucket.try_acquire() is True
    assert bucket.available() == 0


def test_bucket_denies_when_empty() -> None:
    bucket = TokenBucket(capacity=1, window_seconds=60.0, clock=lambda: 0.0)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False


def test_bucket_refills_continuously_over_time() -> None:
    now = [0.0]
    bucket = TokenBucket(capacity=60, window_seconds=60.0, clock=lambda: now[0])
    for _ in range(60):
        assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False
    # Half the window has passed -> roughly half the capacity refilled.
    now[0] = 30.0
    assert 29.0 <= bucket.available() <= 31.0


def test_bucket_time_until_available_when_empty() -> None:
    bucket = TokenBucket(capacity=60, window_seconds=60.0, clock=lambda: 0.0)
    for _ in range(60):
        bucket.try_acquire()
    # 1 token needed, refill rate is 1/sec -> ~1 second.
    wait = bucket.time_until_available()
    assert 0.9 <= wait <= 1.1


def test_bucket_time_until_available_is_zero_when_tokens_present() -> None:
    bucket = TokenBucket(capacity=5, window_seconds=60.0, clock=lambda: 0.0)
    assert bucket.time_until_available() == 0.0


def test_bucket_never_exceeds_capacity_after_long_idle() -> None:
    now = [0.0]
    bucket = TokenBucket(capacity=10, window_seconds=60.0, clock=lambda: now[0])
    bucket.try_acquire()
    now[0] = 10_000.0  # a very long time later
    assert bucket.available() == 10


def test_bucket_rejects_nonpositive_capacity() -> None:
    import pytest

    with pytest.raises(ValueError):
        TokenBucket(capacity=0, window_seconds=60.0)


# -- reconciliation against RateLimit-* headers ------------------------------


def test_set_remaining_tightens_the_local_bucket() -> None:
    bucket = TokenBucket(capacity=60, window_seconds=60.0, clock=lambda: 0.0)
    # Bucket believes it has 60 tokens; server says only 5 remain (some other
    # consumer of the same key has been active).
    bucket.set_remaining(5)
    assert bucket.available() == 5


def test_set_remaining_never_tops_up_above_local_belief() -> None:
    bucket = TokenBucket(capacity=60, window_seconds=60.0, clock=lambda: 0.0)
    bucket.try_acquire(cost=50)  # 10 tokens left locally
    # Server claims 55 remain -- must NOT be trusted upward; local view wins.
    bucket.set_remaining(55)
    assert bucket.available() == 10


def test_set_remaining_floors_at_zero() -> None:
    bucket = TokenBucket(capacity=10, window_seconds=60.0, clock=lambda: 0.0)
    bucket.set_remaining(-5)
    assert bucket.available() == 0


# -- RateLimiter: per-class buckets, DESIGN's documented limits --------------


def test_default_limits_match_design() -> None:
    assert RATE_LIMITS["read"] == (60, 60.0)
    assert RATE_LIMITS["write"] == (20, 60.0)
    assert RATE_LIMITS["market_data"] == (120, 60.0)
    assert RATE_LIMITS["eligibility"] == (20, 60.0)


def test_eligibility_bucket_is_dedicated_not_shared_with_reads() -> None:
    limiter = RateLimiter(clock=lambda: 0.0)
    # Exhaust the eligibility bucket entirely.
    for _ in range(20):
        assert limiter.try_acquire("eligibility") is True
    assert limiter.try_acquire("eligibility") is False
    # Reads are a completely separate bucket -- unaffected.
    assert limiter.try_acquire("read") is True


def test_would_allow_does_not_consume() -> None:
    limiter = RateLimiter(clock=lambda: 0.0)
    assert limiter.would_allow("write") is True
    assert limiter.would_allow("write") is True  # calling it again changes nothing
    # Still full capacity available to actually consume.
    for _ in range(20):
        assert limiter.try_acquire("write") is True
    assert limiter.try_acquire("write") is False


def test_reconcile_from_headers_tightens_bucket() -> None:
    limiter = RateLimiter(clock=lambda: 0.0)
    limiter.reconcile("read", {"RateLimit-Remaining": "3"})
    assert limiter.bucket("read").available() == 3


def test_reconcile_ignores_missing_header() -> None:
    limiter = RateLimiter(clock=lambda: 0.0)
    limiter.reconcile("read", {})  # no header present -- must not raise
    assert limiter.bucket("read").available() == 60


def test_reconcile_ignores_unparsable_header() -> None:
    limiter = RateLimiter(clock=lambda: 0.0)
    limiter.reconcile("read", {"RateLimit-Remaining": "not-a-number"})
    assert limiter.bucket("read").available() == 60


def test_retry_after_parsed_from_headers() -> None:
    retry_after = RateLimiter.retry_after_from_headers({"Retry-After": "12"})
    assert retry_after == 12.0


def test_retry_after_missing_returns_none() -> None:
    assert RateLimiter.retry_after_from_headers({}) is None


def test_retry_after_unparsable_returns_none() -> None:
    assert RateLimiter.retry_after_from_headers({"Retry-After": "soon"}) is None
