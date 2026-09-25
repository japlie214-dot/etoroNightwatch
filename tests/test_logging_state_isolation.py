"""Guards the `_restore_logging_state` autouse fixture in tests/conftest.py.

`configure_logging()` (logging_setup.py) mutates GLOBAL `logging` state --
the `nightwatch` logger's level/handlers/`propagate`, the ROOT logger's
handlers/level, and a few noisy third-party loggers' levels -- with no
teardown of its own. Left unrestored, that pollution leaks from whichever
test calls `configure_logging()` first into every test that runs afterwards
in the same process, and the leak that actually bit
(`tests/test_order_budget.py::test_using_the_placeholder_logs_an_error_not_silence`)
depended on test *order*, which is why it failed under `.venv` but passed
under system Python for the identical suite.

These two tests run in file order (no test-randomization plugin is
installed, see pytest.ini) and together prove the fixture, not a comment,
is what undoes the pollution: the first test deliberately calls
`configure_logging()` and confirms it *did* mutate global state; the second
-- a fresh test, so the autouse fixture has already torn down after the
first -- asserts that mutation is gone. If the autouse fixture in
tests/conftest.py is ever removed or narrowed, this second test fails.
"""

from __future__ import annotations

import logging

from nightwatch.host import DesktopHost
from nightwatch.logging_setup import _NOISY_HTTP_LOGGERS, configure_logging


def test_configure_logging_does_pollute_global_state(host: DesktopHost) -> None:
    """Sanity check: configure_logging() really does mutate global state.

    (If this stops being true, test_global_logging_state_is_restored below
    would pass vacuously.)
    """
    configure_logging(host=host)
    nightwatch_logger = logging.getLogger("nightwatch")
    assert nightwatch_logger.propagate is False
    assert nightwatch_logger.handlers
    assert any(
        logging.getLogger(name).level == logging.WARNING for name in _NOISY_HTTP_LOGGERS
    )


def test_global_logging_state_is_restored() -> None:
    """The autouse fixture must have undone the previous test's pollution."""
    nightwatch_logger = logging.getLogger("nightwatch")
    assert nightwatch_logger.propagate is True
    assert nightwatch_logger.handlers == []
    assert nightwatch_logger.level == logging.NOTSET
    for name in _NOISY_HTTP_LOGGERS:
        assert logging.getLogger(name).level == logging.NOTSET
