"""Pure(ish), offline-testable logic for the 2026-09-11 order-latency
measurement harness (DESIGN.md §1.1's standing highest-risk unknown;
DESIGN_session.md §3.2/§3.4/§4.8/§4.9; RUNBOOK_LIVE_WINDOW_2026-09-11.md
§8(b)).

Mirrors the split ``nightwatch.probes.live_window``/``fee_audit`` already
established: arithmetic, statistics and text rendering live here and are
exercised by the desktop test suite with hand-built inputs; the driver
script (``tools/probe_order_latency_2026-09-11.py``) does the actual
DEMO-account I/O (resolving SCHG, reading config, printing to a file) and
calls into this module for everything worth getting right and worth testing
without a live account.

Two functions here are the one exception to "pure": :func:`run_open_leg` and
:func:`run_close_leg` call the caller-supplied ``client``'s own methods
(``place_buy_order``, ``close_position``, ``lookup_order``) rather than
issuing HTTP directly. That keeps them exercisable against a real
``EtoroClient`` wired to ``httpx.MockTransport`` -- this project's own
established offline-broker-test idiom (``tests/test_etoro_client.py``) --
instead of requiring a live DEMO account to test the timestamping logic
itself. Every side effect (the HTTP call) still goes through the injected
``client``, never through this module directly.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..etoro.errors import EtoroTimeoutError, EtoroValidationError

__all__ = [
    "StatusObservation",
    "OrderLegTrace",
    "run_open_leg",
    "run_close_leg",
    "LegStatistics",
    "summarize_leg",
    "DemoConfirmation",
    "evaluate_demo_confirmation",
    "T30_BUDGET_SECONDS",
    "ProjectionResult",
    "project_session_budget",
    "render_human_summary",
]


# ---------------------------------------------------------------------------
# Per-transition timestamping
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatusObservation:
    """One observed status, the moment it was FIRST seen.

    RUNBOOK §8(b) step 2 is explicit: "do not treat [the] internal polling
    as opaque -- every observed status change [must be timestamped], not
    just the final terminal one." A poll that returns the same status twice
    in a row does not produce a second observation here -- only genuine
    transitions are recorded, so this is a list of *changes*, not a list of
    *polls*.
    """

    status: str
    """``OrderStatus.name`` -- e.g. ``"RECEIVED"``, ``"WAITING_FOR_MARKET"``,
    ``"FILLED"``. A string, not the IntEnum itself, so this dataclass stays
    trivially JSON-serializable for the driver's output file."""
    observed_at: str
    """ISO-8601 wall-clock timestamp of first observation."""
    elapsed_since_submission_s: float
    """Monotonic-clock seconds since the order/close was submitted (t0) --
    never wall-clock subtraction, which a clock adjustment could corrupt."""
    route: str
    """Exact lookup route that produced this observation."""
    raw_lookup_body: Mapping[str, Any] = field(default_factory=dict)
    """Full lookup payload that produced this status transition."""


@dataclass(frozen=True, slots=True)
class OrderLegTrace:
    """One full timed leg: one buy, one full close, or one partial close."""

    leg: str
    """One of ``"open"``, ``"close_full"``, ``"close_partial"``,
    ``"close_cleanup"`` (the last is the flattening close after a
    deliberate partial, RUNBOOK §8(b) step 7 -- timed for completeness but
    never counted toward the three named legs' statistics)."""
    repeat_index: int
    request_id: str
    order_id: str | None
    position_id: str | None
    submitted_at: str
    """ISO-8601 wall-clock timestamp of submission (t0)."""
    observations: tuple[StatusObservation, ...]
    terminal_status: str | None
    """``None`` only if the poll loop never got a single successful lookup
    (an exception before the first ``lookup_order`` call succeeded)."""
    timed_out: bool
    time_to_terminal_s: float | None
    """``None`` when ``timed_out`` -- a timeout must never be reported as a
    latency figure, it would understate the true (unknown) latency."""
    is_partial_fill: bool
    """True iff ``PARTIALLY_FILLED`` was observed at ANY point in this leg's
    polling -- the terminal status alone, DESIGN_session.md §3.4's "note
    partial fills where they occur," surfaced per-trace so the driver's
    summary can call it out by repeat/leg. Checked against every
    observation, not just the terminal one, because ``PARTIALLY_FILLED`` (5)
    is deliberately NOT in ``TERMINAL_ORDER_STATUSES`` (an order can sit in
    it mid-poll and still resolve further) -- only checking the terminal
    status would miss a partial fill the order later filled the rest of."""
    raw_final: Mapping[str, Any] = field(default_factory=dict)
    """The last ``OrderResult.raw`` seen -- full raw payload, survives to the
    output file even though this dataclass itself only reports a summary."""
    raw_submission_response: Any = None
    """Raw order POST response for this leg, when available."""
    raw_submission_request: Mapping[str, Any] | None = None
    """Exact close POST JSON body, when this trace represents a close leg."""
    raw_close_lookup_responses: tuple[Mapping[str, Any], ...] = ()
    """HTTP status and unparsed response body for each close-order lookup."""
    raw_open_lookup_responses: tuple[Mapping[str, Any], ...] = ()
    """HTTP status and unparsed response body for each open-order lookup."""


def _poll_with_transitions(
    client: Any,
    *,
    order_id: str,
    clock: Callable[[], float],
    wall_clock: Callable[[], str],
    sleep: Callable[[float], None],
    poll_interval_s: float,
    poll_timeout_s: float,
    t0: float,
    close_position_id: str | None = None,
    reference_id_on_404: str | None = None,
    raw_open_lookup_responses: list[dict[str, Any]] | None = None,
) -> tuple[list[StatusObservation], Any, bool]:
    """Shared polling loop for both legs -- poll ``lookup_order`` directly
    (never ``poll_order_to_terminal``, whose internal polling this
    measurement must not treat as opaque, RUNBOOK §8(b) step 2), recording
    one :class:`StatusObservation` per genuine transition.

    Returns ``(observations, last_result, timed_out)``.
    """
    observations: list[StatusObservation] = []
    last_status: str | None = None
    deadline = clock() + poll_timeout_s
    captured = raw_open_lookup_responses if raw_open_lookup_responses is not None else []

    def lookup() -> tuple[Any, str]:
        if close_position_id is None:
            before = len(captured)
            try:
                result = client.lookup_order(
                    order_id=order_id, response_capture=captured
                )
            except EtoroValidationError as exc:
                if reference_id_on_404 is None or exc.status_code != 404:
                    raise
                before = len(captured)
                result = client.lookup_order(
                    reference_id=reference_id_on_404, response_capture=captured
                )
            if len(captured) <= before:
                raise EtoroValidationError(
                    "lookup produced no captured response; route unknown"
                )
            route = _route_from_capture(captured[-1])
            if result.order_id != order_id:
                raise EtoroValidationError(
                    f"open lookup returned order_id={result.order_id!r}, expected {order_id!r}"
                )
            if result.reference_id is not None and result.reference_id != reference_id_on_404:
                raise EtoroValidationError(
                    f"open lookup returned reference_id={result.reference_id!r}, "
                    f"expected {reference_id_on_404!r}"
                )
            if route == "v2_reference_id" and result.reference_id != reference_id_on_404:
                raise EtoroValidationError(
                    "open lookup by referenceId did not confirm the request reference"
                )
            return result, route
        result = (
            client.lookup_close_order(order_id)
        )
        if result.order_id != order_id:
            raise EtoroValidationError(
                f"close lookup returned order_id={result.order_id!r}, expected {order_id!r}"
            )
        if result.position_id is not None and result.position_id != close_position_id:
            raise EtoroValidationError(
                f"close lookup returned position_id={result.position_id!r}, "
                f"expected {close_position_id!r}"
            )
        if result.is_filled and result.position_id != close_position_id:
            raise EtoroValidationError(
                f"filled close order {order_id} did not identify position {close_position_id}"
            )
        return result, "close_order_lookup"

    while True:
        try:
            result, route = lookup()
        except EtoroValidationError as exc:
            retryable_not_found = exc.status_code == 404 and (
                close_position_id is not None or reference_id_on_404 is not None
            )
            if not retryable_not_found:
                raise
            if clock() >= deadline:
                raise EtoroTimeoutError(
                    f"order {order_id} was not visible after {poll_timeout_s}s"
                ) from exc
            # Order records can lag an accepted POST. Retry only status GETs,
            # at the normal poll cadence; each response body is retained.
            sleep(poll_interval_s)
            continue
        now = clock()
        if result.status.name != last_status:
            observations.append(
                StatusObservation(
                    status=result.status.name,
                    observed_at=wall_clock(),
                    elapsed_since_submission_s=now - t0,
                    route=route,
                    raw_lookup_body=dict(result.raw),
                )
            )
            last_status = result.status.name
        if result.is_terminal:
            return observations, result, False
        if now >= deadline:
            return observations, result, True
        sleep(poll_interval_s)


def _route_from_capture(entry: Mapping[str, Any]) -> str:
    """Label the lookup route recorded for one HTTP response."""
    if "/api/v1/" in entry.get("path", ""):
        return "v1_order_id_demo"
    if "referenceId" in (entry.get("params") or {}):
        return "v2_reference_id"
    return "v2_order_id"


def run_open_leg(
    client: Any,
    *,
    request_id: str,
    instrument_id: int,
    amount_usd: float,
    repeat_index: int,
    clock: Callable[[], float],
    wall_clock: Callable[[], str],
    sleep: Callable[[float], None],
    poll_interval_s: float = 2.0,
    poll_timeout_s: float = 90.0,
) -> OrderLegTrace:
    """Place one buy order and time every observed status transition to
    terminal (RUNBOOK §8(b) steps 1-3).

    ``clock`` is a monotonic seconds source (``time.monotonic`` in
    production, an injectable fake in tests); ``wall_clock`` returns an
    ISO-8601 string for display; neither is read from this module's own
    imports so a test can supply a deterministic pair, mirroring
    ``tests/test_etoro_client.py``'s ``Counter`` idiom.
    """
    t0 = clock()
    submitted_at = wall_clock()
    raw_lookup_responses: list[dict[str, Any]] = []
    order_id: str | None = None
    raw_submission_request: Mapping[str, Any] | None = None
    raw_submission_response: Any = None
    try:
        order_id = client.place_buy_order(request_id, instrument_id, amount_usd)
        raw_submission_request = getattr(client, "last_write_request_body", None)
        raw_submission_response = getattr(client, "last_write_response_body", None)
        observations, result, timed_out = _poll_with_transitions(
            client,
            order_id=order_id,
            clock=clock,
            wall_clock=wall_clock,
            sleep=sleep,
            poll_interval_s=poll_interval_s,
            poll_timeout_s=poll_timeout_s,
            t0=t0,
            reference_id_on_404=request_id,
            raw_open_lookup_responses=raw_lookup_responses,
        )
    except BaseException as exc:
        raw_submission_request = getattr(
            client, "last_write_request_body", raw_submission_request
        )
        raw_submission_response = getattr(
            client, "last_write_response_body", raw_submission_response
        )
        setattr(exc, "open_leg_trace", {
            "leg": "open",
            "repeat_index": repeat_index,
            "request_id": request_id,
            "order_id": order_id,
            "position_id": None,
            "submitted_at": submitted_at,
            "raw_submission_request": raw_submission_request,
            "raw_submission_response": raw_submission_response,
            "raw_open_lookup_responses": raw_lookup_responses,
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise
    t_end = clock()
    return OrderLegTrace(
        leg="open",
        repeat_index=repeat_index,
        request_id=request_id,
        order_id=order_id,
        position_id=result.position_id,
        submitted_at=submitted_at,
        observations=tuple(observations),
        terminal_status=result.status.name,
        timed_out=timed_out,
        time_to_terminal_s=None if timed_out else (t_end - t0),
        is_partial_fill=any(o.status == "PARTIALLY_FILLED" for o in observations) or result.status.name == "PARTIALLY_FILLED",
        raw_final=dict(result.raw),
        raw_submission_request=raw_submission_request,
        raw_submission_response=raw_submission_response,
        raw_open_lookup_responses=tuple(dict(response) for response in raw_lookup_responses),
    )


def run_close_leg(
    client: Any,
    *,
    leg: str,
    request_id: str,
    position_id: str,
    instrument_id: int,
    units_to_deduct: float | None,
    repeat_index: int,
    clock: Callable[[], float],
    wall_clock: Callable[[], str],
    sleep: Callable[[float], None],
    poll_interval_s: float = 2.0,
    poll_timeout_s: float = 90.0,
) -> OrderLegTrace:
    """Close (fully or partially) one position and time every observed
    status transition to terminal (RUNBOOK §8(b) steps 4/4b).

    ``leg`` must be one of ``"close_full"``, ``"close_partial"``,
    ``"close_cleanup"`` -- the caller decides which, based on whether
    ``units_to_deduct`` is ``None`` (full) or a positive float (partial), and
    whether this call is the measured partial-close leg itself or the
    flattening close that follows it (RUNBOOK §8(b) step 7). This function
    does not infer ``leg`` from ``units_to_deduct`` itself so a cleanup close
    (which also passes ``units_to_deduct=None``, a full close of the
    remainder) is never silently mislabelled as the measured full-close leg.
    """
    if leg not in ("close_full", "close_partial", "close_cleanup"):
        raise ValueError(f"leg must be 'close_full'/'close_partial'/'close_cleanup', got {leg!r}")
    t0 = clock()
    submitted_at = wall_clock()
    lookup_start = len(getattr(client, "last_close_lookup_responses", ()))
    raw_request = {"InstrumentId": instrument_id, "UnitsToDeduct": units_to_deduct}
    order_id: str | None = None
    try:
        order_id = client.close_position(request_id, position_id, instrument_id, units_to_deduct)
        observations, result, timed_out = _poll_with_transitions(
            client,
            order_id=order_id,
            clock=clock,
            wall_clock=wall_clock,
            sleep=sleep,
            poll_interval_s=poll_interval_s,
            poll_timeout_s=poll_timeout_s,
            t0=t0,
            close_position_id=position_id,
        )
    except BaseException as exc:
        # The harness writes this evidence sidecar before stopping. A failed
        # lookup must retain its HTTP body and must never trigger another POST.
        setattr(exc, "close_leg_trace", {
            "leg": leg,
            "repeat_index": repeat_index,
            "request_id": request_id,
            "order_id": order_id,
            "position_id": position_id,
            "submitted_at": submitted_at,
            "raw_submission_request": raw_request,
            "raw_submission_response": getattr(client, "last_write_response_body", None),
            "raw_close_lookup_responses": list(
                getattr(client, "last_close_lookup_responses", ())[lookup_start:]
            ),
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise
    t_end = clock()
    return OrderLegTrace(
        leg=leg,
        repeat_index=repeat_index,
        request_id=request_id,
        order_id=order_id,
        position_id=result.position_id or position_id,
        submitted_at=submitted_at,
        observations=tuple(observations),
        terminal_status=result.status.name,
        timed_out=timed_out,
        time_to_terminal_s=None if timed_out else (t_end - t0),
        is_partial_fill=any(o.status == "PARTIALLY_FILLED" for o in observations) or result.status.name == "PARTIALLY_FILLED",
        raw_final=dict(result.raw),
        raw_submission_response=getattr(client, "last_write_response_body", None),
        raw_submission_request=raw_request,
        raw_close_lookup_responses=tuple(
            dict(response)
            for response in getattr(client, "last_close_lookup_responses", ())[lookup_start:]
        ),
    )


# ---------------------------------------------------------------------------
# Statistics across repeats -- variance must be visible, not just a mean
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegStatistics:
    """Summary statistics for one leg's time-to-terminal, across repeats.

    Only terminal (non-timed-out) samples are ever passed in here -- a
    timeout has no latency figure to contribute (:attr:`OrderLegTrace.
    time_to_terminal_s` is ``None`` for one), so it is reported separately
    by the driver, never silently folded into a mean as if it were fast.
    """

    leg: str
    n: int
    mean_s: float
    median_s: float
    stdev_s: float | None
    """``None`` when ``n < 2`` -- ``statistics.stdev`` itself requires at
    least two data points; reported as unavailable, never as 0.0 (which
    would falsely claim zero variance from a single sample)."""
    min_s: float
    max_s: float
    spread_s: float
    """``max_s - min_s`` -- the task's explicit ask: "variance is visible,
    not just the mean." A single fast run and a single slow run must read
    differently from two runs that were both fast."""
    values_s: tuple[float, ...]


def summarize_leg(leg: str, times_s: Sequence[float]) -> LegStatistics:
    """Summarize one leg's terminal times across repeats.

    Raises ``ValueError`` on an empty sequence rather than returning a
    zeroed-out result -- a leg with zero successful samples has nothing to
    summarize, and pretending otherwise would hide that every repeat of this
    leg timed out or failed, which is itself the most important finding.
    """
    if not times_s:
        raise ValueError(f"summarize_leg({leg!r}): no terminal samples to summarize")
    values = tuple(float(t) for t in times_s)
    n = len(values)
    return LegStatistics(
        leg=leg,
        n=n,
        mean_s=statistics.mean(values),
        median_s=statistics.median(values),
        stdev_s=statistics.stdev(values) if n >= 2 else None,
        min_s=min(values),
        max_s=max(values),
        spread_s=max(values) - min(values),
        values_s=values,
    )


# ---------------------------------------------------------------------------
# The demo-account safety gate -- PROBED not trusted, fail-closed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DemoConfirmation:
    """Whether this session may proceed to place any order at all.

    Built to satisfy the task's hard safety requirement literally: "refuse
    to run, loudly, if it cannot positively confirm it is on the demo
    account." ``confirmed`` is only ever ``True`` when every one of four
    independent signals lines up on ``"demo"`` -- an *absence* of a
    contradicting signal is never enough (HANDOFF's "PROBED not trusted";
    ``EtoroClient.verify_environment``'s own docstring: "unable to confirm"
    is not "confirmed OK"). This is a stricter bar than preflight gate 7
    (DESIGN §5), which remains an unimplemented stub -- this harness cannot
    rely on it and builds its own check instead.
    """

    confirmed: bool
    reasons: tuple[str, ...]
    """Every check performed, in order, human-readable -- printed verbatim
    by the driver whether confirmed or refused, so a refusal is never a bare
    boolean with no explanation."""
    detected_signal: str | None
    """Whatever ``EtoroClient._detect_environment`` returned against the
    live ``/balances`` payload -- ``"demo"``, ``"real"``, or ``None``
    (no signal found)."""


def evaluate_demo_confirmation(
    *,
    config_environment: str,
    credential_label: str,
    verify_environment_ok: bool,
    verify_environment_error: str | None,
    detected_signal: str | None,
) -> DemoConfirmation:
    """Pure decision logic for the demo-account safety gate.

    Takes already-probed signals (the driver does the actual HTTP) so this
    is exercisable with hand-built inputs, no broker at all. Four checks,
    fail-closed on the first one that does not positively support "demo":

    1. ``config_environment == "demo"`` (config.toml's own setting --
       necessary, not sufficient on its own: a config value can be wrong in
       a way no amount of local type-checking catches, per
       ``EtoroClient``'s own module docstring).
    2. ``credential_label`` starts with ``"demo:"`` (the credential pair
       ``EtoroConfig`` actually derived and handed to the client -- a
       second, independent read of the same environment enum, not a fresh
       probe, but cheap insurance against this module's own logic error).
    3. ``verify_environment_ok`` -- i.e. ``EtoroClient.verify_environment()``
       raised no mismatch against a live probe.
    4. ``detected_signal == "demo"`` -- a POSITIVE signal from a live
       environment probe, not merely the absence of a contradicting one.

       Corrected 2026-09-11: this signal used to be sourced from a scan of
       the ``/balances`` body alone, which made it permanently ``None`` --
       that body carries no environment marker at any depth and is
       structurally identical between demo and real (see
       ``EtoroClient.verify_environment``'s docstring for the captured
       evidence). The signal now comes from
       ``EtoroClient.probe_environment()``, which identifies the binding of
       the CREDENTIAL via eToro's per-segment authorisation. Nothing about
       this function's fail-closed logic changed: ``None`` still refuses.
       The fix made positive confirmation *possible*, not refusal rarer.
       This is the check ``verify_environment()`` itself does not make (its
       own docstring: returning normally means "unable to confirm," not
       "confirmed OK") -- and is the one this task's hard safety requirement
       demands: refuse on ambiguity, not just on a detected mismatch.
    """
    reasons: list[str] = []

    if config_environment != "demo":
        reasons.append(
            f"REFUSING: config/config.toml etoro.environment = {config_environment!r}, "
            "not 'demo' -- this harness must never run against anything but the demo "
            "account, and refuses before any order is even considered."
        )
        return DemoConfirmation(confirmed=False, reasons=tuple(reasons), detected_signal=detected_signal)
    reasons.append(
        "config/config.toml etoro.environment = 'demo' (necessary, not sufficient on "
        "its own -- config is PROBED, not trusted, from here on)."
    )

    if not credential_label.startswith("demo:"):
        reasons.append(
            f"REFUSING: derived credential label {credential_label!r} does not start "
            "with 'demo:' -- structural mismatch between config and the credential pair "
            "actually in use."
        )
        return DemoConfirmation(confirmed=False, reasons=tuple(reasons), detected_signal=detected_signal)
    reasons.append(f"credential label {credential_label!r} confirms a demo-bound credential pair.")

    if not verify_environment_ok:
        reasons.append(
            f"REFUSING: a live probe detected an environment mismatch: {verify_environment_error}"
        )
        return DemoConfirmation(confirmed=False, reasons=tuple(reasons), detected_signal=detected_signal)
    reasons.append("EtoroClient.verify_environment() raised no mismatch against a live probe.")

    if detected_signal != "demo":
        reasons.append(
            f"REFUSING: the live environment probe did not POSITIVELY confirm demo "
            f"(detected={detected_signal!r}). 'Unable to confirm' is not 'confirmed' -- "
            "failing closed per this harness's hard safety requirement, even though "
            "nothing here proves the account IS real either."
        )
        return DemoConfirmation(confirmed=False, reasons=tuple(reasons), detected_signal=detected_signal)
    reasons.append(
        f"live environment probe positively signals demo (detected={detected_signal!r})."
    )

    return DemoConfirmation(confirmed=True, reasons=tuple(reasons), detected_signal=detected_signal)


# ---------------------------------------------------------------------------
# T-30 budget projection (§4.9's per-order latency multiplied by lot counts)
# ---------------------------------------------------------------------------

#: DESIGN_session.md §3.2/§14 item 6 / RUNBOOK §8(b): the two-phase session's
#: whole margin against the market close, in seconds.
T30_BUDGET_SECONDS: float = 30.0 * 60.0


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    """Whether N close orders at the measured per-order latency fit T-30."""

    label: str
    per_order_seconds: float
    orders_low: int
    orders_high: int
    t30_budget_seconds: float
    projected_seconds_low: float
    projected_seconds_high: float
    fits_low: bool
    fits_high: bool
    margin_low_seconds: float
    """``t30_budget_seconds - projected_seconds_low`` -- negative means the
    low-end estimate alone already overruns the budget."""
    margin_high_seconds: float


def project_session_budget(
    per_order_seconds: float,
    *,
    orders_low: int,
    orders_high: int,
    label: str,
    t30_budget_seconds: float = T30_BUDGET_SECONDS,
) -> ProjectionResult:
    """Multiply a measured per-order latency by DESIGN_session.md §4.9's
    projected order counts (~2-3 lots per sell today, ~10 as the book
    matures) to answer the real question this whole harness exists for:
    does the two-phase session fit inside the T-30 window.

    ``per_order_seconds`` should be the measured CLOSE-leg latency (full or
    partial -- whichever the caller is projecting), not the open leg: §4.8's
    sell-to-target loop is what repeats per lot inside phase A, and that is
    what §4.9 says multiplies with the book's growth. This function does not
    itself decide which figure to pass -- it only does the multiplication
    and the fits/margin arithmetic, so it stays trivially testable.

    This is a **serial** estimate (``per_order_seconds * n``) -- deliberately
    the conservative, non-optimistic bound: DESIGN_session.md never proposes
    concurrent close requests (each intent goes through the executor's
    poll-to-terminal path one at a time, §3.1 step 3), so pretending N orders
    overlap would understate the risk this measurement exists to surface.
    """
    if per_order_seconds < 0:
        raise ValueError(f"per_order_seconds must be >= 0, got {per_order_seconds!r}")
    if orders_low > orders_high:
        raise ValueError(f"orders_low ({orders_low}) must be <= orders_high ({orders_high})")
    if orders_low < 0:
        raise ValueError(f"orders_low must be >= 0, got {orders_low!r}")
    projected_low = per_order_seconds * orders_low
    projected_high = per_order_seconds * orders_high
    return ProjectionResult(
        label=label,
        per_order_seconds=per_order_seconds,
        orders_low=orders_low,
        orders_high=orders_high,
        t30_budget_seconds=t30_budget_seconds,
        projected_seconds_low=projected_low,
        projected_seconds_high=projected_high,
        fits_low=projected_low <= t30_budget_seconds,
        fits_high=projected_high <= t30_budget_seconds,
        margin_low_seconds=t30_budget_seconds - projected_low,
        margin_high_seconds=t30_budget_seconds - projected_high,
    )


# ---------------------------------------------------------------------------
# Human-readable summary
# ---------------------------------------------------------------------------


def render_human_summary(
    *,
    demo: DemoConfirmation,
    leg_stats: Mapping[str, LegStatistics],
    projections: Sequence[ProjectionResult],
    partial_fill_events: Sequence[str],
    timed_out_events: Sequence[str],
    left_open_positions: Sequence[str],
) -> str:
    """Build the final human-readable block so Friday is execution, not
    authoring (the task's own framing) -- the operator should be able to
    read this block alone and know whether the design fits T-30, without
    re-deriving anything from the machine-readable JSON.
    """
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("ORDER-LATENCY MEASUREMENT HARNESS -- SUMMARY")
    lines.append("=" * 78)
    lines.append("")
    lines.append(f"Demo-account confirmation: {'CONFIRMED' if demo.confirmed else 'REFUSED'}")
    for reason in demo.reasons:
        lines.append(f"  - {reason}")
    lines.append("")

    if not demo.confirmed:
        lines.append("NO ORDERS WERE PLACED -- the demo-account safety gate refused this run.")
        lines.append("=" * 78)
        return "\n".join(lines)

    lines.append("Per-leg timing statistics (terminal samples only; timeouts excluded, see below):")
    for leg_name, stats in leg_stats.items():
        stdev_txt = f"{stats.stdev_s:.3f}s" if stats.stdev_s is not None else "n/a (n<2)"
        lines.append(
            f"  {leg_name:14s} n={stats.n} mean={stats.mean_s:.3f}s median={stats.median_s:.3f}s "
            f"stdev={stdev_txt} min={stats.min_s:.3f}s max={stats.max_s:.3f}s spread={stats.spread_s:.3f}s"
        )
    lines.append("")

    lines.append("T-30 budget projection (§4.9's order-count multiplier, serial estimate):")
    for proj in projections:
        fits_txt = (
            "FITS both bounds"
            if proj.fits_low and proj.fits_high
            else "FITS the low bound only"
            if proj.fits_low
            else "DOES NOT FIT EITHER BOUND"
        )
        lines.append(
            f"  {proj.label}: {proj.orders_low}-{proj.orders_high} orders @ "
            f"{proj.per_order_seconds:.3f}s/order -> {proj.projected_seconds_low:.1f}s..{proj.projected_seconds_high:.1f}s "
            f"vs T-30 budget {proj.t30_budget_seconds:.0f}s -- {fits_txt} "
            f"(margin {proj.margin_low_seconds:.1f}s..{proj.margin_high_seconds:.1f}s)"
        )
    lines.append("")

    if partial_fill_events:
        lines.append("PartiallyFilled terminal statuses observed (DESIGN_session.md §3.4):")
        for event in partial_fill_events:
            lines.append(f"  - {event}")
    else:
        lines.append("No repeat produced an unexpected PartiallyFilled terminal status.")
    lines.append("")

    if timed_out_events:
        lines.append("Legs that TIMED OUT (excluded from the statistics above, reported separately):")
        for event in timed_out_events:
            lines.append(f"  - {event}")
    else:
        lines.append("No leg timed out.")
    lines.append("")

    if left_open_positions:
        lines.append("!!! POSITIONS LEFT OPEN -- MANUAL CLEANUP REQUIRED !!!")
        for pos in left_open_positions:
            lines.append(f"  - {pos}")
    else:
        lines.append("Cleanup confirmed: no DEMO position left open by this run.")

    lines.append("=" * 78)
    return "\n".join(lines)
