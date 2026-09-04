"""Tests for nightwatch.etoro.auth (DESIGN §1.1).

The header-building logic is trivial; the point of this file is the
redaction claim -- verified against the real logging pipeline with an actual
captured log file, per HANDOFF.md's rule that "a check that passes because
nothing happened is not a pass."
"""

from __future__ import annotations

import logging
import uuid

from nightwatch.config import Credentials
from nightwatch.etoro import auth
from nightwatch.host import DesktopHost
from nightwatch.logging_setup import configure_logging, log_path_for_today

FAKE_API_KEY = "ak_live_SuperSecretApiKeyValue123456"
FAKE_USER_KEY = "uk_live_SuperSecretUserKeyValue654321"


def _credentials() -> Credentials:
    return Credentials(label="demo:write", api_key=FAKE_API_KEY, user_key=FAKE_USER_KEY)


# -- header assembly ----------------------------------------------------------


def test_build_headers_contains_the_three_required_headers() -> None:
    headers = auth.build_headers(_credentials())
    assert headers[auth.API_KEY_HEADER] == FAKE_API_KEY
    assert headers[auth.USER_KEY_HEADER] == FAKE_USER_KEY
    assert auth.REQUEST_ID_HEADER in headers
    # a real UUID4, not a placeholder
    uuid.UUID(headers[auth.REQUEST_ID_HEADER])


def test_build_headers_uses_caller_supplied_request_id_when_given() -> None:
    my_id = str(uuid.uuid4())
    headers = auth.build_headers(_credentials(), request_id=my_id)
    assert headers[auth.REQUEST_ID_HEADER] == my_id


def test_build_headers_mints_fresh_id_each_call_when_omitted() -> None:
    creds = _credentials()
    first = auth.build_headers(creds)[auth.REQUEST_ID_HEADER]
    second = auth.build_headers(creds)[auth.REQUEST_ID_HEADER]
    assert first != second


def test_new_request_id_is_a_valid_uuid4_string() -> None:
    value = auth.new_request_id()
    parsed = uuid.UUID(value)
    assert parsed.version == 4


# -- credentials never leak via repr/str -------------------------------------


def test_credentials_repr_never_contains_secret_values() -> None:
    creds = _credentials()
    text = repr(creds)
    assert FAKE_API_KEY not in text
    assert FAKE_USER_KEY not in text


# -- redaction: verified against a real captured log, not assumed -----------


def test_header_values_never_survive_the_real_logging_pipeline(
    host: DesktopHost,
) -> None:
    """Actually log the built header dict and assert the secrets are gone
    from the resulting file on disk -- not a structural argument, a real
    capture (HANDOFF.md: "verify redaction claims, don't just assume them").
    """
    configure_logging(host=host)
    try:
        logger = logging.getLogger("nightwatch.etoro.auth.test")
        headers = auth.build_headers(_credentials())
        # Simulate the worst case: a call site that (against the module's own
        # advice) logs the whole header dict directly, defeating line-of-
        # defense #1 (Credentials.repr suppression) entirely. Line of defense
        # #2 (the handler-level RedactionFilter) must still catch it.
        logger.info("outgoing request headers: %s", headers)
    finally:
        logging.shutdown()

    log_path = log_path_for_today(host)
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")
    assert FAKE_API_KEY not in contents
    assert FAKE_USER_KEY not in contents
    assert "REDACTED" in contents


def test_header_values_never_leak_via_an_etoro_error_message(host: DesktopHost) -> None:
    """A call site that builds an EtoroError message by interpolating the
    headers dict must not leak the values either -- this is the second,
    independent line of defense nightwatch.etoro.errors provides for text
    that never passes through a logging handler at all (e.g. a Telegram
    alert body built straight from ``str(exc)``). Exercised as a real
    raised-and-caught exception, not asserted structurally.
    """
    from nightwatch.etoro.errors import EtoroError

    headers = auth.build_headers(_credentials())
    try:
        raise EtoroError(f"request failed, headers were {headers}")
    except EtoroError as exc:
        text = str(exc)
    assert FAKE_API_KEY not in text
    assert FAKE_USER_KEY not in text
