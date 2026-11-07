"""On-device, READ-ONLY capture of the raw ``/balances`` body (and a small set
of sibling read endpoints) so that ``EtoroClient._detect_environment`` can be
written against the payload eToro ACTUALLY sends, rather than against a guess.

Why this exists (2026-09-11): ``EtoroClient._detect_environment`` looked for
``isDemo``/``is_demo``/``demo``/``accountType``/``account_type``/``environment``
at the TOP LEVEL of the ``/balances`` body only. The live body carries none of
those names at that level, so the function returned ``None`` unconditionally --
which made :meth:`EtoroClient.verify_environment` a silent no-op on the REAL
account as much as on demo, and made the order-latency harness's fourth demo
signal permanently inconclusive (it correctly failed closed and exited 3).

Fixing that requires the real payload shape. This module fetches it.

**READ-ONLY, by construction, not just by convention** -- same guarantees the
sibling ``probe.adopt_fetch`` module documents:

* ``EtoroClient(config.etoro, role="read")`` -- the read-scoped credential
  (``ETORO_READ_PRIVATE_KEY``), never the write one.
* Every call is a ``GET``. No order-placing, order-modifying, order-cancelling
  or position-closing method is imported, let alone called, anywhere in this
  file. ``_request(..., method="GET", request_class="read")`` is the only
  transport call made.
* ``repo=`` is never passed to ``EtoroClient``, so this process opens no
  sqlite file, live sidecar included.

Called from Kotlin via ``PyBridge.runEnvProbe`` -- reuses the existing
``NIGHTWATCH_HOME`` config-push convention unchanged (see
``doc/RUNBOOK_DEVICE_PROBE.md``), and is reached through an intent extra on the
already-exported ``MainActivity``, adding no new exported component.

Secrets: the endpoints captured here return account balance/pnl figures, not
credentials, and the request headers (which DO carry the credential) are never
recorded. Only response bodies and status codes are written out.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["run"]

_HERE = Path(__file__).resolve()
# .../android-probe/app/src/main/python/probe/env_probe.py -- parents[1] is the
# python source root (.../src/main/python), the same belt-and-suspenders
# sys.path insert every other probe module in this package uses.
_SOURCE_ROOT = _HERE.parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))


def _p(buf: list[str], msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    line = f"{ts} ENVPROBE: {msg}"
    print(line, flush=True)
    buf.append(line)


def _save_json(output_dir: Path, name: str, obj: Any) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"{ts}_{name}.json"
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return path


#: Read-only GET paths to capture, as (save-name, path) pairs. ``{seg}`` is
#: substituted with the configured environment's path segment (``"demo/"`` or
#: ``""``); ``{env}`` with ``"demo"``/``"real"``.
#:
#: Every entry is a GET. Nothing here is environment-*mutating*; the point of
#: capturing both the segmented and unsegmented variants of the same resource
#: is precisely to see whether the broker distinguishes them, which is what a
#: correct detector needs to know.
_READ_PATHS: tuple[tuple[str, str], ...] = (
    ("balances_raw", "/api/v1/balances"),
    ("portfolio_raw", "/api/v1/trading/info/{seg}portfolio"),
    ("pnl_raw", "/api/v1/trading/info/{env}/pnl"),
    # The opposite environment's segmented paths, captured deliberately: if the
    # broker rejects the segment the credential is NOT bound to, that rejection
    # is itself a positive, *probe-able* environment signal -- which is exactly
    # what this task needs if the bodies themselves carry no marker.
    ("portfolio_raw_demo_segment", "/api/v1/trading/info/demo/portfolio"),
    ("portfolio_raw_real_segment", "/api/v1/trading/info/portfolio"),
    ("pnl_raw_demo_segment", "/api/v1/trading/info/demo/pnl"),
    ("pnl_raw_real_segment", "/api/v1/trading/info/real/pnl"),
)


def run(config_dir: str, output_dir: str) -> str:
    """Kotlin entry point. Captures raw read-only payloads, saves them as JSON.

    Args:
        config_dir: directory containing a ``config/`` subfolder with
            ``config.toml``/``.secrets.env`` pushed onto the device
            beforehand -- passed through as ``NIGHTWATCH_HOME``, exactly the
            existing ``probe.adopt_fetch``/``probe.probe_runner`` convention.
        output_dir: where the raw JSON files land, for ``adb pull``.

    Never raises -- every failure is caught and reported in the returned
    string, so a partial run still leaves whatever succeeded on disk.
    """
    buf: list[str] = []
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    os.environ["NIGHTWATCH_HOME"] = config_dir
    _p(buf, f"NIGHTWATCH_HOME set to {config_dir!r}")
    _p(buf, f"output_dir={out}")

    try:
        from nightwatch.config import load_config
        from nightwatch.etoro.client import EtoroClient
        from nightwatch.host import DesktopHost, get_host, set_host
    except Exception as e:
        _p(buf, f"FATAL: cannot import nightwatch: {e!r}")
        _p(buf, traceback.format_exc())
        return "\n".join(buf)

    previous_host = get_host()
    try:
        set_host(DesktopHost(Path(config_dir)))
        try:
            config = load_config(host=get_host())
        except Exception as e:
            _p(buf, f"FATAL: load_config failed: {e!r}")
            _p(buf, traceback.format_exc())
            return "\n".join(buf)

        env = config.etoro.environment.value
        seg = config.etoro.path_segment
        _p(buf, f"config: {config.describe()}")
        _p(buf, f"environment={env!r} path_segment={seg!r}")

        # role="read": the read-scoped credential, never the write one. repo=None
        # (default) so no sqlite file is opened.
        client = EtoroClient(config.etoro, role="read")
        captured: dict[str, Any] = {
            "configured_environment": env,
            "configured_path_segment": seg,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "endpoints": {},
        }
        try:
            for name, template in _READ_PATHS:
                path = template.format(seg=seg, env=env)
                entry: dict[str, Any] = {"path": path}
                try:
                    response = client._request("GET", path, request_class="read")
                    entry["status_code"] = response.status_code
                    try:
                        body = response.json()
                    except Exception:
                        body = {"__non_json_body__": response.text[:2000]}
                    entry["body"] = body
                    _save_json(out, name, {"path": path, "status_code": response.status_code, "body": body})
                    _p(buf, f"GET {path} -> {response.status_code}, saved {name}.json")
                    if isinstance(body, dict):
                        _p(buf, f"    top-level keys: {sorted(body.keys())!r}")
                except Exception as e:
                    # A rejection is DATA, not a failure of this probe: it is
                    # precisely the signal a path-segment-based detector would key
                    # on. Record it and keep going.
                    entry["error"] = f"{type(e).__name__}: {e}"
                    _p(buf, f"GET {path} -> ERROR {type(e).__name__}: {e}")
                captured["endpoints"][name] = entry

            _save_json(out, "env_probe_summary", captured)
            _p(buf, "saved env_probe_summary.json")
        finally:
            client.close()

        _p(buf, "env_probe.run finished -- GETs only, no order placed, no DB opened")
        return "\n".join(buf)
    finally:
        set_host(previous_host)
