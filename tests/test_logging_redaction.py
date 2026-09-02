"""Log redaction: secrets must never reach a log file (task brief §3)."""

from __future__ import annotations

import io
import logging

import pytest

from nightwatch.host import DesktopHost
from nightwatch.logging_setup import (
    RedactionFilter,
    configure_logging,
    harden_third_party_logging,
    log_path_for_today,
)


@pytest.fixture(autouse=True)
def _clean_root_logger():
    """Root logger state leaks across tests via the stdlib's global registry.

    Snapshot and restore it around every test in this module so a handler
    (or level, or the loggers this module quiets) one test installs on root
    doesn't bleed into the next.
    """
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    other_levels = {
        name: logging.getLogger(name).level for name in ("httpx", "httpcore", "urllib3")
    }
    try:
        yield
    finally:
        for handler in list(root_logger.handlers):
            if handler not in original_handlers:
                root_logger.removeHandler(handler)
                handler.close()
        root_logger.setLevel(original_level)
        for name, lvl in other_levels.items():
            logging.getLogger(name).setLevel(lvl)


def test_redaction_filter_scrubs_known_secret_shapes() -> None:
    github_pat = "github_pat_" + "a" * 60
    telegram_token = "123456789:AA" + "b" * 33
    generic_hex = "f" * 40
    api_key_kv = "ETORO_API_KEY=abcdef0123456789ABCDEF0123456789"

    text = (
        f"leaked pat={github_pat} token={telegram_token} "
        f"hex={generic_hex} kv={api_key_kv}"
    )
    redacted = RedactionFilter.redact(text)

    assert github_pat not in redacted
    assert telegram_token not in redacted
    assert generic_hex not in redacted
    assert "abcdef0123456789ABCDEF0123456789" not in redacted
    assert "***REDACTED***" in redacted


def test_redaction_filter_leaves_ordinary_text_alone() -> None:
    text = "session started for 2026-01-02, mode=dry_run, cone_z=-2.5"
    assert RedactionFilter.redact(text) == text


def test_configured_logger_never_writes_a_fake_token_to_disk(host: DesktopHost) -> None:
    logger = configure_logging(level=logging.INFO, host=host)
    fake_bot_token = "987654321:AAHfakeTelegramBotTokenValueForTestingOnly1234"
    fake_pat = "github_pat_" + "z" * 60

    logger.info("startup ok, telegram token=%s, pat=%s", fake_bot_token, fake_pat)
    for handler in logger.handlers:
        handler.flush()

    log_path = log_path_for_today(host)
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")

    assert fake_bot_token not in contents
    assert fake_pat not in contents
    assert "REDACTED" in contents

    for handler in list(logger.handlers):
        handler.close()
    for handler in list(logging.getLogger().handlers):
        if getattr(handler, "_nightwatch_owned_handler", False):
            handler.close()


def test_third_party_logger_is_redacted_via_root(host: DesktopHost) -> None:
    """httpx (or any other third-party logger) propagates to root, not to the
    `nightwatch` logger's handlers. configure_logging() must still redact it,
    since httpx is the eToro client's transport and can log request lines
    containing the x-api-key / x-user-key auth headers verbatim.
    """
    configure_logging(level=logging.INFO, host=host)

    third_party_logger = logging.getLogger("httpx")
    third_party_logger.setLevel(logging.INFO)  # override the WARNING default for this test
    fake_user_key = "x-user-key=" + "e" * 40
    fake_pat = "github_pat_" + "q" * 60

    third_party_logger.info("GET https://api.example/x?%s pat=%s", fake_user_key, fake_pat)

    for handler in logging.getLogger().handlers:
        handler.flush()

    log_path = log_path_for_today(host)
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")

    assert fake_user_key not in contents
    assert "e" * 40 not in contents
    assert fake_pat not in contents
    assert "REDACTED" in contents

    for handler in list(logging.getLogger().handlers):
        if getattr(handler, "_nightwatch_owned_handler", False):
            handler.close()


def test_pre_existing_root_handler_is_also_redacted(host: DesktopHost) -> None:
    """A handler some other code attached to root *before* configure_logging()
    ran (simulated here with a StreamHandler to a StringIO) must still get
    scrubbed — configure_logging() filters handlers it does not own rather
    than only handling the ones it creates itself.
    """
    buffer = io.StringIO()
    other_code_handler = logging.StreamHandler(stream=buffer)
    logging.getLogger().addHandler(other_code_handler)

    configure_logging(level=logging.INFO, host=host)

    fake_pat = "github_pat_" + "r" * 60
    logging.getLogger("some_other_lib").warning("oops leaked pat=%s", fake_pat)

    other_code_handler.flush()
    output = buffer.getvalue()

    assert fake_pat not in output
    assert "REDACTED" in output

    for handler in list(logging.getLogger().handlers):
        if getattr(handler, "_nightwatch_owned_handler", False):
            handler.close()


def test_configure_logging_twice_does_not_duplicate_or_remove_foreign_root_handlers(
    host: DesktopHost,
) -> None:
    """Calling configure_logging() twice must not stack owned handlers on
    root, and must never remove a handler this module did not create.
    """
    foreign_handler = logging.StreamHandler(stream=io.StringIO())
    logging.getLogger().addHandler(foreign_handler)

    configure_logging(level=logging.INFO, host=host)
    owned_after_first = [
        h for h in logging.getLogger().handlers if getattr(h, "_nightwatch_owned_handler", False)
    ]
    assert len(owned_after_first) == 1

    configure_logging(level=logging.INFO, host=host)
    root_handlers = logging.getLogger().handlers
    owned_after_second = [
        h for h in root_handlers if getattr(h, "_nightwatch_owned_handler", False)
    ]

    assert len(owned_after_second) == 1  # not duplicated
    assert foreign_handler in root_handlers  # not removed

    for handler in list(logging.getLogger().handlers):
        if getattr(handler, "_nightwatch_owned_handler", False):
            handler.close()


def test_harden_third_party_logging_is_idempotent(host: DesktopHost) -> None:
    """Calling harden_third_party_logging() repeatedly must not add the
    filter more than once per handler (which would double-redact harmlessly
    but is still worth pinning down) and must not raise.
    """
    configure_logging(level=logging.INFO, host=host)
    harden_third_party_logging()
    harden_third_party_logging()

    for handler in logging.getLogger().handlers:
        redaction_filters = [f for f in handler.filters if isinstance(f, RedactionFilter)]
        assert len(redaction_filters) == 1

    for handler in list(logging.getLogger().handlers):
        if getattr(handler, "_nightwatch_owned_handler", False):
            handler.close()
