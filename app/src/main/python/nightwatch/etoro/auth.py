"""eToro authentication headers (DESIGN §1.1).

Auth is two static headers, ``x-api-key`` and ``x-user-key``, plus a fresh
``x-request-id`` UUID per request. No signing, no nonce, no JWT (DESIGN
§1.1) — this module's only job is to assemble the three headers consistently
and never let a secret value leak into a log line.

Two independent lines of defense against that leak:

1. :class:`~nightwatch.config.Credentials` already suppresses ``repr()`` on
   its ``api_key``/``user_key`` fields, so a stray ``repr(credentials)`` in a
   log call or a traceback stays empty.
2. :mod:`nightwatch.logging_setup` installs a handler-level
   :class:`~nightwatch.logging_setup.RedactionFilter` that scrubs
   secret-shaped substrings from every formatted record, so even a call site
   that logs the *header dict itself* (defeating line 1) gets scrubbed before
   it reaches disk or stdout.

Neither is assumed to be sufficient on its own — see
``tests/test_etoro_auth.py`` for a test that actually logs a header dict
through the real logging pipeline and asserts the secret values do not
survive, rather than trusting that the design intent holds.
"""

from __future__ import annotations

import uuid

from ..config import Credentials

__all__ = ["REQUEST_ID_HEADER", "API_KEY_HEADER", "USER_KEY_HEADER", "build_headers", "new_request_id"]

API_KEY_HEADER = "x-api-key"
USER_KEY_HEADER = "x-user-key"
REQUEST_ID_HEADER = "x-request-id"


def new_request_id() -> str:
    """Return a fresh UUID4 string.

    Exposed separately from :func:`build_headers` because, for write calls,
    the idempotency anchor (DESIGN §5/§6.H) requires the id to be generated
    and persisted to the ``intents`` table *before* any HTTP call — the
    client accepts a caller-supplied id for those calls rather than
    generating its own. This helper exists for read calls (where no prior
    persistence is needed) and for the caller that does the persisting.
    """
    return str(uuid.uuid4())


def build_headers(credentials: Credentials, request_id: str | None = None) -> dict[str, str]:
    """Return the three headers eToro requires for one HTTP request.

    Args:
        credentials: The ``x-api-key``/``x-user-key`` pair for the request's
            role (write or read), sourced from
            :class:`~nightwatch.config.EtoroConfig` — never constructed by
            hand from raw strings elsewhere in the app.
        request_id: A pre-generated UUID for a write call (DESIGN §5/§6.H —
            the caller generates and persists this before calling); a fresh
            one is minted via :func:`new_request_id` if omitted, which is
            only appropriate for read calls that have no idempotency
            requirement.
    """
    headers = dict(credentials.as_headers())
    headers[REQUEST_ID_HEADER] = request_id or new_request_id()
    return headers
