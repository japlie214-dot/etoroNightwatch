"""On-device DEMO-ONLY position reader and closer (2026-09-11).

Why this exists: the 2026-09-11 ``_parse_order_result`` bug (a filled order's
``positionId`` lives at ``positionExecutions[0].positionId``, not at the top
level, and a parse miss was silently defaulted) stranded demo positions that
no route in this app could close -- the order-latency harness only closes the
positions it opened itself in the same process. Cleanup therefore had to be
done by hand, which is exactly the sort of "no route exists" gap that turns a
paper-money mistake into a real-money one later.

SAFETY -- this module cannot touch the real account:

* It reuses the order-latency harness's OWN demo gate verbatim
  (``probe_order_latency_2026_09_11._confirm_demo_or_die`` ->
  ``nightwatch.probes.order_latency.evaluate_demo_confirmation``). It defines
  no gate of its own and takes no flag that can weaken it. "Unable to
  confirm" is refused exactly like "confirmed real".
* It can only ever CLOSE. It imports no order-opening method; there is no
  code path here that constructs an ``OrderRequest``.
* With no ``close_ids`` it is strictly read-only: it fetches the portfolio
  and prints it.

Every raw payload it sees (portfolio before, each close-order lookup body,
portfolio after) is written to ``output_dir`` for an ``adb pull``.

Called from Kotlin via ``PyBridge.runDemoCleanup``.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["run"]

_HERE = Path(__file__).resolve()
_SOURCE_ROOT = _HERE.parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))


def _p(buf: list[str], msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    line = f"{ts} DEMO_CLEANUP: {msg}"
    print(line, flush=True)
    buf.append(line)


def _save(out: Path, buf: list[str], name: str, obj: object) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"{stamp}_{name}.json"
    try:
        path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
        _p(buf, f"saved {path}")
    except Exception as e:  # never let a save failure abort a close
        _p(buf, f"could not save {name}: {e!r}")


def _parse_ids(raw: str, label: str) -> list[str]:
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    if any(not value.isascii() or not value.isdecimal() or int(value) <= 0 for value in ids):
        raise ValueError(f"{label} must be comma-separated positive integer IDs")
    if len(ids) != len(set(ids)):
        raise ValueError(f"{label} contains duplicate IDs")
    if len(ids) > 5:
        raise ValueError(f"{label} is limited to five IDs per run")
    return ids


def _poll_close_order(
    client,
    order_id: str,
    *,
    clock=time.monotonic,
    sleep=time.sleep,
    poll_interval: float = 2.0,
    poll_timeout: float = 90.0,
) -> dict[str, object]:
    """GET a close-order record until terminal, timeout, or a non-404 error."""
    interval = min(max(float(poll_interval), 0.1), 15.0)
    timeout = min(max(float(poll_timeout), 0.0), 120.0)
    start = clock()
    deadline = start + timeout
    attempts: list[dict[str, object]] = []
    transitions: list[dict[str, object]] = []
    last_status = None
    error = None
    timed_out = False
    terminal = False

    while True:
        response_start = len(client.last_close_lookup_responses)
        try:
            result = client.lookup_close_order(order_id)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            captured = client.last_close_lookup_responses[response_start:]
            if captured:
                attempts.extend({**response, "error": error} for response in captured)
            else:
                attempts.append({"status_code": None, "body": None, "error": error})
            status_code = captured[-1].get("status_code") if captured else None
            if status_code == 404:
                remaining = deadline - clock()
                if remaining > 0:
                    sleep(min(interval, remaining))
                    continue
                timed_out = True
            break

        captured = client.last_close_lookup_responses[response_start:]
        if captured:
            attempts.extend(captured)
        else:
            attempts.append({"status_code": 200, "body": result.raw})
        now = clock()
        status = result.status.name
        if status != last_status:
            transitions.append({
                "status": status,
                "label": result.status_label,
                "ms_since_start": round(1000 * (now - start), 1),
                "raw": result.raw,
            })
            last_status = status
        terminal = result.is_terminal
        if terminal:
            break
        remaining = deadline - now
        if remaining <= 0:
            timed_out = True
            break
        sleep(min(interval, remaining))

    return {
        "order_id": order_id,
        "attempts": attempts,
        "transitions": transitions,
        "error": error,
        "terminal": terminal,
        "timed_out": timed_out,
        "elapsed_ms": round(1000 * (clock() - start), 1),
    }


def run(
    config_dir: str,
    output_dir: str,
    close_ids: str = "",
    poll_interval: float = 2.0,
    poll_timeout: float = 90.0,
    lookup_close_ids: str = "",
    lookup_open_ids: str = "",
) -> str:
    """Read the demo book; optionally full-close the given position ids.

    Args:
        config_dir: as ``order_latency_runner.run`` -- exported as
            ``NIGHTWATCH_HOME``.
        output_dir: where raw payloads are written for ``adb pull``.
        close_ids: comma-separated eToro position ids to FULL-close
            (``UnitsToDeduct: null``). Empty (the default) = read-only.
        lookup_close_ids: close-order IDs to inspect via GET only. Mutually
            exclusive with ``close_ids``; 404 responses are polled for a
            bounded interval and the demo portfolio is re-read afterward.
        lookup_open_ids: open-order IDs to inspect via GET only, including
            the client's demo-only v1 fallback after a v2 404.

    Never raises; returns its whole narrative for the Kotlin event log.
    """
    buf: list[str] = []
    out = Path(output_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _p(buf, f"could not create output_dir {out}: {e!r}")

    os.environ["NIGHTWATCH_HOME"] = config_dir
    _p(buf, f"NIGHTWATCH_HOME={config_dir!r} output_dir={out}")
    try:
        wanted = _parse_ids(close_ids, "close_ids")
        lookup_wanted = _parse_ids(lookup_close_ids, "lookup_close_ids")
        open_lookup_wanted = _parse_ids(lookup_open_ids, "lookup_open_ids")
    except ValueError as e:
        _p(buf, f"REFUSING: {e}; nothing was touched.")
        return "\n".join(buf)
    if sum(bool(ids) for ids in (wanted, lookup_wanted, open_lookup_wanted)) > 1:
        _p(buf, "REFUSING: close_ids, lookup_close_ids, and lookup_open_ids are mutually exclusive; nothing was touched.")
        return "\n".join(buf)
    _p(buf, f"close_ids={wanted!r} ({'will CLOSE these' if wanted else 'none'})")
    _p(buf, f"lookup_close_ids={lookup_wanted!r} ({'GET-ONLY' if lookup_wanted else 'none'})")
    _p(buf, f"lookup_open_ids={open_lookup_wanted!r} ({'GET-ONLY' if open_lookup_wanted else 'none'})")

    try:
        from nightwatch.config import load_config
        from nightwatch.etoro.client import EtoroClient
        from nightwatch.host import DesktopHost, get_host, set_host
        import probe_order_latency_2026_09_11 as harness
    except Exception as e:
        _p(buf, f"FATAL import: {e!r}")
        _p(buf, traceback.format_exc())
        return "\n".join(buf)

    previous_host = get_host()
    try:
        set_host(DesktopHost(Path(config_dir)))
        try:
            config = load_config(host=get_host())
            client = EtoroClient(config.etoro, role="write")
        except Exception as e:
            _p(buf, f"FATAL setup: {e!r}")
            return "\n".join(buf)

        # The harness's own gate, unmodified.
        try:
            import dataclasses

            demo = harness._confirm_demo_or_die(client, config)
            _save(out, buf, "demo_confirmation", dataclasses.asdict(demo))
            for reason in demo.reasons:
                _p(buf, f"  gate: {reason}")
            if not demo.confirmed:
                _p(buf, "DEMO NOT CONFIRMED -> REFUSING to close anything. Nothing was touched.")
                client.close()
                return "\n".join(buf)
        except Exception as e:
            _p(buf, f"FATAL: demo gate itself failed: {e!r} -> refusing, nothing touched.")
            client.close()
            return "\n".join(buf)
        _p(buf, "demo CONFIRMED.")

        def _book(tag: str):
            envelope = client._fetch_portfolio_envelope()  # noqa: SLF001
            _save(out, buf, f"portfolio_raw_{tag}", envelope)
            positions = client.get_portfolio()
            _p(buf, f"DEMO BOOK {tag.upper()}: {len(positions)} position(s)")
            for p in positions:
                _p(buf, f"  positionId={p.position_id} instrumentID={p.instrument_id} "
                        f"units={p.units} amount={p.amount} openRate={p.entry_price}")
            return positions

        try:
            before = _book("before")
        except Exception as e:
            _p(buf, f"FATAL: could not read the demo book: {e!r} -> refusing to close blind.")
            client.close()
            return "\n".join(buf)

        if lookup_wanted:
            for order_id in lookup_wanted:
                _p(buf, f"GET-only close-order status poll order_id={order_id}")
                result = _poll_close_order(
                    client, order_id,
                    poll_interval=poll_interval,
                    poll_timeout=poll_timeout,
                )
                _save(out, buf, f"close_order_{order_id}_poll_raw", result["attempts"])
                _save(out, buf, f"close_order_{order_id}_transitions", result["transitions"])
                _p(buf, f"  close order {order_id}: terminal={result['terminal']} "
                        f"timed_out={result['timed_out']} elapsed={result['elapsed_ms']}ms "
                        f"error={result['error']!r}")

        for order_id in open_lookup_wanted:
            captured: list[dict] = []
            try:
                result = client.lookup_order(order_id=order_id, response_capture=captured)
                _p(buf, f"open order {order_id}: status={result.status.name} "
                        f"label={result.status_label!r} terminal={result.is_terminal} "
                        f"position_id={result.position_id!r}")
            except Exception as e:
                _p(buf, f"open order {order_id}: unresolved GET-only lookup: {e!r}")
            finally:
                _save(out, buf, f"open_order_{order_id}_lookup_raw", captured)

        for pid in wanted:
            match = next((p for p in before if str(p.position_id) == str(pid)), None)
            if match is None:
                _p(buf, f"position {pid} is NOT in the demo book -- nothing to close.")
                continue
            rid = str(uuid.uuid4())
            _p(buf, f"closing {pid} (FULL close, UnitsToDeduct=null) request_id={rid}")
            t0 = time.monotonic()
            try:
                order_id = client.close_position(
                    request_id=rid, position_id=str(pid), instrument_id=match.instrument_id
                )
            except Exception as e:
                _p(buf, f"  close SUBMIT FAILED for {pid}: {e!r} -- position still open")
                _save(out, buf, f"close_{pid}_raw_response_body",
                      getattr(client, "last_write_response_body", None))
                continue
            _save(out, buf, f"close_{pid}_raw_response_body",
                  getattr(client, "last_write_response_body", None))
            _p(buf, f"  close order_id={order_id} submit={1000*(time.monotonic()-t0):.0f}ms")
            raw_final = None
            transitions: list[dict] = []
            last = None
            deadline = time.monotonic() + poll_timeout
            while True:
                try:
                    res = client.lookup_order(order_id=order_id)
                except Exception as e:
                    _p(buf, f"  lookup raised: {e!r} (this is now LOUD by design, not defaulted)")
                    break
                raw_final = res.raw
                if res.status is not last:
                    transitions.append(
                        {"status": res.status.name, "label": res.status_label,
                         "ms_since_submit": round(1000 * (time.monotonic() - t0), 1)}
                    )
                    last = res.status
                if res.is_terminal or time.monotonic() >= deadline:
                    _p(buf, f"  terminal={res.is_terminal} status={res.status.name} "
                            f"label={res.status_label!r} "
                            f"total={1000*(time.monotonic()-t0):.0f}ms")
                    break
                time.sleep(poll_interval)
            _save(out, buf, f"close_{pid}_orders_lookup_raw", raw_final)
            _save(out, buf, f"close_{pid}_transitions", transitions)

        try:
            after = _book("after")
            _p(buf, f"FINAL: demo book holds {len(after)} position(s) "
                    f"({'FLAT' if not after else 'NOT FLAT'}).")
        except Exception as e:
            _p(buf, f"could not re-read the demo book: {e!r} -- state UNVERIFIED")
        client.close()
        _p(buf, "=== demo_cleanup.run finished ===")
        return "\n".join(buf)
    finally:
        set_host(previous_host)
