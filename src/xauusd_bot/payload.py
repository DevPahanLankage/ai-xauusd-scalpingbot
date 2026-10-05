from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .economic_calendar import EconomicNewsEvent, EconomicNewsGateResult
from .models import Candle, MarketGateResult, Tick, XAUUSDMarketSnapshot


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
    encoded = json.dumps(
        payload, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
