"""Demo buys for adoption testing; only demo_seed may call unattended; never opens a database."""

from __future__ import annotations

from contextlib import closing
import math
from pathlib import Path
from uuid import uuid4

from nightwatch.android_entry import _session_lock
from nightwatch.config import load_config
from nightwatch.engine.preflight import gate_7_environment
from nightwatch.engine.session import _redact_reason
from nightwatch.etoro.client import EtoroClient
from nightwatch.host import DesktopHost, get_host, set_host


def run(home: str, count: int, usd_each: float) -> str:
    """Submit sequential SCHG buys, stopping on the first error; never retry here."""
    previous_host = get_host()
    lines = []
    config = None
    stage = "setup"

    def report(text):
        safe = (_redact_reason(text, config=config, fallback="Demo buy result unavailable")
                if config is not None else text)
        print(safe, flush=True)
        lines.append(safe)

    try:
        host = DesktopHost(Path(home))
        set_host(host)
        if host.root.name != "nightwatch_demo":
            report("Demo buy refused: dedicated nightwatch_demo home required")
            return "\n".join(lines)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
            report("Demo buy refused: count must be an integer from 1 to 10")
            return "\n".join(lines)
        if (isinstance(usd_each, bool) or not isinstance(usd_each, (int, float))
                or not math.isfinite(usd_each) or not 50 <= usd_each <= 15000):
            report("Demo buy refused: USD each must be finite and from 50 to 15000")
            return "\n".join(lines)
        with _session_lock(host.data_dir() / "session.lock") as acquired:
            if not acquired:
                report("Demo buy refused: session, adoption or demo buy already running")
                return "\n".join(lines)
            stage = "configuration load"
            config = load_config(host=host)
            if config.etoro.environment.value != "demo":
                report("Demo buy refused: configuration must select demo, never real")
                return "\n".join(lines)
            stage = "client setup"
            # No Repo: instrument resolution must not persist cache rows either.
            with closing(EtoroClient(config.etoro, role="write", repo=None)) as client:
                stage = "environment detection"
                gate = gate_7_environment(client=client)
                if not gate.passed or gate.details.get("detected") != "demo":
                    report("Demo buy refused: credential environment not positively confirmed as demo")
                    return "\n".join(lines)
                stage = "SCHG resolution"
                instrument_id = client.resolve_instrument_id("SCHG")
                report(f"DEMO only: {count} SCHG buys, USD {usd_each:g} each, total USD {count * usd_each:g}")
                for number in range(1, count + 1):
                    stage = f"order {number}/{count} submission"
                    # Manual, unmanaged orders deliberately have no session intent or recovery row.
                    order_id = client.place_buy_order(str(uuid4()), instrument_id, usd_each)
                    safe_id = (f"order ID {order_id}"
                               if isinstance(order_id, str) and 1 <= len(order_id) <= 20
                               and order_id.isascii() and order_id.isdigit()
                               else "order ID unavailable (non-numeric)")
                    report(f"Order {number}/{count}: accepted, {safe_id}; fill not verified")
                report("Demo buy complete: no local database opened; positions remain unmanaged until adoption")
    except Exception as exc:
        # Broker bodies and exception messages are not operator-facing diagnostics.
        report(f"Demo buy stopped during {stage} ({type(exc).__name__}); "
               "nothing further attempted. Check demo portfolio before retrying; an order may have been accepted.")
    finally:
        set_host(previous_host)
    return "\n".join(lines)
