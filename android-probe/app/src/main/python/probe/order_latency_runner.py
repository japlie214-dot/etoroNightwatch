"""On-device entry point for the 2026-09-11 order-latency measurement harness
(RUNBOOK_LIVE_WINDOW_2026-09-11.md §8(b)).

This module is a thin launcher, nothing more. All of the harness's logic --
and, critically, all of its SAFETY logic -- lives in the copied
``probe_order_latency_2026_09_11`` module (a byte-for-byte copy of
``tools/probe_order_latency_2026-09-11.py``, mirrored here under a legal
Python module name exactly as ``probe_live_2026_09_11.py`` mirrors
``tools/probe_live_2026-09-11.py``; the hyphenated original cannot be
imported). Nothing in this file relaxes, bypasses, or pre-empts that
harness's fail-closed demo confirmation:

* The harness itself fetches the configured environment, the derived
  credential label, and a live ``/balances`` signal, and refuses to place a
  single order unless every one of those signals POSITIVELY confirms demo
  (``nightwatch.probes.order_latency.evaluate_demo_confirmation``). "Unable
  to confirm" is refused exactly like "confirmed real". It returns 3 in that
  case, having placed nothing.
* This launcher passes NO flag that could weaken that -- it forwards only
  ``--output-dir`` plus the caller's numeric run-shape parameters. There is
  no override, no "force", no environment mutation beyond ``NIGHTWATCH_HOME``
  (the same config-push mechanism ``probe_runner``/``adopt_fetch`` already
  use).

So invoking this against a device whose ``config/config.toml`` says
``environment = "real"`` is SAFE and is in fact the intended smoke test: the
harness refuses, loudly, and this function returns that refusal verbatim.

Called from Kotlin via ``PyBridge.runOrderLatency``.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["run"]

_HERE = Path(__file__).resolve()
# .../android-probe/app/src/main/python/probe/order_latency_runner.py --
# parents[1] is the python source root (.../src/main/python), where the
# copied probe_order_latency_2026_09_11.py sits as a top-level module. Same
# belt-and-suspenders sys.path insert every other module in this package uses.
_SOURCE_ROOT = _HERE.parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))


def _p(buf: list[str], msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    line = f"{ts} LATENCY_RUNNER: {msg}"
    print(line, flush=True)
    buf.append(line)


def run(
    config_dir: str,
    output_dir: str,
    repeats: int = 4,
    order_amount_usd: float = 50.0,
    partial_fraction: float = 0.5,
    poll_interval: float = 2.0,
    poll_timeout: float = 90.0,
) -> str:
    """Kotlin entry point for the order-latency harness.

    Args:
        config_dir: directory containing a ``config/`` subfolder with
            ``config.toml`` and ``.secrets.env``, pushed onto the device
            beforehand. Bound explicitly as the host for this run, matching
            ``probe_runner.run_default``/``adopt_fetch.run``;
            ``NIGHTWATCH_HOME`` is also retained for compatibility.
        output_dir: where the harness writes its JSON payloads and the
            human-readable summary, for a later ``adb pull``.
        repeats / order_amount_usd / partial_fraction / poll_interval /
        poll_timeout: forwarded to the harness's own CLI flags, defaults
            matching the harness's own defaults exactly.

    Returns the runner's log lines plus everything the harness printed to
    stdout/stderr, so a refusal (or a crash) is visible in the Kotlin-side
    event log and not only in logcat. Never raises.
    """
    buf: list[str] = []
    out = Path(output_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _p(buf, f"could not create output_dir {out}: {e!r} (continuing; the harness retries this itself)")

    os.environ["NIGHTWATCH_HOME"] = config_dir
    _p(buf, f"NIGHTWATCH_HOME set to {config_dir!r}")
    _p(buf, f"output_dir={out}")
    _p(
        buf,
        "SAFETY: this launcher passes no flag that can weaken the harness's demo confirmation. "
        "If the resolved config is not positively, verifiably demo, the harness refuses and "
        "places nothing (exit 3). That refusal is a correct outcome, not a failure of this route.",
    )

    from nightwatch.host import DesktopHost, get_host, set_host

    previous_host = get_host()
    try:
        set_host(DesktopHost(Path(config_dir)))
        try:
            import probe_order_latency_2026_09_11 as harness
        except Exception as e:
            _p(buf, f"FATAL: cannot import copied probe_order_latency_2026_09_11: {e!r}")
            _p(buf, traceback.format_exc())
            return "\n".join(buf)

        # The harness gained its own Kotlin-facing entry point (2026-09-11):
        # run_all(output_dir) -> int, mirroring probe_live's convention. Prefer it
        # over calling main() ourselves so the harness owns its own argv shape.
        # It requires an explicit output_dir by design -- the module's
        # DEFAULT_OUTPUT_DIR is computed from the repo layout and resolves to an
        # unwritable path once the file lives in the APK's source set.
        #
        # run_all() takes only output_dir; every other setting keeps the harness's
        # documented RUNBOOK §8(b) default. When this function's own run-shape
        # parameters are left at those same defaults we call run_all() directly;
        # if a caller overrides one, we fall back to main() with an explicit argv,
        # since run_all() has no way to express the override.
        use_run_all = (
            repeats == 4
            and order_amount_usd == 50.0
            and partial_fraction == 0.5
            and poll_interval == 2.0
            and poll_timeout == 90.0
        )

        # The harness prints its entire narrative (including every demo-confirmation
        # reason) to stdout. Chaquopy routes that to logcat, but the Kotlin caller
        # only ever sees this function's RETURN value -- so tee it into the buffer
        # as well, otherwise a refusal would be invisible in the on-device event log.
        captured = io.StringIO()

        class _Tee(io.TextIOBase):
            def __init__(self, *streams):
                self._streams = streams

            def write(self, s):  # type: ignore[override]
                for st in self._streams:
                    try:
                        st.write(s)
                        st.flush()
                    except Exception:
                        pass
                return len(s)

            def flush(self):  # type: ignore[override]
                for st in self._streams:
                    with contextlib.suppress(Exception):
                        st.flush()

        real_stdout = sys.stdout
        tee = _Tee(real_stdout, captured)
        rc: int | None = None
        try:
            with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
                if use_run_all:
                    _p(buf, f"calling probe_order_latency_2026_09_11.run_all({str(out)!r}) (all defaults)")
                    rc = harness.run_all(str(out))
                else:
                    argv = [
                        "--output-dir", str(out),
                        "--repeats", str(repeats),
                        "--order-amount-usd", str(order_amount_usd),
                        "--partial-fraction", str(partial_fraction),
                        "--poll-interval", str(poll_interval),
                        "--poll-timeout", str(poll_timeout),
                    ]
                    _p(buf, f"calling probe_order_latency_2026_09_11.main({argv!r}) (non-default run shape)")
                    rc = harness.main(argv)
        except SystemExit as e:
            _p(buf, f"harness raised SystemExit({e.code!r})")
            rc = e.code if isinstance(e.code, int) else None
        except Exception as e:
            _p(buf, f"harness raised: {e!r}")
            _p(buf, traceback.format_exc())

        text = captured.getvalue()
        if text:
            buf.append(text.rstrip("\n"))

        if rc == 3:
            _p(
                buf,
                "harness returned 3 = DEMO NOT CONFIRMED -> REFUSED TO PLACE ANY ORDER. "
                "This is the fail-closed path working as designed; nothing was traded.",
            )
        elif rc == 2:
            _p(buf, "harness returned 2 = setup failure (import or load_config). Nothing was traded.")
        elif rc == 0:
            _p(buf, "harness returned 0 = completed, book left flat.")
        elif rc == 1:
            _p(buf, "harness returned 1 = FINISHED WITH POSITION(S) LEFT OPEN -- see left_open_positions output file.")
        else:
            _p(buf, f"harness returned {rc!r}")

        _p(buf, "=== order_latency_runner.run finished -- see output_dir for every saved file ===")
        return "\n".join(buf)
    finally:
        set_host(previous_host)
