"""Client-side rate limiting (DESIGN §1.1, §6.H).

eToro's documented per-user-key, 1-minute windows:

    reads          60/min
    writes         20/min
    market_data   120/min
    eligibility    20/min  (dedicated bucket, separate from reads)

This is a token bucket per request class, refilling continuously (not a hard
reset every 60s) so a burst right at the minute boundary cannot double up.
It also reconciles against ``RateLimit-*`` response headers when the API
supplies them, because a local bucket can drift from the server's own count
(a prior process, a webapp reader sharing the same key, clock skew) — the
server is always the more authoritative source when it tells us something.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Final, Literal, Mapping

__all__ = [
    "RequestClass",
    "RATE_LIMITS",
    "TokenBucket",
    "RateLimiter",
]

RequestClass = Literal["read", "write", "market_data", "eligibility"]

#: (capacity, window_seconds) per request class (DESIGN §1.1).
RATE_LIMITS: Final[dict[RequestClass, tuple[int, float]]] = {
    "read": (60, 60.0),
    "write": (20, 60.0),
    "market_data": (120, 60.0),
    "eligibility": (20, 60.0),
}


class TokenBucket:
    """A single continuously-refilling token bucket. Thread-safe.

    Not eToro-specific: this is the generic primitive, exercised directly by
    tests, then wired up per request class by :class:`RateLimiter`.
    """

    def __init__(
        self,
        capacity: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.capacity = capacity
        self.window_seconds = window_seconds
        self._refill_rate = capacity / window_seconds  # tokens per second
        self._clock = clock
        self._tokens = float(capacity)
        self._last_refill = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(self.capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now

    def available(self) -> float:
        """Return the current token count without consuming any (for callers
        that want to check "would this be allowed right now" — DESIGN §8's
        webapp read-key throttling)."""
        with self._lock:
            self._refill()
            return self._tokens

    def try_acquire(self, cost: float = 1.0) -> bool:
        """Attempt to consume ``cost`` tokens. Returns whether it succeeded."""
        with self._lock:
            self._refill()
            if self._tokens >= cost:
                self._tokens -= cost
                return True
            return False

    def time_until_available(self, cost: float = 1.0) -> float:
        """Seconds until ``cost`` tokens will be available (0 if already so)."""
        with self._lock:
            self._refill()
            deficit = cost - self._tokens
            if deficit <= 0:
                return 0.0
            return deficit / self._refill_rate

    def set_remaining(self, remaining: float) -> None:
        """Reconcile the local bucket against a server-reported remaining count.

        Only ever *tightens* the local view (takes the minimum): a server
        that reports fewer tokens than we think we have means some other
        consumer of the same key used some, and we must not act as if we
        still have them. A server reporting *more* than we locally believe is
        never used to top the bucket back up — that would defeat the purpose
        of a client-side limiter guarding against our own bursts.
        """
        with self._lock:
            self._refill()
            self._tokens = max(0.0, min(self._tokens, remaining))


@dataclass(frozen=True, slots=True)
class _HeaderNames:
    """The ``RateLimit-*`` header names this reconciles against.

    Kept as data (not string literals scattered through the code) so a test
    can drive reconciliation without depending on eToro's exact header
    casing being guessed right everywhere at once.
    """

    remaining: str = "RateLimit-Remaining"
    limit: str = "RateLimit-Limit"
    reset: str = "RateLimit-Reset"
    retry_after: str = "Retry-After"


HEADER_NAMES: Final = _HeaderNames()


class RateLimiter:
    """One :class:`TokenBucket` per eToro request class (DESIGN §1.1).

    Usage from :mod:`nightwatch.etoro.client`::

        if not limiter.try_acquire("write"):
            time.sleep(limiter.time_until_available("write"))
        response = ...
        limiter.reconcile(response_class, response.headers)
    """

    def __init__(
        self,
        limits: dict[RequestClass, tuple[int, float]] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        effective = limits or RATE_LIMITS
        self._buckets: dict[RequestClass, TokenBucket] = {
            request_class: TokenBucket(capacity, window, clock=clock)
            for request_class, (capacity, window) in effective.items()
        }

    def bucket(self, request_class: RequestClass) -> TokenBucket:
        return self._buckets[request_class]

    def try_acquire(self, request_class: RequestClass, cost: float = 1.0) -> bool:
        return self._buckets[request_class].try_acquire(cost)

    def time_until_available(self, request_class: RequestClass, cost: float = 1.0) -> float:
        return self._buckets[request_class].time_until_available(cost)

    def would_allow(self, request_class: RequestClass, cost: float = 1.0) -> bool:
        """Check without consuming — the interface DESIGN §8 needs for the
        webapp's read-only key to ask "would this be allowed right now"."""
        return self._buckets[request_class].available() >= cost

    def reconcile(self, request_class: RequestClass, headers: Mapping[str, str]) -> None:
        """Tighten the local bucket using ``RateLimit-Remaining`` if present.

        Silently does nothing if the header is absent or unparsable — eToro's
        header presence is not guaranteed on every response, and this must
        never be the reason a request fails.
        """
        raw = headers.get(HEADER_NAMES.remaining)
        if raw is None:
            return
        try:
            remaining = float(raw)
        except (TypeError, ValueError):
            return
        self._buckets[request_class].set_remaining(remaining)

    @staticmethod
    def retry_after_from_headers(headers: Mapping[str, str]) -> float | None:
        """Parse ``Retry-After`` (seconds) from a 429 response, if present."""
        raw = headers.get(HEADER_NAMES.retry_after)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
