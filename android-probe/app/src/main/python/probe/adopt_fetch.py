"""On-device, READ-ONLY portfolio fetch for the §4.7 adoption dry-run
migration (doc/DESIGN_market_data.md §4.7, §10.9, §10.10; the concurrent
task that built ``nightwatch.market.adopt``).

**What this is not.** This module contains NO adoption-resolution logic at
all -- no call to ``nightwatch.market.adopt.build_adoption_plan`` or
``run_adoption_backfill``, no SQLite access of any kind, no Tiingo call.
Per that module's own design ("no network call to eToro from this codepath,
ever ... the dev machine cannot reach eToro at all"), the eToro portfolio
fetch and the migration's resolution are kept in two different processes on
two different machines: this file does ONLY the on-device half (the only
half that can reach eToro, per the Kominfo DNS sinkhole recorded in
board nightwatch/handoff-2026-09-04-market-data.md §2.8); the resolution runs on the PC
against the JSON this file saves (``tools/adopt_backfill_runner.py``),
where Tiingo (unlike eToro) IS reachable.

**READ-ONLY, by construction, not just by convention:**

* ``EtoroClient(config.etoro, role="read")`` -- the read-scoped credential
  (``ETORO_READ_PRIVATE_KEY``), never the write one, matching the task's
  hard safety constraint. ``role`` is validated in the client's own
  constructor (``ValueError`` for anything but ``"write"``/``"read"``).
* Every call made here is a GET: ``get_portfolio()``, ``resolve_instrument_id``
  (a cached ``GET market-data/search``), ``get_instruments`` (a
  ``GET market-data/instruments``). Nothing in this file can place, modify,
  cancel, or close an order -- those methods are not imported, let alone
  called.
* ``repo=`` is never passed to ``EtoroClient`` -- ``resolve_instrument_id``'s
  own doc says a passed ``Repo`` gets a permanent upsert cache; passing
  ``None`` (the default) means this process touches no sqlite file, live or
  otherwise, satisfying the "don't write to the operator's live sidecar
  database" constraint the same way for a fetch step as
  ``nightwatch.market.adopt`` does for the resolution step.

Called from Kotlin via ``PyBridge.runAdoptionFetch`` -- see
``doc/RUNBOOK_DEVICE_PROBE.md`` for the config-push mechanism this reuses
unchanged (``NIGHTWATCH_HOME`` pointed at a directory with
``config/config.toml`` + ``config/.secrets.env`` already pushed on-device).
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
# .../android-probe/app/src/main/python/probe/adopt_fetch.py -- parents[1] is
# the python source root (.../src/main/python), same belt-and-suspenders
# sys.path insert every other probe module in this package uses.
_SOURCE_ROOT = _HERE.parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))


def _p(buf: list[str], msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    line = f"{ts} ADOPT_FETCH: {msg}"
    print(line, flush=True)
    buf.append(line)


def _save_json(output_dir: Path, name: str, obj: Any) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.json"
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return path


def run(config_dir: str, output_dir: str) -> str:
    """Kotlin entry point. Fetches the live portfolio (read-only) and saves
    it, plus enough instrument metadata to resolve every distinct
    ``instrument_id`` seen, as JSON under ``output_dir`` for a later
    ``adb pull`` and PC-side resolution.

    Args:
        config_dir: directory containing a ``config/`` subfolder with
            ``config.toml``/``.secrets.env`` pushed onto the device
            beforehand -- passed through as ``NIGHTWATCH_HOME``, exactly the
            existing ``probe.probe_runner.run_default`` convention.
        output_dir: where the raw JSON files land, for ``adb pull``.

    Never raises -- every failure is caught and reported in the returned
    string (mirrors every other on-device probe entry point in this
    package), so a partial run still leaves whatever succeeded on disk.
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
        from nightwatch.host import get_host
    except Exception as e:
        _p(buf, f"FATAL: cannot import nightwatch: {e!r}")
        _p(buf, traceback.format_exc())
        return "\n".join(buf)

    try:
        config = load_config(host=get_host())
    except Exception as e:
        _p(buf, f"FATAL: load_config failed: {e!r}")
        _p(buf, traceback.format_exc())
        return "\n".join(buf)

    _p(buf, f"config: {config.describe()}")
    if config.etoro.environment.value != "real":
        _p(
            buf,
            "WARNING: etoro.environment is not 'real' -- this will fetch the "
            "DEMO portfolio, not the 209 real positions the migration is "
            "about. See doc/RUNBOOK_DEVICE_PROBE.md step 3 for the "
            "device-config-copy trick (never edit the committed config.toml).",
        )

    # role="read": the read-scoped credential (ETORO_READ_PRIVATE_KEY), never
    # the write one -- and repo=None (the default) so this process never
    # opens or touches any sqlite file, live sidecar included.
    client = EtoroClient(config.etoro, role="read")
    try:
        try:
            positions = client.get_portfolio()
        except Exception as e:
            _p(buf, f"FATAL: get_portfolio() failed: {e!r}")
            _p(buf, traceback.format_exc())
            return "\n".join(buf)

        _p(buf, f"fetched {len(positions)} open positions (READ-ONLY, nothing placed/modified)")
        raw_positions = [dict(p.raw) for p in positions]
        _save_json(out, "adopt_portfolio_raw", raw_positions)
        _p(buf, f"saved {len(raw_positions)} raw position records -> adopt_portfolio_raw.json")

        distinct_ids = sorted({p.instrument_id for p in positions})
        _p(buf, f"distinct instrument_ids in portfolio: {distinct_ids}")

        managed_ids: dict[str, int] = {}
        for symbol in ("SCHG", "UPRO"):
            try:
                managed_ids[symbol] = client.resolve_instrument_id(symbol)
                _p(buf, f"resolved {symbol} -> instrument_id {managed_ids[symbol]}")
            except Exception as e:
                _p(buf, f"could not resolve {symbol}: {e!r}")
        _save_json(out, "adopt_managed_instrument_ids", managed_ids)

        if distinct_ids:
            try:
                instruments_meta = client.get_instruments(distinct_ids)
                _save_json(out, "adopt_instruments_meta_raw", instruments_meta)
                _p(buf, f"saved metadata for {len(instruments_meta)} instrument rows")
            except Exception as e:
                _p(buf, f"get_instruments FAILED (non-fatal, PC side falls back to placeholder symbols for unresolved ids): {e!r}")
    finally:
        client.close()

    _p(buf, "adopt_fetch.run finished -- read-only throughout, no order placed, no DB opened")
    return "\n".join(buf)
