"""T037 demo-only close-contract experiment. Live execution belongs to the PM.

No system/network settings are changed. A fresh run directory prevents an
accidental restart from overwriting evidence or replaying a previous run.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "src" / "main" / "python"))
from nightwatch.config import load_config  # noqa: E402
from nightwatch.etoro.client import EtoroClient, PollResult  # noqa: E402
from nightwatch.etoro.errors import EtoroValidationError  # noqa: E402
from nightwatch.etoro.models import FILLED_ORDER_STATUSES  # noqa: E402

SENSITIVE = re.compile(r"token|secret|key|auth", re.I)
PLACEHOLDER = "[REDACTED]"


class Sanitizer:
    def __init__(self) -> None:
        self.ids: dict[str, dict[str, str]] = {k: {} for k in ("ORDER", "POSITION", "REFERENCE")}
        self.accounts: set[str] = set()

    def identify(self, kind: str, value: Any) -> str:
        value = str(value)
        table = self.ids[kind]
        if value not in table:
            table[value] = f"{kind}_{len(table) + 1}"
        return table[value]

    def sanitize(self, value: Any) -> Any:
        def walk(node: Any) -> Any:
            if is_dataclass(node):
                node = asdict(node)
            if isinstance(node, dict):
                result = {}
                for key, item in node.items():
                    name = re.sub(r"[-_]", "", key).lower()
                    if name in ("authorization", "xapikey", "xuserkey", "token", "accesstoken"):
                        continue
                    if name in ("accountid", "cid", "gcid", "clientid"):
                        if item is not None:
                            self.accounts.add(str(item))
                        result[key] = "ACCOUNT"
                    else:
                        kind = {"orderid": "ORDER", "positionid": "POSITION",
                                "referenceid": "REFERENCE", "requestid": "REFERENCE",
                                "xrequestid": "REFERENCE"}.get(name)
                        result[key] = (self.identify(kind, item) if kind and item is not None
                                       else walk(item))
                return result
            if isinstance(node, (list, tuple)):
                return [walk(item) for item in node]
            return node

        def strings(node: Any) -> Any:
            if isinstance(node, dict):
                for key, item in node.items():
                    if SENSITIVE.search(key) and item != PLACEHOLDER:
                        raise ValueError("fixture refused: sensitive key remains")
                return {key: strings(item) for key, item in node.items()}
            if isinstance(node, list):
                return [strings(item) for item in node]
            if isinstance(node, str):
                replacements = {v: "ACCOUNT" for v in self.accounts}
                for table in self.ids.values():
                    replacements.update(table)
                for original in sorted(replacements, key=len, reverse=True):
                    node = re.sub(r"(?<![\w])" + re.escape(original) + r"(?![\w])",
                                  lambda _: replacements[original], node)
            return node

        return strings(walk(value))


def error_details(exc: Exception) -> dict[str, Any]:
    message = re.sub(r"(?i)Bearer\s+[^\s\"',;}]+", "Bearer " + PLACEHOLDER, str(exc))
    message = re.sub(
        r'''(?i)(["']?[\w-]*(?:token|secret|key|auth)[\w-]*["']?\s*[:=]\s*)("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|[^\s,;}]+)''',
        lambda match: match[1] + '"' + PLACEHOLDER + '"', message,
    )
    return {"error_type": type(exc).__name__, "error_message": message,
            "status_code": getattr(exc, "status_code", None)}


def write_json(path: Path, value: Any, *, durable: bool = False) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        if durable:
            os.fsync(handle.fileno())


def body_json(content: bytes) -> Any:
    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError):
        # shortcut: non-JSON bodies are redacted, review locally if a new wire format appears.
        return PLACEHOLDER


class CaptureTransport(httpx.BaseTransport):
    """Capture structured wire evidence; drop one completed response, never retry."""
    def __init__(self, inner: httpx.BaseTransport, report_path: Path) -> None:
        self.inner = inner
        self.report_path = report_path
        self.calls: list[dict[str, Any]] = []
        self.drop_reference: str | None = None
        self.close_counts: dict[str, int] = {}
        self.close_limits: dict[str, int] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        rid = request.headers.get("x-request-id")
        if request.method == "POST":
            report = json.loads(self.report_path.read_text(encoding="utf-8"))
            if not any(row["request_id"] == rid for row in report["requests"]):
                raise RuntimeError("POST refused: request id not persisted")
            if "/execution/demo/" not in request.url.path:
                raise RuntimeError("POST refused: not a demo execution path")
            if "/market-close-orders/positions/" in request.url.path:
                pid = request.url.path.rsplit("/", 1)[-1]
                count = self.close_counts.get(pid, 0)
                if count >= self.close_limits.get(pid, 0):
                    raise RuntimeError("close refused: foreign position or resend")
                self.close_counts[pid] = count + 1
        call = {"method": request.method, "path": request.url.path,
                "params": dict(request.url.params), "headers": dict(request.headers),
                "request": body_json(request.content), "started": time.time()}
        self.calls.append(call)
        try:
            response = self.inner.handle_request(request)
            response.read()
            call.update(status_code=response.status_code, response=body_json(response.content))
            if request.method == "POST" and rid == self.drop_reference:
                call["response_discarded"] = True
                response.close()
                raise httpx.ReadTimeout("experiment discarded completed response", request=request)
            return response
        except Exception as exc:
            call.update(error_details(exc))
            raise
        finally:
            call["elapsed_seconds"] = time.time() - call["started"]

    def close(self) -> None:
        self.inner.close()


class Experiment:
    def __init__(self, config: Any, run_dir: Path, fixture_dir: Path,
                 *, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 settle_timeout: float = 90.0, poll_interval: float = 2.0) -> None:
        if config.etoro.environment != "demo":
            raise ValueError("experiment refused: etoro.environment must be demo")
        self.minimum = config.strategy.min_order_size
        if not math.isfinite(self.minimum) or self.minimum <= 0:
            raise ValueError("experiment refused: invalid minimum order size")
        if not all(math.isfinite(v) and v > 0 for v in (settle_timeout, poll_interval)):
            raise ValueError("experiment refused: invalid polling bounds")
        self.clock, self.sleep = clock, sleep
        self.settle_timeout, self.poll_interval = settle_timeout, poll_interval
        self.run_dir, self.fixture_dir = run_dir, fixture_dir
        # Do not overwrite an earlier run, including a partially completed run.
        run_dir.mkdir(parents=True, exist_ok=False)
        fixture_dir.mkdir(parents=True, exist_ok=False)
        self.report_path = run_dir / "report.json"
        self.report: dict[str, Any] = {"requests": [], "steps": {}, "opened": [],
                                      "remaining_position_ids": [], "unresolved_open_requests": []}
        self.save()
        self.capture = CaptureTransport(transport if transport is not None else httpx.HTTPTransport(),
                                        self.report_path)
        self.client = EtoroClient(config.etoro, transport=self.capture, max_attempts=1,
                                  clock=clock, sleep=sleep)
        self.sanitizer = Sanitizer()
        self.owned: dict[str, Any] = {}
        self.full_attempted: set[str] = set()
        self.partial_confirmed: set[str] = set()
        self.partial_attempted: set[str] = set()
        self.full: dict[str, Any] | None = None

    def save(self) -> None:
        write_json(self.report_path, self.report, durable=True)

    def request_id(self, purpose: str, position_id: str | None = None) -> str:
        rid = str(uuid.uuid4())
        self.sanitizer.identify("REFERENCE", rid)
        self.report["requests"].append({"request_id": rid, "purpose": purpose,
                                        "position_id": position_id, "time": time.time()})
        self.save()
        return rid

    def wait_portfolio(self, pid: str, *, closed: bool = False,
                       units: float | None = None) -> tuple[list[Any], bool]:
        deadline = self.clock() + self.settle_timeout
        while True:
            portfolio = self.client.get_portfolio_strict()
            position = next((p for p in portfolio if p.position_id == pid), None)
            matched = (position is None if closed else position is not None
                       and (units is None or math.isclose(position.units, units,
                                                         rel_tol=1e-8, abs_tol=1e-8)))
            # Absence can be a stale pre-open snapshot: allow the whole settling window.
            if self.clock() >= deadline or (matched and not closed):
                return portfolio, matched
            self.sleep(max(0.0, min(self.poll_interval, deadline - self.clock())))

    def poll_close(self, oid: str) -> dict[str, Any]:
        deadline = self.clock() + self.settle_timeout
        lookup, poll, errors = None, None, []
        attempts = 0
        while True:
            attempts += 1
            try:
                order = self.client.lookup_close_order(oid)
                if lookup is None:
                    lookup = asdict(order)
                poll = asdict(PollResult(order, attempts, not order.is_terminal))
                if order.is_terminal:
                    break
            except EtoroValidationError as exc:
                if exc.status_code != 404:
                    raise
                errors.append(error_details(exc))
            if self.clock() >= deadline:
                break
            self.sleep(max(0.0, min(self.poll_interval, deadline - self.clock())))
        return {"lookup": lookup, "poll": poll, "lookup_errors": errors}

    @staticmethod
    def close_lookup_matches(evidence: dict[str, Any], pid: str) -> bool:
        poll = evidence["poll"]
        # Only an unavailable lookup falls back to portfolio proof, not a rejection.
        return poll is None or (not poll["timed_out"]
                                and poll["order"]["status"] in FILLED_ORDER_STATUSES
                                and poll["order"]["position_id"] == pid)

    def lookup_visible(self, *, order_id: str | None = None,
                       reference_id: str | None = None, terminal: bool = False) -> Any:
        deadline = self.clock() + self.settle_timeout
        while True:
            try:
                order = self.client.lookup_order(order_id=order_id, reference_id=reference_id)
                if not terminal or order.is_terminal or self.clock() >= deadline:
                    return order
            except EtoroValidationError as exc:
                if exc.status_code != 404 or self.clock() >= deadline:
                    raise
            self.sleep(max(0.0, min(self.poll_interval, deadline - self.clock())))

    def open_position(self, amount: float) -> Any:
        before = {p.position_id for p in self.client.get_portfolio_strict()}
        rid = self.request_id("open")
        self.report["unresolved_open_requests"].append(rid)
        self.save()
        oid = self.client.place_buy_order(rid, self.instrument_id, amount)
        self.sanitizer.identify("ORDER", oid)
        position = None
        try:
            order = self.lookup_visible(order_id=oid, terminal=True)
        except EtoroValidationError as exc:
            if exc.status_code != 404:
                raise
            order = None
        if order is None:
            deadline = self.clock() + self.settle_timeout
            while True:
                candidates = [p for p in self.client.get_portfolio_strict()
                              if p.instrument_id == self.instrument_id
                              and p.position_id not in before | self.baseline | self.owned.keys()]
                if len(candidates) > 1:
                    raise RuntimeError("open refused: ambiguous new portfolio positions")
                if candidates:
                    position = candidates[0]
                    break
                if self.clock() >= deadline:
                    raise RuntimeError("open unresolved: lookup unavailable and no new portfolio position")
                self.sleep(max(0.0, min(self.poll_interval, deadline - self.clock())))
            pid = position.position_id
        else:
            pid = order.position_id
        if pid and pid not in before and pid not in self.baseline and pid not in self.owned:
            self.sanitizer.identify("POSITION", pid)
            self.report["opened"].append(pid)
            self.report["unresolved_open_requests"].remove(rid)
            self.owned[pid] = None
            self.save()
        else:
            raise RuntimeError("open refused: no proven new position identity")
        if position is None:
            portfolio, _ = self.wait_portfolio(pid)
            position = next((p for p in portfolio if p.position_id == pid), None)
        if (order is not None and not order.is_filled) or position is None:
            raise RuntimeError("open not confirmed filled and present")
        if position.instrument_id != self.instrument_id:
            raise RuntimeError("open position instrument mismatch")
        self.owned[pid] = position
        return position

    def close_position(self, position: Any, *, units: float | None = None,
                       discard: bool = False) -> tuple[str, str | None]:
        pid = position.position_id
        if pid not in self.owned or pid in self.full_attempted:
            raise RuntimeError("close refused: foreign position or resend")
        if pid in self.partial_attempted and pid not in self.partial_confirmed:
            raise RuntimeError("close refused: uncertain partial close")
        rid = self.request_id("partial" if units is not None else "full", pid)
        if units is None:
            self.full_attempted.add(pid)
        else:
            self.partial_attempted.add(pid)
        self.capture.close_limits[pid] = (2 if pid in self.partial_confirmed else 1)
        if discard:
            self.capture.drop_reference = rid
        try:
            oid = self.client.close_position(rid, pid, self.instrument_id, units)
        except Exception:
            if not discard:
                raise
            oid = None
        finally:
            self.capture.drop_reference = None
        if oid:
            self.sanitizer.identify("ORDER", oid)
        return rid, oid

    def reference_probe(self, rid: str, pid: str, oid: str | None) -> dict[str, Any]:
        try:
            result = self.lookup_visible(reference_id=rid)
            return {"result": asdict(result), "matches": result.position_id == pid
                    and result.order_id is not None and (oid is None or result.order_id == oid)}
        except Exception as exc:
            return {**error_details(exc), "matches": False}

    def step(self, number: int) -> dict[str, Any]:
        if number == 1:
            position = self.open_position(self.minimum)
            before = self.client.get_portfolio_strict()
            balance = self.client.get_balance()
            rid, oid = self.close_position(position)
            self.full = {"reference_id": rid, "position_id": position.position_id, "order_id": oid,
                         "balance_before": balance.available_cash}
            return {**self.full, "portfolio_before": [asdict(p) for p in before],
                    "balance": asdict(balance), "acknowledged": oid is not None}
        if number == 2:
            if self.full is None:
                raise RuntimeError("step 2 depends on step 1")
            evidence = self.poll_close(self.full["order_id"])
            portfolio, gone = self.wait_portfolio(self.full["position_id"], closed=True)
            balance = self.client.get_balance()
            return {**evidence, "position_gone": gone,
                    "portfolio": [asdict(p) for p in portfolio], "balance": asdict(balance),
                    "cash_change": balance.available_cash - self.full["balance_before"],
                    "passed": gone and self.close_lookup_matches(evidence, self.full["position_id"])}
        if number == 3:
            if self.full is None:
                raise RuntimeError("step 3 depends on step 1")
            return self.reference_probe(self.full["reference_id"], self.full["position_id"],
                                        self.full["order_id"])
        if number == 4:
            position = self.open_position(max(2.5 * self.minimum, self.minimum))
            rid, oid = self.close_position(position, units=position.units / 2)
            evidence = self.poll_close(oid)
            portfolio, reduced = self.wait_portfolio(position.position_id, units=position.units / 2)
            confirmed = reduced and self.close_lookup_matches(evidence, position.position_id)
            if confirmed:
                self.partial_confirmed.add(position.position_id)
            return {"reference_id": rid, "before": asdict(position), **evidence,
                    "portfolio": [asdict(p) for p in portfolio], "passed": confirmed}
        position = self.open_position(self.minimum)
        before = self.client.get_portfolio_strict()
        rid, _ = self.close_position(position, discard=True)
        probe = self.reference_probe(rid, position.position_id, None)
        after, gone = self.wait_portfolio(position.position_id, closed=True)
        count = self.capture.close_counts.get(position.position_id, 0)
        dropped = any(c.get("response_discarded") and c["headers"].get("x-request-id") == rid
                      for c in self.capture.calls)
        if count != 1:
            raise RuntimeError("lost-response experiment requires exactly one close POST")
        return {"reference_id": rid, "probe": probe, "close_post_count": count,
                "response_discarded": dropped, "portfolio_before": [asdict(p) for p in before],
                "portfolio_after": [asdict(p) for p in after],
                "passed": dropped and gone}

    def cleanup(self) -> dict[str, Any]:
        errors = []
        remaining = set(self.owned)
        for pid in self.owned:
            if pid not in self.full_attempted:
                portfolio, present = self.wait_portfolio(pid)
                position = next((p for p in portfolio if p.position_id == pid), None)
                if not present or position.instrument_id != self.instrument_id:
                    continue
                if pid in self.partial_attempted and pid not in self.partial_confirmed:
                    continue
                self.owned[pid] = position
                try:
                    _, oid = self.close_position(position)
                    self.poll_close(oid)
                except Exception as exc:
                    errors.append({"position_id": pid, **error_details(exc)})
            # Never infer closure for an order-identified position not yet seen in portfolio.
            if self.owned[pid] is not None:
                _, gone = self.wait_portfolio(pid, closed=True)
                if gone:
                    remaining.discard(pid)
        self.report["remaining_position_ids"] = sorted(remaining)
        return {"remaining_position_ids": sorted(remaining), "errors": errors}

    def run(self, steps: Sequence[int]) -> dict[str, Any]:
        try:
            self.baseline = {p.position_id for p in self.client.get_portfolio_strict()}
            self.instrument_id = self.client.resolve_instrument_id("SCHG")
            for number in steps:
                start, offset = time.time(), len(self.capture.calls)
                try:
                    result = self.step(number)
                    status = "pass" if result.get("passed", result.get("matches", True)) else "fail"
                except Exception as exc:
                    result, status = error_details(exc), "fail"
                evidence = {"step": number, "status": status, "result": result,
                            "calls": self.capture.calls[offset:]}
                self.report["steps"][str(number)] = {"status": status, "elapsed_seconds": time.time() - start}
                self.save()
                write_json(self.fixture_dir / f"step_{number}.json", self.sanitizer.sanitize(evidence))
                if self.report["unresolved_open_requests"]:
                    break
        finally:
            try:
                if hasattr(self, "instrument_id"):
                    cleanup = self.cleanup()
                else:
                    cleanup = {"not_started": True}
                self.report["cleanup"] = cleanup
            except Exception as exc:
                self.report["cleanup"] = error_details(exc)
                self.report["remaining_position_ids"] = sorted(self.owned)
            finally:
                self.save()
                self.client.close()
        write_json(self.fixture_dir / "summary.json", self.sanitizer.sanitize(self.report))
        return self.report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--secrets", type=Path)
    parser.add_argument("--steps", default="1,2,3,4,5")
    parser.add_argument("--dry-plan", action="store_true")
    args = parser.parse_args(argv)
    try:
        steps = [int(s) for s in args.steps.split(",")]
    except ValueError:
        parser.error("steps must be comma-separated integers")
    if not steps or steps != sorted(set(steps)) or any(s not in range(1, 6) for s in steps):
        parser.error("steps must be unique, increasing values from 1 through 5")
    if any(s in steps for s in (2, 3)) and 1 not in steps:
        parser.error("steps 2 and 3 require step 1")
    if args.dry_plan:
        # Default dry-plan needs neither local credentials nor configuration files.
        if args.config is not None:
            config = load_config(config_path=args.config, secrets_path=args.secrets)
            if config.etoro.environment != "demo":
                parser.error("experiment refused: etoro.environment must be demo")
        print("DEMO ONLY; no network calls; SCHG at strategy.min_order_size (step 4: 2.5x)")
        plans = {1: "GET portfolio; POST buy; GET open poll/portfolio/balance; POST full close",
                 2: "GET close-orders/{orderId}; GET close poll; GET portfolio/balance",
                 3: "GET orders:lookup?referenceId=<step-1-rid>; match order and position",
                 4: "GET portfolio; POST buy 2.5x; GET open poll; POST half-units close; GET close poll/portfolio",
                 5: "GET portfolio; POST buy; GET open poll; POST close once, discard response; GET reference/portfolio"}
        for step in steps:
            print(f"{step}: {plans[step]}")
        print("Finally: GET portfolio; close only this run's confirmed remainders/untouched positions; report leftovers")
        return 0
    config = load_config(config_path=args.config, secrets_path=args.secrets)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S_%f")
    experiment = Experiment(config, ROOT / "data" / f"close_experiment_{stamp}",
                            ROOT / "tests" / "fixtures" / "etoro_contracts" / f"close_experiment_{stamp}")
    report = experiment.run(steps)
    print(f"Report: {experiment.report_path}")
    return int(len(report["steps"]) != len(steps)
               or any(s["status"] != "pass" for s in report["steps"].values())
               or bool(report["remaining_position_ids"] or report["unresolved_open_requests"])
               or "error_type" in report.get("cleanup", {}))


if __name__ == "__main__":
    raise SystemExit(main())
