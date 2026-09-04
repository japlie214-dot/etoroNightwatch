"""eToro client error hierarchy (DESIGN §1.1, §5).

Every raise site in :mod:`nightwatch.etoro.client` raises one of these, never a
bare ``httpx`` exception, so :mod:`nightwatch.engine.preflight` and
:mod:`nightwatch.engine.executor` do not need to know which HTTP library is
underneath.

**Redaction.** ``x-api-key``/``x-user-key`` values must never appear in a
raised exception's message, because exception text routinely ends up in a
Telegram alert or an ``error`` column, both outside the logging pipeline's
handler-level :class:`~nightwatch.logging_setup.RedactionFilter` (that filter
only scrubs records that pass through a ``logging`` handler; a bare
``str(exc)`` does not). Every constructor here runs its message through
:func:`nightwatch.logging_setup.RedactionFilter.redact` as a second,
independent line of defense, so the same secret-shaped substrings are
scrubbed regardless of whether the text ever reaches a logger.
"""

from __future__ import annotations

from typing import Any, Final

from ..logging_setup import RedactionFilter

__all__ = [
    "EtoroError",
    "EtoroAuthError",
    "EtoroEnvironmentMismatchError",
    "EtoroRateLimitError",
    "EtoroValidationError",
    "EtoroServerError",
    "EtoroNetworkError",
    "EtoroOrderRejected",
    "EtoroEligibilityError",
    "EtoroTimeoutError",
]


def _safe(message: str) -> str:
    """Redact secret-shaped substrings from an exception message."""
    return RedactionFilter.redact(message)


class EtoroError(Exception):
    """Base class for every error this client raises."""

    def __init__(self, message: str, *args: Any) -> None:
        super().__init__(_safe(str(message)), *args)


class EtoroAuthError(EtoroError):
    """``GET /balances`` did not return 200 (preflight gate 6), or a 401/403."""


class EtoroEnvironmentMismatchError(EtoroAuthError):
    """Config says one environment but the key is bound to the other (gate 7).

    This must be *detected*, not assumed (DESIGN §6.F): the client probes the
    API and compares what it learns about the credential's own environment
    against :attr:`EtoroConfig.environment`, rather than trusting that the
    caller configured things correctly.
    """

    def __init__(self, configured: str, detected: str) -> None:
        super().__init__(
            f"eToro environment mismatch: config says {configured!r} but the "
            f"credential appears to be bound to {detected!r}"
        )
        self.configured = configured
        self.detected = detected


class EtoroRateLimitError(EtoroError):
    """The documented per-user-key 1-minute window was exceeded (HTTP 429).

    Carries ``retry_after`` (seconds) when the API supplies one, via the
    ``Retry-After`` header or the response body.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class EtoroValidationError(EtoroError):
    """Any 4xx other than 401/403/429 — malformed request, bad params, etc.

    Never retried (DESIGN's retry policy): retrying a malformed request only
    burns rate-limit budget on a call that can never succeed unchanged.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class EtoroServerError(EtoroError):
    """A 5xx response. Retried with bounded exponential backoff."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class EtoroNetworkError(EtoroError):
    """DNS/TCP/TLS failure, connection reset, or a request timeout.

    Preflight gate 3 treats this as "no network to public-api.etoro.com".
    """


class EtoroTimeoutError(EtoroNetworkError):
    """A request (or a poll-to-terminal loop) exceeded its configured timeout."""


class EtoroOrderRejected(EtoroError):
    """An order reached terminal status 4 (Rejected) or 10 (RejectedPartiallyFilled)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class EtoroEligibilityError(EtoroError):
    """``allowOpenPosition`` is false, or the minimum size exceeds order_size.

    Per DESIGN §4.3: this blocks and alerts loudly. It is never silently
    resized — a silent bump would break the tier-cap arithmetic the whole
    strategy rests on.
    """


#: Status codes that unambiguously mean "do not retry this request as-is".
NON_RETRYABLE_STATUS_PREFIX: Final = 4
