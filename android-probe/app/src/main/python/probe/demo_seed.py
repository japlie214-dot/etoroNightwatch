"""Alarm-only one-shot demo seed over the existing buy and adoption runners."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

from nightwatch.config import load_config
from nightwatch.etoro.client import EtoroClient
from nightwatch.host import DesktopHost, get_host, set_host
from probe import adopt_apply, demo_manual_buy

_sleep = time.sleep
_monotonic = time.monotonic


def run(home: str) -> str:
    """One-shot unattended demo seed. Returns JSON; never raises."""
    started = _monotonic()
    previous_host = get_host()
    result = dict(status="refused", count=None, usd_each=None, accepted=0,
                  buy_complete=False, positions_visible=False, adoption_attempts=0,
                  started_utc=datetime.now(timezone.utc).isoformat(), finished_utc=None, steps=[])
    root = None
    persist = False
    claimed = False
    phase = "buy"
    adopted = False
    stage = "home"

    def step(name, status, detail):
        result["steps"].append(dict(step=name, status=status, detail=detail))

    def budget():
        if _monotonic() - started < 600:
            return True
        result["status"] = "budget_exceeded"
        step("budget", "budget_exceeded", f"before {stage}")
        return False

    def seed():
        nonlocal root, persist, claimed, phase, stage
        root = Path(home).resolve()
        if root.name != "nightwatch_demo":
            step(stage, "refused", "dedicated nightwatch_demo home required")
            return
        adoption_flag = root / "SEED_DEMO.adopt"
        if adoption_flag.exists():
            phase = "adopt"
            persist = True
            for key in ("count", "usd_each", "accepted", "buy_complete", "positions_visible"):
                del result[key]
            result["alarm_number"] = None
            stage = "adoption flag"
            try:
                text = adoption_flag.read_text(encoding="ascii")
                if re.fullmatch(r"\s*[+]?[0-9]+\s*", text) is None:
                    raise ValueError
                used = int(text)
                if not 0 <= used < 5:
                    raise ValueError
            except (ValueError, UnicodeError):
                adoption_flag.replace(root / "SEED_DEMO.adopt.failed")
                result["status"] = "adoption_abandoned"
                step(stage, "adoption_abandoned", "expected alarm count 0 to 4; five alarms exhaust adoption")
                return
            result["alarm_number"] = used + 1
            temporary = root / "SEED_DEMO.adopt.tmp"
            temporary.write_text(str(used + 1), encoding="ascii")
            temporary.replace(adoption_flag)
            step(stage, "claimed", f"alarm number={used + 1}")
            adoption()
            return
        stage = "flag"
        flag = root / "SEED_DEMO"
        try:
            with flag.open("rb") as stream:
                raw = stream.read(33)
        except OSError as exc:
            step(stage, "refused", f"flag unreadable ({type(exc).__name__})")
            return
        try:
            if len(raw) > 32:
                raise ValueError
            text = raw.decode("ascii")
            if re.fullmatch(r"\s*[+-]?[0-9]+\s+[+-]?[0-9]+\s*", text) is None:
                raise ValueError
            count, usd_each = map(int, text.split())
            if not 1 <= count <= 10 or not 50 <= usd_each <= 15000:
                raise ValueError
        except (ValueError, UnicodeError):
            flag.replace(root / "SEED_DEMO.done")
            persist = True
            step(stage, "refused", "expected count 1 to 10 and integer USD each 50 to 15000; at most 32 ASCII bytes")
            return
        result.update(count=count, usd_each=usd_each)
        stage = "claim"
        try:
            flag.replace(root / "SEED_DEMO.running")
        except OSError as exc:
            step(stage, "refused", f"flag rename failed ({type(exc).__name__})")
            return
        persist = claimed = True
        step(stage, "claimed", "one-shot flag consumed")
        host = DesktopHost(root)
        set_host(host)
        stage = "baseline"
        try:
            config = load_config(host=host)
            client = EtoroClient(config.etoro, role="read", repo=None)
            with closing(client):
                instrument_id = client.resolve_instrument_id("SCHG")
                baseline = sum(p.instrument_id == instrument_id for p in client.get_portfolio_strict())
                step(stage, "read", f"SCHG positions={baseline}")
                buy_and_adopt(client, instrument_id, baseline, count, usd_each)
        except Exception as exc:
            if stage != "baseline":
                raise
            result["status"] = "refused"
            step(stage, "refused", f"portfolio unreadable ({type(exc).__name__})")

    def buy_and_adopt(client, instrument_id, baseline, count, usd_each):
        nonlocal stage
        stage = "buy"
        if not budget():
            return
        # The recovery flag exists BEFORE any order can be accepted: a kill or an
        # exception anywhere after this line still leaves a later alarm to adopt.
        stage = "recovery flag"
        (root / "SEED_DEMO.adopt").write_text("0", encoding="ascii")
        stage = "buy"
        text = demo_manual_buy.run(home, count, usd_each)
        result["accepted"] = len(re.findall(r"^Order \d+/\d+: accepted", text, re.MULTILINE))
        result["buy_complete"] = any(line.startswith("Demo buy complete") for line in text.splitlines())
        step(stage, "complete" if result["buy_complete"] else "incomplete", text)
        if result["accepted"] == 0:
            # The runner returned normally and reported no accepted order: nothing to adopt.
            stage = "recovery flag removal"
            (root / "SEED_DEMO.adopt").unlink(missing_ok=True)
            result["status"] = "buy_refused_or_failed"
            return
        stage = "positions"
        wait_started = _monotonic()
        while True:
            if not budget():
                return
            visible = sum(p.instrument_id == instrument_id for p in client.get_portfolio_strict())
            if visible >= baseline + result["accepted"]:
                result["positions_visible"] = True
                break
            remaining = 180 - (_monotonic() - wait_started)
            if remaining <= 0:
                break
            _sleep(min(10, remaining, max(0, 600 - (_monotonic() - started))))
        step(stage, "visible" if result["positions_visible"] else "timeout", f"SCHG positions={visible}")
        adoption()
        if adopted:
            result["status"] = ("seeded" if result["buy_complete"]
                                and result["accepted"] == count and result["positions_visible"]
                                else "seeded_partial")

    def adoption():
        nonlocal stage, adopted
        for attempt in range(1, 4):
            stage = f"adoption {attempt} plan"
            if not budget():
                return
            result["adoption_attempts"] = attempt
            planned = json.loads(adopt_apply.plan(home))
            step(stage, planned["status"], planned["report"])
            if planned["status"] == "planned":
                stage = f"adoption {attempt} apply"
                if not budget():
                    return
                applied = json.loads(adopt_apply.apply(home, planned["plan_id"]))
                step(stage, applied["status"], applied["report"])
                if applied["status"] == "applied":
                    adopted = True
                    return
            if attempt < 3:
                _sleep(min(10, max(0, 600 - (_monotonic() - started))))

    try:
        seed()
    except Exception as exc:
        result["status"] = "error"
        step(stage, "error", f"{stage} ({type(exc).__name__})")
    finally:
        set_host(previous_host)
    result["finished_utc"] = datetime.now(timezone.utc).isoformat()
    try:
        if phase == "adopt":
            if adopted:
                stage = "adoption flag removal"
                (root / "SEED_DEMO.adopt").unlink()
                result["status"] = "adopted"
            elif result["status"] != "adoption_abandoned":
                result["status"] = "adoption_pending"
        elif claimed and adopted:
            stage = "recovery flag removal"
            (root / "SEED_DEMO.adopt").unlink(missing_ok=True)
        elif claimed and (root / "SEED_DEMO.adopt").exists():
            # Orders were accepted, or may have been (the buy runner did not return normally).
            if result["accepted"] > 0:
                result["status"] = "bought_adoption_pending"
            step("recovery flag", "kept", "a later alarm will try adoption")
        raw = json.dumps(result, allow_nan=False)
        if persist:
            stage = "result persistence"
            filename = "SEED_DEMO.adopt.done" if phase == "adopt" else "SEED_DEMO.done"
            temporary = root / (filename + ".tmp")
            temporary.write_text(raw, encoding="utf-8")
            temporary.replace(root / filename)
            if claimed:
                (root / "SEED_DEMO.running").unlink(missing_ok=True)
    except Exception as exc:
        result["status"] = "error"
        step(stage, "error", f"{stage} ({type(exc).__name__})")
        raw = json.dumps(result, allow_nan=False)
    print(f"Demo seed: status={result['status']}", flush=True)
    return raw
