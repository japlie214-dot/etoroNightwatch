"""One-off, explicitly confirmed real-account SCHG take-profit probe.

This sends one $10 market buy through the shared eight-field
``EtoroClient.place_buy_order`` body, then reads the same position back. It
never changes or suppresses take profit and deliberately leaves any fill open.
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from nightwatch.etoro.models import FILLED_ORDER_STATUSES

CONFIRMATION = "PLACE REAL $10 SCHG ORDER"
ETORO_API_BASE_URL = "https://public-api.etoro.com"
SYMBOL = "SCHG"
AMOUNT_USD = 10.0
POLL_INTERVAL_SECONDS = 2.0
POLL_TIMEOUT_SECONDS = 120.0
TAKE_PROFIT_RATIO = 11.0
TAKE_PROFIT_RATIO_REL_TOL = 0.001


def safety_gate_failures(
    configured_environment: object,
    credential_label: object,
    verified_environment: object,
    confirmation: object,
    api_base_url: object = ETORO_API_BASE_URL,
) -> tuple[str, ...]:
    """Return every failed explicit authorization gate; unknowns fail closed."""
    failures = []
    if configured_environment != "real":
        failures.append("configured environment is not exactly real")
    if credential_label != "real:write":
        failures.append("credential label is not exactly real:write")
    if verified_environment != "real":
        failures.append("live credential environment was not positively verified as real")
    if type(confirmation) is not str or confirmation != CONFIRMATION:
        failures.append("explicit confirmation extra does not match exactly")
    if api_base_url != ETORO_API_BASE_URL:
        failures.append("API base URL is not the canonical eToro host")
    return tuple(failures)


def consume_approval_token(path: Path, supplied_token: object) -> None:
    """Require and delete the app-private one-use operator token."""
    if type(supplied_token) is not str or not supplied_token:
        raise ValueError("private approval token is missing")
    try:
        stored_token = path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError:
        raise ValueError("private approval token is missing") from None
    if not stored_token or not secrets.compare_digest(stored_token, supplied_token):
        raise ValueError("private approval token does not match")
    try:
        path.unlink()
    except OSError:
        raise ValueError("private approval token could not be consumed") from None


def _finite_number(value: object, *, positive: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def require_filled_position_id(status: object, position_id: object) -> str:
    try:
        is_filled = status in FILLED_ORDER_STATUSES
    except TypeError:
        is_filled = False
    if not is_filled or not isinstance(position_id, str) or not position_id:
        raise ValueError(
            f"order status {getattr(status, 'name', None)!r} is not FILLED with a position ID"
        )
    return position_id


def evaluate_position_readback(
    raw: Mapping[str, Any], *, expected_position_id: str, expected_instrument_id: int
) -> dict[str, Any]:
    """Validate exact identity and return the observed broker TP verdict."""
    position_id = next(
        (raw[key] for key in ("positionID", "positionId", "id") if raw.get(key) is not None),
        None,
    )
    instrument_id = raw.get("instrumentID", raw.get("instrumentId"))
    if str(position_id) != expected_position_id:
        raise ValueError("readback position ID does not match filled order")
    if isinstance(instrument_id, bool) or instrument_id != expected_instrument_id:
        raise ValueError("readback instrument does not match SCHG")
    if raw.get("isBuy") is not True:
        raise ValueError("readback does not positively identify a buy position")
    if _finite_number(raw.get("leverage")) != 1.0:
        raise ValueError("readback leverage is not exactly 1")

    open_rate = _finite_number(raw.get("openRate"), positive=True)
    if open_rate is None:
        raise ValueError("readback openRate is missing or invalid")
    no_take_profit = raw.get("isNoTakeProfit")
    if type(no_take_profit) is not bool:
        raise ValueError("readback isNoTakeProfit is missing or invalid")
    take_profit_rate = _finite_number(raw.get("takeProfitRate"))
    ratio = None if take_profit_rate is None else take_profit_rate / open_rate

    if no_take_profit:
        verdict = "NO_TAKE_PROFIT"
    elif ratio is not None and math.isclose(
        ratio, TAKE_PROFIT_RATIO, rel_tol=TAKE_PROFIT_RATIO_REL_TOL
    ):
        verdict = "DEFAULT_11X_TAKE_PROFIT"
    else:
        raise ValueError("readback take-profit state is neither no-TP nor approximately 11x")

    return {
        "position_id": expected_position_id,
        "instrument_id": expected_instrument_id,
        "open_rate": open_rate,
        "take_profit_rate": take_profit_rate,
        "take_profit_rate_over_open_rate": ratio,
        "is_no_take_profit": no_take_profit,
        "verdict": verdict,
    }


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if any(
                token in str(key).lower()
                for token in (
                    "token", "secret", "private", "apikey", "api-key", "userkey",
                    "user-key", "authorization",
                )
            ) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _save_json(directory: Path, name: str, payload: Any) -> Path:
    path = directory / name
    path.write_text(json.dumps(_redact(payload), indent=2, default=str), encoding="utf-8")
    return path


def _log(lines: list[str], message: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} TP_PROBE: {message}"
    print(line, flush=True)
    lines.append(line)


def _parse_positions(client: Any, envelope: Mapping[str, Any]) -> list[Any]:
    rows = envelope.get("positions")
    if not isinstance(rows, list):
        raise ValueError("portfolio positions field is missing or invalid")
    return [client._parse_position(row) for row in rows]  # noqa: SLF001


def run(
    config_dir: str,
    output_dir: str,
    confirmation: str = "",
    approval_token: str = "",
) -> str:
    """Kotlin entry point. Never raises and never retries the order submission."""
    lines: list[str] = []
    out = Path(output_dir)
    client = None
    stage = "output setup"
    try:
        out.mkdir(parents=True, exist_ok=True)
        attempt_path = out / "submit_attempt.json"
        if attempt_path.exists():
            raise ValueError("a submit attempt already exists; this probe is at-most-once")

        stage = "configuration loading"
        os.environ["NIGHTWATCH_HOME"] = config_dir
        from nightwatch.config import load_config
        from nightwatch.etoro.client import EtoroClient
        from nightwatch.etoro.errors import EtoroValidationError

        config = load_config(
            config_path=Path(config_dir) / "config" / "config.toml",
            secrets_path=Path(config_dir) / "config" / ".secrets.env",
        )
        credential_label = config.etoro.write_credentials.label
        if config.etoro.environment.value != "real" or credential_label != "real:write":
            raise ValueError("configured real environment and real:write credential are required")
        if config.etoro.base_url != ETORO_API_BASE_URL:
            raise ValueError("API base URL is not the canonical eToro host")
        if type(confirmation) is not str or confirmation != CONFIRMATION:
            raise ValueError("explicit confirmation extra does not match exactly")
        stage = "private one-use operator approval"
        consume_approval_token(out / "approval.token", approval_token)

        stage = "live credential environment verification"
        client = EtoroClient(config.etoro, role="write", max_attempts=1)
        verified_environment = client.probe_environment()
        failures = safety_gate_failures(
            config.etoro.environment.value,
            credential_label,
            verified_environment,
            confirmation,
            config.etoro.base_url,
        )
        if failures:
            raise ValueError("; ".join(failures))
        _save_json(
            out,
            "authorization_gate.json",
            {
                "configured_environment": config.etoro.environment.value,
                "credential_label": credential_label,
                "verified_environment": verified_environment,
                "canonical_api_host": True,
                "confirmation_matches": confirmation == CONFIRMATION,
            },
        )
        _log(lines, "all real-account authorization gates passed")

        stage = "pre-order portfolio read"
        before_raw = client._fetch_portfolio_envelope()  # noqa: SLF001
        _save_json(out, "portfolio_before.json", before_raw)
        before = _parse_positions(client, before_raw)
        _log(lines, f"saved raw pre-order portfolio ({len(before)} parsed position(s))")

        stage = "SCHG exact-symbol and eligibility checks"
        instrument = client.search_instrument(SYMBOL)
        if instrument.raw.get("internalSymbolFull") != SYMBOL:
            raise ValueError("instrument search did not return an exact SCHG symbol")
        instrument_id = instrument.instrument_id
        eligibility = client.check_eligibility_for_order(instrument_id, AMOUNT_USD)
        _save_json(out, "eligibility_raw.json", eligibility.raw)
        minimum_exposure = _finite_number(eligibility.min_position_exposure, positive=True)
        minimum_amount = _finite_number(eligibility.min_position_amount, positive=True)
        if not eligibility.allow_open_position or minimum_exposure is None or minimum_amount is None:
            raise ValueError("eligibility or a required minimum is unknown/invalid")
        if AMOUNT_USD < max(minimum_exposure, minimum_amount):
            raise ValueError("$10 is below SCHG's live eligibility minimum")
        _log(lines, f"SCHG exact match instrument_id={instrument_id}; $10 meets both live minimums")

        request_id = str(uuid.uuid4())
        # Persist the single-use marker before POST. A crash/timeout after this point
        # can only trigger lookup by referenceId on a later manual inspection, never a re-buy.
        with attempt_path.open("x", encoding="utf-8") as marker:
            json.dump(
                {
                    "request_id": request_id,
                    "symbol": SYMBOL,
                    "instrument_id": instrument_id,
                    "amount_usd": AMOUNT_USD,
                    "started_at_utc": datetime.now(timezone.utc).isoformat(),
                },
                marker,
            )
            marker.flush()
            os.fsync(marker.fileno())

        order_id = None
        submit_error = None
        client_logger = logging.getLogger("nightwatch.etoro.client")
        logger_was_disabled = client_logger.disabled
        client_logger.disabled = True
        try:
            try:
                order_id = client.place_buy_order(request_id, instrument_id, AMOUNT_USD)
            except Exception as exc:
                submit_error = type(exc).__name__
        finally:
            client_logger.disabled = logger_was_disabled
        submit_raw = client.last_write_response_body
        evidence_complete = submit_raw is not None
        try:
            _save_json(out, "order_submit_raw.json", submit_raw)
        except Exception:
            evidence_complete = False
            _log(lines, "could not save raw submit response; continuing read-only verification")
        if submit_error:
            _log(lines, f"submit raised {submit_error}; no retry will be made")

        latest = None
        lookups = 0
        lookup_by_reference = not bool(order_id)
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        while True:
            try:
                if lookup_by_reference:
                    latest = client.lookup_order(reference_id=request_id)
                else:
                    try:
                        latest = client.lookup_order(order_id=order_id)
                    except EtoroValidationError as exc:
                        if exc.status_code != 404:
                            raise
                        lookup_by_reference = True
                        _log(lines, "order ID lookup returned 404; continuing by request referenceId")
                        latest = client.lookup_order(reference_id=request_id)
            except Exception as exc:
                _log(lines, f"order lookup failed ({type(exc).__name__}); no retry of submit")
                break
            lookups += 1
            try:
                _save_json(out, f"order_lookup_{lookups:03d}.json", latest.raw)
            except Exception:
                evidence_complete = False
                _log(lines, f"could not save raw lookup {lookups}; continuing read-only polling")
            _log(lines, f"lookup {lookups}: status={latest.status.name} order_id={latest.order_id}")
            if latest.is_terminal or time.monotonic() >= deadline:
                break
            time.sleep(POLL_INTERVAL_SECONDS)

        stage = "post-order portfolio read"
        after_raw = client._fetch_portfolio_envelope()  # noqa: SLF001
        _save_json(out, "portfolio_after.json", after_raw)
        after = _parse_positions(client, after_raw)
        _log(lines, f"saved raw post-order portfolio ({len(after)} parsed position(s))")
        if latest is None:
            raise ValueError("no order status could be read; submission will not be repeated")
        position_id = require_filled_position_id(latest.status, latest.position_id)
        if any(position.position_id == position_id for position in before):
            raise ValueError("filled order returned a position ID already present before submit")

        matches = [position for position in after if position.position_id == position_id]
        if len(matches) != 1:
            raise ValueError("exact filled position ID was not present exactly once after submit")
        result = evaluate_position_readback(
            matches[0].raw,
            expected_position_id=position_id,
            expected_instrument_id=instrument_id,
        )
        expected_status_label = latest.status.name.title().replace("_", "")
        if latest.status_label not in (None, expected_status_label):
            raise ValueError("broker status label conflicts with FILLED status")
        _save_json(out, "take_profit_verdict.json", result)
        report_status = "VERIFIED" if evidence_complete else "INCOMPLETE_EVIDENCE"
        _save_json(out, "probe_report.json", {"status": report_status, **result})
        _log(
            lines,
            f"{report_status}: verdict={result['verdict']} position_id={position_id} "
            f"takeProfitRate/openRate={result['take_profit_rate_over_open_rate']} "
            f"isNoTakeProfit={result['is_no_take_profit']}; position left open",
        )
    except Exception as exc:
        _log(lines, f"REFUSED/INCOMPLETE at {stage}: {type(exc).__name__}; no further order writes")
        try:
            _save_json(out, "probe_report.json", {"status": "REFUSED_OR_INCOMPLETE", "reason_type": type(exc).__name__})
        except Exception:
            _log(lines, "could not save failure report")
    finally:
        if client is not None:
            client.close()
    _log(lines, f"finished; artifacts={out}; any filled position remains open")
    return "\n".join(lines)
