"""T7 demo-only capture of search, portfolio, and rejected-order payloads.

The order path uses the order-latency harness's demo gate unchanged, then
places one at-most-once SCHG order capped at $1 and strictly below both live
real/long minimums. An unexpected terminal fill is closed only by the
position ID returned for this order's reference ID.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

__all__ = ["run"]

_HERE = Path(__file__).resolve()
_SOURCE_ROOT = _HERE.parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

_SYMBOL = "SCHG"
_AMOUNT_CAP_USD = 1.0
_POLL_INTERVAL_SECONDS = 2.0
_POLL_TIMEOUT_SECONDS = 90.0


def _log(lines: list[str], message: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} PAYLOAD_GAPS: {message}"
    print(line, flush=True)
    lines.append(line)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if any(
                token in str(key).lower()
                for token in ("token", "secret", "private", "apikey", "api-key", "userkey", "user-key", "authorization")
            ) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _save(out: Path, name: str, value: Any) -> Path:
    path = out / name
    path.write_text(json.dumps(_redact(value), indent=2, default=str), encoding="utf-8")
    return path


def _try_save(out: Path, name: str, value: Any, lines: list[str]) -> bool:
    try:
        _save(out, name, value)
        _log(lines, f"saved {name}")
        return True
    except Exception as exc:
        _log(lines, f"could not save {name}: {type(exc).__name__}")
        return False


def _rows(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, Mapping):
        rows = raw.get("items", raw.get("instruments"))
    else:
        rows = None
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("market-data/search response has no array of objects")
    return rows


def _instrument_id(row: Mapping[str, Any]) -> int:
    value = row.get("instrumentId", row.get("internalInstrumentId"))
    if isinstance(value, bool):
        raise ValueError("instrument ID is boolean")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdecimal():
        result = int(value)
    else:
        raise ValueError("instrument ID is missing or invalid")
    if result <= 0:
        raise ValueError("instrument ID must be positive")
    return result


def _positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} is missing or not numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} is not finite and positive")
    return number


def _subminimum_amount(min_exposure: object, min_real_long_amount: object) -> float:
    """Choose a positive amount <=$1 that is strictly below both minima."""
    exposure = _positive_number(min_exposure, "minPositionExposure")
    amount = _positive_number(min_real_long_amount, "real/long minPositionAmount")
    chosen = min(_AMOUNT_CAP_USD, min(exposure, amount) / 2.0)
    if not (0.0 < chosen < exposure and chosen < amount):
        raise ValueError("could not prove a positive amount below both live minimums")
    return chosen


def _portfolio_raw(client: Any) -> Any:
    response = client._request(  # noqa: SLF001
        "GET", f"/api/v1/trading/info/{client._demo()}portfolio", request_class="read"  # noqa: SLF001
    )
    return response.json()


def _positions(client: Any, raw: Any) -> list[Any]:
    envelope = raw.get("clientPortfolio", raw) if isinstance(raw, Mapping) else None
    if not isinstance(envelope, Mapping) or not isinstance(envelope.get("positions"), list):
        raise ValueError("portfolio response has no positions array")
    if any(not isinstance(row, Mapping) for row in envelope["positions"]):
        raise ValueError("portfolio positions contains a non-object row")
    return [client._parse_position(row) for row in envelope["positions"]]  # noqa: SLF001


def _capture_portfolio(client: Any, out: Path, tag: str, lines: list[str], *, required: bool) -> list[Any] | None:
    try:
        raw = _portfolio_raw(client)
        saved = _try_save(out, f"portfolio_{tag}_raw.json", raw, lines)
        positions = _positions(client, raw)
        envelope = raw.get("clientPortfolio", raw) if isinstance(raw, Mapping) else {}
        orders = envelope.get("orders")
        orders_open = envelope.get("ordersForOpen")
        _log(
            lines,
            f"portfolio {tag}: {len(positions)} positions; "
            f"orders={len(orders) if isinstance(orders, list) else 'unknown'}, "
            f"ordersForOpen={len(orders_open) if isinstance(orders_open, list) else 'unknown'}",
        )
        if required and not saved:
            raise ValueError("could not persist pre-order portfolio evidence")
        return positions
    except Exception as exc:
        _log(lines, f"portfolio {tag} failed: {type(exc).__name__}; state UNVERIFIED")
        if required:
            raise
        return None


def _search(client: Any, symbol: str) -> Any:
    response = client._request(  # noqa: SLF001
        "GET",
        "/api/v1/market-data/search",
        request_class="market_data",
        params={
            "internalSymbolFull": symbol,
            "fields": "instrumentId,internalSymbolFull,internalInstrumentDisplayName,internalExchangeName",
        },
    )
    return response.json()


def _lookup_raw(client: Any, reference_id: str) -> Any:
    response = client._request(  # noqa: SLF001
        "GET",
        f"/api/v2/trading/info/{client._demo()}orders:lookup",  # noqa: SLF001
        request_class="read",
        params={"referenceId": reference_id},
    )
    return response.json()


def _poll(client: Any, reference_id: str, out: Path, label: str, lines: list[str]) -> Any | None:
    from nightwatch.etoro.client import EtoroClient

    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            raw = _lookup_raw(client, reference_id)
            _try_save(out, f"{label}_lookup_{attempt:03d}_raw.json", raw, lines)
            result = EtoroClient._parse_order_result(raw)  # noqa: SLF001
        except Exception as exc:
            _log(lines, f"{label} lookup failed ({type(exc).__name__}); no write retry; state UNVERIFIED")
            return None
        if result.reference_id != reference_id:
            _log(lines, f"{label} lookup reference ID mismatch/missing; refusing to use its position ID")
            return None
        _log(lines, f"{label} lookup {attempt}: status={result.status.name}, position_id={result.position_id}")
        if result.is_terminal:
            return result
        if time.monotonic() >= deadline:
            _log(lines, f"{label} lookup timed out in {result.status.name}; state UNVERIFIED")
            return None
        time.sleep(_POLL_INTERVAL_SECONDS)


def run(config_dir: str, output_dir: str) -> str:
    """Capture T7 payloads; never retries an order write and never uses real credentials."""
    lines: list[str] = []
    out = Path(output_dir)
    client = None
    submit_started = False
    report: dict[str, Any] = {"status": "REFUSED", "reason": "not started"}
    stage = "output setup"
    try:
        out.mkdir(parents=True, exist_ok=True)
        attempt_path = out / "submit_attempt.json"
        if attempt_path.exists():
            raise ValueError("a submit attempt already exists; this probe is at-most-once")

        stage = "configuration"
        os.environ["NIGHTWATCH_HOME"] = config_dir
        from nightwatch.config import load_config
        from nightwatch.etoro.client import EtoroClient
        from nightwatch.etoro.errors import EtoroValidationError
        import probe_order_latency_2026_09_11 as harness

        config = load_config(
            config_path=Path(config_dir) / "config" / "config.toml",
            secrets_path=Path(config_dir) / "config" / ".secrets.env",
        )
        credential_label = config.etoro.write_credentials.label
        if config.etoro.environment.value != "demo" or not credential_label.startswith("demo:"):
            raise ValueError("config and derived write credentials must both be demo; no API call made")
        client = EtoroClient(config.etoro, role="write", max_attempts=1)

        stage = "positive demo confirmation"
        demo = harness._confirm_demo_or_die(client, config)
        _save(out, "demo_confirmation.json", dataclasses.asdict(demo))
        for reason in demo.reasons:
            _log(lines, f"gate: {reason}")
        if not demo.confirmed:
            raise ValueError("existing order-latency demo gate refused; no order attempted")
        _log(lines, "existing fail-closed demo gate positively confirmed demo")

        stage = "pre-order portfolio capture"
        before = _capture_portfolio(client, out, "before", lines, required=True)
        assert before is not None
        before_ids = {position.position_id for position in before}

        stage = "SPY and SCHG search capture"
        schg_search = None
        for symbol in ("SPY", _SYMBOL):
            try:
                raw = _search(client, symbol)
                _save(out, f"search_{symbol.lower()}_raw.json", raw)
                rows = _rows(raw)
                exact = [row for row in rows if row.get("internalSymbolFull") == symbol]
                _log(lines, f"{symbol} search saved: {len(rows)} total rows, {len(exact)} exact rows")
                if symbol == _SYMBOL:
                    schg_search = rows
            except Exception as exc:
                _log(lines, f"{symbol} search capture failed: {type(exc).__name__}")
                if symbol == _SYMBOL:
                    raise
        if schg_search is None:
            raise ValueError("SCHG search response was not captured")
        schg_exact = [row for row in schg_search if row.get("internalSymbolFull") == _SYMBOL]
        if len(schg_exact) != 1:
            raise ValueError(f"SCHG search returned {len(schg_exact)} exact rows; refusing to guess")
        instrument_id = _instrument_id(schg_exact[0])

        stage = "eligibility proof"
        eligibility = client.get_eligibility([instrument_id]).get(instrument_id)
        if eligibility is None:
            raise ValueError("no SCHG eligibility row returned")
        _save(out, "schg_eligibility_raw.json", eligibility.raw)
        raw_eligibility = eligibility.raw
        if (
            raw_eligibility.get("allowOpenPosition") is not True
            or raw_eligibility.get("instrumentId", raw_eligibility.get("instrumentID")) != instrument_id
        ):
            raise ValueError("eligibility does not positively allow SCHG or identify the requested instrument")
        exposure = _positive_number(raw_eligibility.get("minPositionExposure"), "minPositionExposure")
        configs = raw_eligibility.get("leverageConfigs")
        if not isinstance(configs, list):
            raise ValueError("eligibility has no leverageConfigs array")
        real_long = [
            row for row in configs
            if isinstance(row, Mapping)
            and row.get("settlementType") == "real"
            and row.get("direction") == "long"
        ]
        if len(real_long) != 1:
            raise ValueError("eligibility does not contain exactly one real/long minimum row")
        amount_minimum = _positive_number(real_long[0].get("minPositionAmount"), "real/long minPositionAmount")
        amount_usd = _subminimum_amount(exposure, amount_minimum)
        if eligibility.allow_open_position is not True:
            raise ValueError("parsed eligibility does not allow opening")
        _log(lines, f"SCHG instrument_id={instrument_id}; amount=${amount_usd:.6g} is below both live minima")

        stage = "at-most-once submit marker"
        request_id = str(uuid.uuid4())
        marker = {
            "request_id": request_id,
            "instrument_id": instrument_id,
            "amount_usd": amount_usd,
            "min_position_exposure": exposure,
            "real_long_min_position_amount": amount_minimum,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        with attempt_path.open("x", encoding="utf-8") as stream:
            json.dump(marker, stream)
            stream.flush()
            os.fsync(stream.fileno())

        stage = "single demo order submission"
        submit_started = True
        submit_error: Exception | None = None
        order_id = None
        try:
            order_id = client.place_buy_order(request_id, instrument_id, amount_usd)
        except Exception as exc:
            submit_error = exc
        response_body = client.last_write_response_body
        response_status = getattr(submit_error, "status_code", 200 if submit_error is None else None)
        explicit_rejection = (
            isinstance(submit_error, EtoroValidationError)
            and isinstance(response_status, int)
            and 400 <= response_status < 500
            and response_body is not None
        )
        submit_record = {
            "request_id": request_id,
            "http_status": response_status,
            "client_returned_order_id": order_id,
            "exception_type": type(submit_error).__name__ if submit_error else None,
            "explicit_http_rejection": explicit_rejection,
            "body": response_body,
        }
        evidence_saved = _try_save(out, "order_submit_response_raw.json", submit_record, lines)
        if submit_error is not None:
            _log(lines, f"submit raised {type(submit_error).__name__}; write will never be retried")

        if explicit_rejection:
            _log(lines, f"BROKER REJECTED the deliberately subminimum order (HTTP {response_status})")
            report = {"status": "REJECTED_SUBMINIMUM", "request_id": request_id, "http_status": response_status, "evidence_saved": evidence_saved}
            _capture_portfolio(client, out, "after_rejection", lines, required=False)
        else:
            order = _poll(client, request_id, out, "open_order", lines)
            after_order = _capture_portfolio(client, out, "after_order", lines, required=False)
            if order is None:
                report = {"status": "UNVERIFIED", "request_id": request_id, "reason": "order outcome not recovered", "evidence_saved": evidence_saved}
            elif not order.is_filled:
                report = {"status": "ORDER_TERMINAL_WITHOUT_FILL", "request_id": request_id, "order_status": order.status.name, "evidence_saved": evidence_saved}
                _log(lines, f"terminal {order.status.name}; no position ID to close")
            elif order.position_id is None or not order.position_id:
                report = {"status": "UNVERIFIED", "request_id": request_id, "reason": "fill has no position ID", "evidence_saved": evidence_saved}
            elif order.position_id in before_ids:
                report = {"status": "UNVERIFIED", "request_id": request_id, "reason": "returned position ID existed before submit"}
                _log(lines, "returned position ID existed before submit; refusing to close it")
            else:
                # The order's own referenceId lookup supplies the only ID eligible for cleanup.
                _log(lines, f"unexpected fill: closing only request-derived position {order.position_id}")
                close_reference = str(uuid.uuid4())
                close_id = None
                close_error: Exception | None = None
                try:
                    close_id = client.close_position(close_reference, order.position_id, instrument_id)
                except Exception as exc:
                    close_error = exc
                _try_save(
                    out,
                    "cleanup_close_submit_raw.json",
                    {
                        "request_id": close_reference,
                        "position_id": order.position_id,
                        "instrument_id": instrument_id,
                        "http_status": getattr(close_error, "status_code", 200 if close_error is None else None),
                        "order_id": close_id,
                        "exception_type": type(close_error).__name__ if close_error else None,
                        "body": client.last_write_response_body,
                    },
                    lines,
                )
                if close_error:
                    _log(lines, f"cleanup close raised {type(close_error).__name__}; no close retry")
                else:
                    _poll(client, close_reference, out, "cleanup_close", lines)
                final_positions = _capture_portfolio(client, out, "after_cleanup", lines, required=False)
                if final_positions is None:
                    report = {"status": "UNVERIFIED", "request_id": request_id, "position_id": order.position_id, "reason": "independent post-close portfolio read failed"}
                elif any(position.position_id == order.position_id for position in final_positions):
                    report = {"status": "POSITION_REMAINS_OPEN", "request_id": request_id, "position_id": order.position_id, "reason": "no further write attempted"}
                    _log(lines, f"WARNING: position {order.position_id} remains open; no further write attempted")
                else:
                    report = {"status": "POSITION_ABSENT_AFTER_CLEANUP", "request_id": request_id, "position_id": order.position_id}
                    _log(lines, f"independent portfolio confirms request-derived position {order.position_id} is absent")
                if after_order is None:
                    report["post_order_portfolio_captured"] = False

        if report.get("status") == "UNVERIFIED":
            _log(lines, "LOUD: order outcome/state is UNVERIFIED; no further trade write will be attempted")
        stage = "report"
        _save(out, "probe_report.json", report)
    except Exception as exc:
        report = {
            "status": "UNVERIFIED" if submit_started else "REFUSED",
            "stage": stage,
            "exception_type": type(exc).__name__,
            "submit_started": submit_started,
        }
        _log(lines, f"{report['status']}: stopped at {stage} ({type(exc).__name__}); no write retry")
        _log(lines, traceback.format_exc())
        _try_save(out, "probe_report.json", report, lines)
    finally:
        if client is not None:
            client.close()
    _log(lines, f"finished; status={report.get('status')}; artifacts={out}")
    return "\n".join(lines)
