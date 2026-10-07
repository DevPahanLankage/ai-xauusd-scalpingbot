from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .economic_calendar import EconomicNewsEvent, EconomicNewsGateResult
from .models import Candle, MarketGateResult, Tick, XAUUSDMarketSnapshot
from .timestamps import TimestampError, canonical_timestamp, parse_timestamp


class PayloadValidationError(ValueError):
    """Raised before reservation when a sanitized AI payload is unsafe."""


FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "account",
        "account_login",
        "login",
        "owner",
        "broker",
        "balance",
        "equity",
        "free_margin",
        "used_margin",
        "margin",
        "api_key",
        "openai_api_key",
        "mcp_token",
        "authorization",
        "authorization_header",
        "credential",
        "credentials",
        "secret",
        "secrets",
    }
)


def _candle(item: Candle) -> dict[str, Any]:
    return {
        "time": item.time,
        "open": item.open,
        "high": item.high,
        "low": item.low,
        "close": item.close,
        "tick_volume": item.tick_volume,
    }


def _event(item: EconomicNewsEvent) -> dict[str, Any]:
    return {
        "name": item.name,
        "currency": item.currency,
        "importance": item.importance,
        "scheduled_time": item.scheduled_time,
        "minutes_to_event": item.minutes_to_event,
        "actual": item.actual,
        "forecast": item.forecast,
        "previous": item.previous,
        "revised_previous": item.revised_previous,
    }


def _tick_behavior(ticks: tuple[Tick, ...], point: float) -> dict[str, Any]:
    if not ticks or point <= 0:
        return {"count": len(ticks), "available": False}
    bids = [item.bid for item in ticks if math.isfinite(item.bid) and item.bid > 0]
    if not bids:
        return {"count": len(ticks), "available": False}
    changes = [right - left for left, right in zip(bids, bids[1:])]
    return {
        "count": len(ticks),
        "available": True,
        "latest_time": ticks[-1].time,
        "latest_bid": ticks[-1].bid,
        "latest_ask": ticks[-1].ask,
        "bid_change_points": round((bids[-1] - bids[0]) / point, 3),
        "bid_range_points": round((max(bids) - min(bids)) / point, 3),
        "upticks": sum(change > 0 for change in changes),
        "downticks": sum(change < 0 for change in changes),
        "unchanged_ticks": sum(change == 0 for change in changes),
    }


def build_ai_payload(
    snapshot: XAUUSDMarketSnapshot,
    market_gate: MarketGateResult,
    news_gate: EconomicNewsGateResult,
    *,
    m1_limit: int,
    m5_limit: int,
) -> dict[str, Any]:
    """Build a compact market-only payload with no account identity or funds."""

    metrics = market_gate.metrics
    payload = {
        "schema_version": "1.0",
        "instrument": {
            "symbol": snapshot.symbol.symbol,
            "trade_server_time": snapshot.trade_server_time,
            "bid": snapshot.symbol.bid,
            "ask": snapshot.symbol.ask,
            "spread_price": snapshot.metrics.spread_price,
            "spread_points": snapshot.metrics.spread_points,
            "point": snapshot.symbol.point,
            "digits": snapshot.symbol.digits,
        },
        "completed_m1_candles": [
            _candle(item) for item in snapshot.m1_candles[-m1_limit:]
        ],
        "completed_m5_candles": [
            _candle(item) for item in snapshot.m5_candles[-m5_limit:]
        ],
        "market_context": {
            "m1_latest_range_points": snapshot.metrics.m1_latest_range_points,
            "m5_latest_range_points": snapshot.metrics.m5_latest_range_points,
            "m1_average_range_points": snapshot.metrics.m1_average_range_points,
            "m5_average_range_points": snapshot.metrics.m5_average_range_points,
            "direction_m1": market_gate.direction_m1,
            "direction_m5": market_gate.direction_m5,
            "directions_aligned": market_gate.directions_aligned,
            "m1_spike_ratio": metrics.m1_spike_ratio,
            "m5_spike_ratio": metrics.m5_spike_ratio,
            "spread_to_m1_range_ratio": metrics.spread_to_m1_range_ratio,
            "quote_age_seconds": metrics.quote_age_seconds,
            "tick_age_seconds": metrics.tick_age_seconds,
        },
        "recent_tick_behavior": _tick_behavior(
            snapshot.recent_ticks, snapshot.symbol.point
        ),
        "economic_news": {
            "minutes_to_nearest_high_impact_usd_event": (
                news_gate.minutes_to_nearest_event
            ),
            "upcoming_high_impact_usd_events": [
                _event(item) for item in news_gate.upcoming_high_impact_events
            ],
            "recent_high_impact_usd_events": [
                _event(item) for item in news_gate.recent_high_impact_events
            ],
        },
    }
    # This also rejects NaN/Infinity before a request can be attempted.
    json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return payload


def payload_hash(payload: dict[str, Any]) -> str:
    encoded = serialize_ai_payload(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def serialize_ai_payload(payload: dict[str, Any]) -> str:
    """Return the exact canonical JSON used as the advisory user input."""

    _validate_tree(payload)
    return json.dumps(
        payload, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _validate_tree(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in FORBIDDEN_PAYLOAD_KEYS:
                raise PayloadValidationError(f"forbidden payload field: {path}.{key}")
            _validate_tree(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_tree(item, f"{path}[{index}]")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise PayloadValidationError(f"non-finite numeric value: {path}")


def _validate_candle_order(candles: Any, label: str, required: int) -> None:
    if not isinstance(candles, list) or len(candles) < required:
        raise PayloadValidationError(f"insufficient {label} payload history")
    try:
        times = [parse_timestamp(str(item["time"])) for item in candles]
    except (KeyError, TypeError, TimestampError) as exc:
        raise PayloadValidationError(f"invalid {label} candle timestamp") from exc
    try:
        unordered = any(right <= left for left, right in zip(times, times[1:]))
    except TypeError as exc:
        raise PayloadValidationError(
            f"{label} candle timestamps use incompatible timezone forms"
        ) from exc
    if unordered:
        raise PayloadValidationError(f"{label} candles are not strictly chronological")


def validate_ai_payload(
    payload: dict[str, Any],
    snapshot: XAUUSDMarketSnapshot,
    market_gate: MarketGateResult,
    news_gate: EconomicNewsGateResult,
    *,
    m1_required: int,
    m5_required: int,
) -> None:
    """Validate the exact outbound payload before any persistent reservation."""

    if not market_gate.eligible_for_ai:
        raise PayloadValidationError("MarketGate rejected the candidate")
    if not news_gate.safe_for_ai:
        raise PayloadValidationError("NewsGate rejected the candidate")
    if not market_gate.completed_m1_time:
        raise PayloadValidationError("candidate has no completed M1 identity")

    instrument = payload.get("instrument")
    if not isinstance(instrument, dict):
        raise PayloadValidationError("payload instrument is missing")
    payload_symbol = str(instrument.get("symbol", ""))
    snapshot_symbol = snapshot.symbol.symbol
    description = snapshot.symbol.description.upper()
    if (
        not snapshot.symbol.selected
        or payload_symbol != snapshot_symbol
        or ("XAUUSD" not in snapshot_symbol.upper() and "GOLD" not in description)
    ):
        raise PayloadValidationError("payload symbol is not the selected broker gold symbol")
    try:
        bid = float(instrument["bid"])
        ask = float(instrument["ask"])
        point = float(instrument["point"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PayloadValidationError("payload quote is malformed") from exc
    if any(not math.isfinite(value) for value in (bid, ask, point)):
        raise PayloadValidationError("payload quote contains a non-finite value")
    if bid <= 0 or ask <= bid or point <= 0:
        raise PayloadValidationError("payload bid/ask/point relationship is invalid")

    m1 = payload.get("completed_m1_candles")
    m5 = payload.get("completed_m5_candles")
    _validate_candle_order(m1, "M1", m1_required)
    _validate_candle_order(m5, "M5", m5_required)
    try:
        payload_candidate = canonical_timestamp(str(m1[-1]["time"]))
        gate_candidate = canonical_timestamp(market_gate.completed_m1_time)
    except (KeyError, TypeError, TimestampError) as exc:
        raise PayloadValidationError("candidate M1 identity is invalid") from exc
    if payload_candidate != gate_candidate:
        raise PayloadValidationError("latest payload M1 does not match the candidate")

    _validate_tree(payload)
    try:
        json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PayloadValidationError("payload is not strict finite JSON") from exc


def build_validated_ai_payload(
    snapshot: XAUUSDMarketSnapshot,
    market_gate: MarketGateResult,
    news_gate: EconomicNewsGateResult,
    *,
    m1_limit: int,
    m5_limit: int,
) -> dict[str, Any]:
    payload = build_ai_payload(
        snapshot,
        market_gate,
        news_gate,
        m1_limit=m1_limit,
        m5_limit=m5_limit,
    )
    validate_ai_payload(
        payload,
        snapshot,
        market_gate,
        news_gate,
        m1_required=m1_limit,
        m5_required=m5_limit,
    )
    return payload
