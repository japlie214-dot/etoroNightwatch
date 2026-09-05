"""The ``cone_5_z`` signal (DESIGN.md §1.4), computed over the window
DESIGN_market_data.md §3 assembles (build step 4 of §12: "fetch-derive-
discard").

    cone_5_z[t] = (C[t]/C[t-5] - 1) / stdev_pop( C[i]/C[i-5] - 1 , i = t-259..t )

``t`` is always the *last* element of the ``closes`` sequence handed in --
this module has no notion of "today" or a calendar; it is a pure function
over whatever price series the caller assembled. DESIGN_market_data.md §3.1
is explicit about what that series is in production: Tiingo's adjusted daily
SPY closes with today's provisional close (Tiingo IEX ``tngoLast``, or the
eToro-candle fallback) spliced on as the final element -- ``market.window``
is what does that splicing; this module only ever sees the resulting flat
list of numbers.

First valid value needs 265 bars (260-bar lookback + a 5-bar shift) --
:data:`REQUIRED_HISTORY_BARS` mirrors ``config.StrategyConfig
.required_history_bars``, duplicated as a module constant rather than
imported so this module has no config dependency, per the same "keep it a
pure function" reasoning ``engine/preflight.py`` uses for gate 10. With fewer
than that many bars, :func:`cone_5_z` returns ``None`` -- faithfully, not by
raising -- because "insufficient history" is an expected, ordinary state
(warmup, or a day preflight gate 10 item 2 will separately catch and halt on
for a different reason). ``engine/preflight.py`` gate 10 item 5 is what
actually turns an extreme *value* into a hard abort (``|cone_5_z| > 10``);
this module's job stops at computing the number.

**Every division here can hit a zero denominator on bad input, and this
module never defaults that to 0/None and continues** (the project's
recurring "silent zero" failure mode, per ``get_balance``'s docstring): a
zero-valued close (:data:`ConeSignalError` names the date/index) or a
zero-variance return window (every ``C[i]/C[i-5]`` identical across 260
sessions -- not a real market, but a corrupt or duplicated-bar series) both
raise rather than produce a fabricated ``inf``/``nan`` z-score that could
silently pass gate 10 item 5's ``abs() > 10`` check by being exactly a
non-numeric value it was never taught to reject.
"""

from __future__ import annotations

import math
import statistics
from typing import Final, Sequence

__all__ = [
    "ConeSignalError",
    "CONE_LOOKBACK_BARS",
    "REQUIRED_HISTORY_BARS",
    "cone_5_z",
]

#: The rolling population-stdev window (DESIGN §1.4).
CONE_LOOKBACK_BARS: Final[int] = 260

#: 260-bar lookback + a 5-bar shift -- the smallest ``len(closes)`` for
#: which the last bar has a full 260-return lookback window, each of whose
#: returns itself needs a bar ``horizon`` sessions earlier. Mirrors
#: ``config.StrategyConfig.required_history_bars`` (see module docstring for
#: why this is a duplicated constant, not an import).
REQUIRED_HISTORY_BARS: Final[int] = CONE_LOOKBACK_BARS + 5


class ConeSignalError(ValueError):
    """The input series cannot produce an honest ``cone_5_z`` value.

    Raised, never swallowed into a fabricated 0/None/inf -- see the module
    docstring. Callers (the session engine, not yet built) must treat this
    the same way ``BadTickError``/``EtoroValidationError`` are already
    treated elsewhere in this codebase: an abort, not a value to paper over.
    """


def cone_5_z(closes: Sequence[float], *, horizon: int = 5) -> float | None:
    """The ``cone_5_z`` signal for the most recent bar in ``closes``.

    Args:
        closes: A chronologically-ordered (oldest first) price series --
            in production, DESIGN_market_data.md §3.1's adjusted SPY
            closes with today's provisional close spliced on as the last
            element. The signal is computed for ``closes[-1]``; this
            function does not know or care what date that is.
        horizon: The bar shift in the numerator/denominator (DESIGN §1.4:
            ``CONE_HORIZON``, config-driven upstream -- this function takes
            it as a plain argument rather than importing
            ``config.StrategyConfig`` so it stays a pure function of its
            inputs, matching every other signal/guard module in this
            codebase, e.g. ``engine/preflight.py``'s stated reasoning).

    Returns:
        The z-score, or ``None`` if fewer than ``260 + horizon`` bars are
        available -- DESIGN §1.4's warmup case, not an error.

    Raises:
        ConeSignalError: a close needed by the computation is non-finite or
            not strictly positive (so a ratio against it would be zero,
            negative-flipped, or undefined), or the 260-return lookback
            window has zero variance, which would make the z-score a
            division by zero. Neither condition can arise from ordinary
            market data; both indicate a corrupt or degenerate input series
            that must not be silently converted into a fabricated signal.
    """
    if horizon <= 0:
        raise ConeSignalError(f"cone_5_z: horizon must be positive, got {horizon!r}")

    n = len(closes)
    if n < CONE_LOOKBACK_BARS + horizon:
        return None

    returns: list[float] = []
    for i in range(n - CONE_LOOKBACK_BARS, n):
        c_t = closes[i]
        c_t_minus_h = closes[i - horizon]
        for label, value in (("closes[%d]" % i, c_t), ("closes[%d]" % (i - horizon), c_t_minus_h)):
            if value is None or not math.isfinite(value) or value <= 0:
                raise ConeSignalError(
                    f"cone_5_z: {label} is {value!r} -- not a finite, positive "
                    f"price; refusing to compute a return off it rather than "
                    f"fabricate a zero/inf z-score"
                )
        returns.append(c_t / c_t_minus_h - 1.0)

    stdev = statistics.pstdev(returns)
    if stdev == 0.0:
        raise ConeSignalError(
            "cone_5_z: the 260-bar return window has zero variance (every "
            f"C[i]/C[i-{horizon}] identical) -- cannot compute a z-score; "
            "this is not a real market condition and indicates a corrupt or "
            "duplicated price series"
        )
    return returns[-1] / stdev
