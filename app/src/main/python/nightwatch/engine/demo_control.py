"""Demo-only controls; trusted authorization is supplied by the device adapter.

No dispatch, credentials, strategy, resume, or broker writes live here. SQLite's
write transaction serializes pause with the final transport call across processes.
"""

from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import math
import re
import sqlite3
import time

from ..host import Host


ACTIONS = frozenset({"refresh_demo_broker", "pause_demo_orders"})


class DemoSubmissionPaused(RuntimeError):
    """A new demo submission was refused locally, not by the broker."""


@contextmanager
def _transaction(host: Host):
    path = host.halt_flag_path().with_name("DEMO_CONTROL.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS pause (singleton INTEGER PRIMARY KEY CHECK(singleton=1))")
        connection.execute("CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY)")
        yield connection
        connection.commit()
    finally:
        connection.close()


@contextmanager
def demo_submission_guard(host: Host):
    """Hold until the actual HTTP call returns; an earlier admission is insufficient."""
    with _transaction(host) as connection:
        if connection.execute("SELECT 1 FROM pause").fetchone():
            raise DemoSubmissionPaused("tool refused new demo submission: persisted pause")
        try:
            host.halt_flag_path().stat()
        except FileNotFoundError:
            pass
        else:
            raise DemoSubmissionPaused("tool refused new demo submission: HALT")
        yield


def check_demo_submission(host: Host) -> None:
    """Non-transmitting admission check using exactly the live submission guard."""
    with demo_submission_guard(host):
        pass


def _time_ms(value):
    return type(value) is int and 0 <= value <= 9_007_199_254_620_991


def _portfolio_evidence(broker, *, issued_at, deadline_at, clock):
    observation = {"account": False, "positions": False, "openOrders": False, "observedAt": None}
    evidence = {"brokerObservation": observation, "portfolioArrays": {},
                "portfolioRead": {"status": "unavailable", "scope": "clientPortfolio_top_level"}}
    started = clock()
    if started >= deadline_at:
        return evidence
    broker.set_deadline_in((deadline_at - started) / 1000)
    try:
        # Fixed read-only demo surface, never a request-supplied path or operation.
        response = broker._request("GET", "/api/v1/trading/info/demo/portfolio", request_class="read")
        completed = clock()
        source_at, source_status = None, "missing"
        if "Date" in response.headers:
            source_status = "invalid"
            try:
                source = parsedate_to_datetime(response.headers["Date"])
                if source.utcoffset() is not None:
                    source_at = int(source.timestamp() * 1000)
                    if _time_ms(source_at):
                        source_status = ("stale" if source_at <= issued_at else
                                         "future" if source_at > completed else "response_time")
                    else:
                        source_at = None
            except (TypeError, ValueError, OverflowError):
                pass
        # HTTP Date is a response-generation time, not broker observation proof.
        timing = dict(readStartedAt=started, readCompletedAt=completed,
                      sourceResponseAt=source_at, sourceTimeStatus=source_status)
        evidence["portfolioRead"].update(timing, status="invalid_envelope")
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("clientPortfolio"), dict):
            return evidence
        data = payload["clientPortfolio"]
        evidence["portfolioRead"]["status"] = "read"
        # No totals: these documented categories may overlap. Obsolete fields stay labelled by name.
        for field in ("positions", "mirrors", "orders", "ordersForOpen", "ordersForClose",
                      "ordersForCloseMultiple", "stockOrders", "entryOrders", "exitOrders"):
            value = data.get(field)
            if field not in data:
                state, count = "absent", None
            elif value is None:
                state, count = "null", None
            elif not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
                state, count = "invalid", None
            else:
                state, count = ("empty" if not value else "present"), len(value)
            evidence["portfolioArrays"][field] = dict(state=state, count=count)
        credit = data.get("credit")
        if type(credit) in (int, float) and math.isfinite(credit):
            observation["account"] = True
            evidence["accountRead"] = dict(timing)
        if evidence["portfolioArrays"]["positions"]["state"] in ("empty", "present"):
            try:
                for row in data["positions"]:
                    broker._parse_position(row)
            except Exception:
                pass
            else:
                observation["positions"] = True
                evidence["positionsRead"] = dict(timing)
        return evidence
    except Exception:
        return evidence  # No foreign error text or raw broker records in receipts.


def handle_demo_control(request, *, host: Host, device_id: str,
                        operator_write_authorized: bool = False,
                        broker=None, clock=lambda: time.time_ns() // 1_000_000):
    """Apply an authenticated adapter request, never authorization fields in JSON.

    ``broker`` is a dedicated read-role demo EtoroClient owned by the adapter.
    Refresh deliberately remains partial: documented pending-order categories do
    not establish exhaustive coverage or absent-vs-empty inventory semantics.
    """
    if operator_write_authorized is not True:
        return {"status": "refused", "reasonCode": "write_authority_required"}
    try:
        valid_id = isinstance(request["requestId"], str) and re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            request["requestId"], re.IGNORECASE,
        ) is not None
        valid = (valid_id and request["action"] in ACTIONS and
                 request["environment"] == "demo" and isinstance(device_id, str) and
                 bool(device_id.strip()) and request["deviceId"] == device_id and
                 _time_ms(request["issuedAt"]) and type(request["deadlineAt"]) is int and
                 request["deadlineAt"] == request["issuedAt"] + 120_000)
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        return {"status": "refused", "reasonCode": "invalid_demo_request"}
    receipt = {key: request[key] for key in ("requestId", "action", "deviceId", "environment")}
    now = clock()
    if not _time_ms(now) or now < request["issuedAt"]:
        return dict(receipt, status="refused", reasonCode="invalid_time")
    if now >= request["deadlineAt"]:
        return dict(receipt, status="expired_unconfirmed", reasonCode="deadline")
    try:
        with _transaction(host) as connection:
            # Recheck after waiting for a submission already in flight.
            now = clock()
            if not _time_ms(now) or not request["issuedAt"] <= now < request["deadlineAt"]:
                return dict(receipt, status="expired_unconfirmed", reasonCode="deadline")
            if connection.execute("SELECT 1 FROM requests WHERE request_id=?", (request["requestId"].lower(),)).fetchone():
                return dict(receipt, status="refused", reasonCode="duplicate")
            if request["action"] == "refresh_demo_broker" and not (
                broker is not None and broker.environment == "demo" and broker.read_only is True
            ):
                return dict(receipt, status="refused", reasonCode="read_only_demo_client_required")
            connection.execute("INSERT INTO requests VALUES (?)", (request["requestId"].lower(),))
            if request["action"] == "pause_demo_orders":
                connection.execute("INSERT OR IGNORE INTO pause VALUES (1)")
        if request["action"] == "pause_demo_orders":
            try:
                check_demo_submission(host)
            except DemoSubmissionPaused:
                receipt.update(pausePersisted=True, finalSubmissionGuard=True)
            else:
                return dict(receipt, status="unconfirmed", reasonCode="guard_not_enforced")
            completed = clock()
            if not _time_ms(completed) or completed < now:
                return dict(receipt, status="unconfirmed", reasonCode="invalid_time")
            return dict(receipt, executedAt=completed,
                        status="applied" if completed < request["deadlineAt"] else "expired_unconfirmed")
        evidence = _portfolio_evidence(broker, issued_at=request["issuedAt"],
                                       deadline_at=request["deadlineAt"], clock=clock)
        completed = clock()
        if not _time_ms(completed) or completed < now:
            return dict(receipt, status="unconfirmed", reasonCode="invalid_time")
        # shortcut: exhaustive order coverage is unproved, upgrade only with documented coverage and fixtures.
        return dict(receipt, status="partial" if completed < request["deadlineAt"] else "expired_unconfirmed",
                    executedAt=completed, **evidence, reasonCode="open_orders_unestablished")
    except (OSError, sqlite3.Error):
        return dict(receipt, status="unconfirmed", reasonCode="control_storage_unavailable")
