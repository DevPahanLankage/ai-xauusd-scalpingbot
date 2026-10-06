from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .economic_calendar import EconomicNewsGateResult
from .models import MarketGateResult, XAUUSDMarketSnapshot
from .preview import AIPreviewResult


REASON_LABELS = {
    "stale_or_missing_quote": "XAUUSD quote is stale or unavailable",
    "stale_or_missing_tick": "Recent XAUUSD tick data is stale or unavailable",
    "market_inactive": "The XAUUSD market is not currently active",
    "insufficient_recent_tick_history": "Recent tick history is insufficient",
    "no_completed_m1_candle": "No completed M1 candidate candle is available",
    "insufficient_m1_history": "M1 candle history is insufficient",
    "insufficient_m5_history": "M5 candle history is insufficient",
    "abnormal_or_unknown_m1_volatility": "M1 volatility is abnormal or unavailable",
    "abnormal_or_unknown_m5_volatility": "M5 volatility is abnormal or unavailable",
    "spread_exceeds_absolute_limit": "Spread exceeds the configured absolute limit",
    "spread_too_large_relative_to_m1_volatility": "Spread is too large relative to recent M1 volatility",
    "m1_m5_directions_not_aligned": "M1 and M5 directions are not aligned",
    "existing_xauusd_position": "An XAUUSD position already exists",
    "insufficient_free_margin": "Free margin is below the configured requirement",
    "high_impact_usd_news_blackout": "High-impact USD news blackout is active",
    "market_gate_rejected": "MarketGate rejected the current candidate",
    "news_gate_rejected": "EconomicCalendarGate rejected the current candidate",
    "missing_openai_credentials": "OpenAI is not configured",
    "missing_completed_m1_candle": "No completed M1 candidate candle is available",
    "completed_m1_already_consumed": "This completed M1 candle was already evaluated",
    "daily_call_limit": "The daily OpenAI call limit has been reached",
    "daily_spend_limit": "The daily OpenAI budget cannot reserve another call",
    "weekly_spend_limit": "The weekly OpenAI budget cannot reserve another call",
    "invalid_sanitized_payload": "The sanitized AI payload failed local validation",
    "persistent_state_unavailable": "Persistent state is unavailable",
}


def human_reason(code: str) -> str:
    if code.startswith("calendar_unavailable") or code.startswith(
        "calendar_interpretation_error"
    ):
        return "Economic calendar data is unavailable or invalid"
    if code.startswith("market_gate_internal_error"):
        return "MarketGate failed closed due to an internal data error"
    return REASON_LABELS.get(code, code.replace("_", " ").capitalize())


def valid_reasons(gate: MarketGateResult, news: EconomicNewsGateResult) -> list[str]:
    reasons = [
        "Market data is fresh",
        "Spread is within configured limits",
        f"M1 and M5 are aligned {gate.direction_m1}",
        "Volatility is within configured limits",
        "No existing XAUUSD position",
        "Free margin requirement is satisfied",
        "High-impact USD news window is safe",
    ]
    if not gate.directions_aligned:
        reasons.remove(f"M1 and M5 are aligned {gate.direction_m1}")
    if not news.safe_for_ai:
        reasons.remove("High-impact USD news window is safe")
    return reasons


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    timestamp: str
    level: str
    message: str


class EventFeed:
    def __init__(self, limit: int = 100) -> None:
        self._events: deque[ApplicationEvent] = deque(maxlen=limit)
        self._last_by_key: dict[str, str] = {}

    def add(
        self,
        level: str,
        message: str,
        *,
        key: str | None = None,
        fingerprint: str | None = None,
    ) -> bool:
        identity = key or f"{level}:{message}"
        value = fingerprint or message
        if self._last_by_key.get(identity) == value:
            return False
        self._last_by_key[identity] = value
        self._events.append(
            ApplicationEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                level=level,
                message=message,
            )
        )
        return True

    def to_list(self) -> list[dict[str, str]]:
        return [asdict(item) for item in self._events]


class MeaningfulEventTracker:
    """Generate events only when meaningful state transitions occur."""

    def __init__(self, feed: EventFeed) -> None:
        self.feed = feed
        self._previous: dict[str, Any] = {}

    def update(self, state: dict[str, Any]) -> None:
        system = state["system"]
        validity = state["validity"]
        gate = state["market_gate"]
        news = state["news_gate"]
        candidate = system.get("candidate_time")
        if self._previous.get("connected") != system["mt5_connected"]:
            self.feed.add(
                "CONNECTED" if system["mt5_connected"] else "ERROR",
                "MT5 Terminal MCP connected"
                if system["mt5_connected"]
                else "MT5 connection lost",
                key="connection",
            )
        if candidate and self._previous.get("candidate") not in (None, candidate):
            self.feed.add(
                "INFO",
                "New completed M1 candle",
                key="candidate",
                fingerprint=candidate,
            )
        if self._previous.get("valid") != validity["eligible"]:
            self.feed.add(
                "VALID" if validity["eligible"] else "BLOCKED",
                "Setup passed deterministic gates"
                if validity["eligible"]
                else validity["reasons"][0]["message"] if validity["reasons"] else "Setup blocked",
                key="validity",
            )
        alignment = (gate["directions_aligned"], gate["direction_m1"], gate["direction_m5"])
        if self._previous.get("alignment") not in (None, alignment):
            message = (
                f"M1/M5 aligned {gate['direction_m1']}"
                if gate["directions_aligned"]
                else "M1/M5 directions no longer aligned"
            )
            self.feed.add("VALID" if gate["directions_aligned"] else "BLOCKED", message, key="alignment")
        if self._previous.get("news_safe") not in (None, news["safe_for_ai"]):
            self.feed.add(
                "NEWS",
                "High-impact USD news window safe"
                if news["safe_for_ai"]
                else "High-impact USD news window blocked",
                key="news",
            )
        advisory_stamp = (state["ai"].get("last_advisory") or {}).get("timestamp")
        if advisory_stamp and self._previous.get("advisory") not in (None, advisory_stamp):
            advisory = state["ai"]["last_advisory"]
            self.feed.add(
                "AI",
                f"{advisory.get('decision') or 'ERROR'}"
                + (
                    f" | {advisory['confidence']}% confidence"
                    if advisory.get("confidence") is not None
                    else ""
                ),
                key="advisory",
                fingerprint=advisory_stamp,
            )
            if advisory.get("estimated_cost_usd") is not None:
                latency = advisory.get("latency_ms")
                suffix = f" | {float(latency) / 1000:.2f} sec" if latency is not None else ""
                self.feed.add(
                    "COST",
                    f"${float(advisory['estimated_cost_usd']):.6f}{suffix}",
                    key="advisory_cost",
                    fingerprint=advisory_stamp,
                )
        self._previous = {
            "connected": system["mt5_connected"],
            "candidate": candidate,
            "valid": validity["eligible"],
            "alignment": alignment,
            "news_safe": news["safe_for_ai"],
            "advisory": advisory_stamp,
        }


def _candles(items: Any) -> list[dict[str, Any]]:
    return [
        {
            "time": item.time,
            "open": item.open,
            "high": item.high,
            "low": item.low,
            "close": item.close,
        }
        for item in items
    ]


def _last_advisory(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    result = value.get("result") or {}
    decision = result.get("decision") or {}
    usage = result.get("usage") or value.get("usage") or {}
    return {
        "timestamp": value.get("timestamp"),
        "candidate_time": value.get("candidate_time"),
        "input_hash": value.get("input_hash"),
        "model": value.get("model"),
        "status": result.get("status") or value.get("status"),
        "decision": decision.get("decision") or value.get("decision"),
        "confidence": decision.get("confidence"),
        "market_regime": decision.get("market_regime"),
        "setup_summary": decision.get("setup_summary"),
        "entry_price": decision.get("entry_price"),
        "entry_zone_low": decision.get("entry_zone_low"),
        "entry_zone_high": decision.get("entry_zone_high"),
        "stop_loss": decision.get("stop_loss"),
        "take_profit": decision.get("take_profit"),
        "risk_reward_ratio": decision.get("risk_reward_ratio"),
        "invalidation_reason": decision.get("invalidation_reason"),
        "warnings": decision.get("warnings") or [],
        "usage": usage,
        "estimated_cost_usd": result.get("estimated_cost_usd", value.get("estimated_cost_usd")),
        "latency_ms": result.get("latency_ms", value.get("latency_ms")),
    }


def build_application_state(
    *,
    snapshot: XAUUSDMarketSnapshot,
    market_gate: MarketGateResult,
    news_gate: EconomicNewsGateResult,
    preview: AIPreviewResult,
    settings: Settings,
    last_advisory: dict[str, Any] | None,
    events: EventFeed,
    started_at: datetime,
    mode: str,
) -> dict[str, Any]:
    raw_codes = list(market_gate.rejection_reasons) + list(news_gate.rejection_reasons)
    for code in preview.skip_reasons:
        if code not in {"market_gate_rejected", "news_gate_rejected"} and code not in raw_codes:
            raw_codes.append(code)
    reasons = [{"code": code, "message": human_reason(code)} for code in raw_codes]
    eligible = preview.would_request
    ai_state = (
        "NOT_CONFIGURED"
        if not preview.credentials_configured
        else "ELIGIBLE" if eligible else "BLOCKED"
    )
    usage = preview.usage.to_dict() if preview.usage else None
    budget = preview.budget.to_dict() if preview.budget else None
    nearest = news_gate.nearest_event.to_dict() if news_gate.nearest_event else None
    return {
        "schema_version": "1.0",
        "system": {
            "mode": mode,
            "advisory_only": True,
            "engine_status": "MONITORING",
            "mt5_connected": True,
            "selected_symbol": snapshot.symbol.symbol,
            "candidate_time": market_gate.completed_m1_time,
            "candidate_hash": preview.input_hash,
            "persistent_state_available": preview.state_available,
            "last_refresh": snapshot.captured_at_utc,
            "trade_server_time": snapshot.trade_server_time,
            "started_at": started_at.isoformat(),
            "uptime_seconds": max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds()),
        },
        "market": {
            "symbol": snapshot.symbol.symbol,
            "bid": snapshot.symbol.bid,
            "ask": snapshot.symbol.ask,
            "spread_price": snapshot.metrics.spread_price,
            "spread_points": snapshot.metrics.spread_points,
            "point": snapshot.symbol.point,
            "digits": snapshot.symbol.digits,
            "m1_latest_range_points": snapshot.metrics.m1_latest_range_points,
            "m1_average_range_points": snapshot.metrics.m1_average_range_points,
            "m5_latest_range_points": snapshot.metrics.m5_latest_range_points,
            "m5_average_range_points": snapshot.metrics.m5_average_range_points,
            "recent_tick_count": len(snapshot.recent_ticks),
        },
        "market_gate": market_gate.to_dict(),
        "validity": {
            "eligible": eligible,
            "status": "VALID" if eligible else "BLOCKED",
            "reasons": [] if eligible else reasons,
            "valid_reasons": valid_reasons(market_gate, news_gate) if eligible else [],
            "original_reason_codes": raw_codes,
        },
        "news_gate": {
            **news_gate.to_dict(),
            "nearest_event": nearest,
        },
        "ai": {
            "state": ai_state,
            "model": settings.ai.model,
            "reasoning_effort": settings.ai.reasoning_effort,
            "current_candidate_time": market_gate.completed_m1_time,
            "current_candidate_hash": preview.input_hash,
            "candidate_consumed": preview.candidate_consumed,
            "last_advisory": _last_advisory(last_advisory),
        },
        "usage": {"summary": usage, "budget": budget},
        "account": {
            "currency": snapshot.account.currency,
            "balance": snapshot.account.balance,
            "equity": snapshot.account.equity,
            "free_margin": snapshot.account.free_margin,
            "xauusd_position_exists": bool(snapshot.positions),
            "xauusd_position_count": len(snapshot.positions),
        },
        "chart": {
            "m1": _candles(snapshot.m1_candles),
            "m5": _candles(snapshot.m5_candles),
        },
        "events": events.to_list(),
    }


def disconnected_state(*, mode: str, started_at: datetime, message: str, events: EventFeed) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "system": {
            "mode": mode,
            "advisory_only": True,
            "engine_status": "RECONNECTING",
            "mt5_connected": False,
            "selected_symbol": None,
            "candidate_time": None,
            "candidate_hash": None,
            "persistent_state_available": None,
            "last_refresh": datetime.now(timezone.utc).isoformat(),
            "trade_server_time": None,
            "started_at": started_at.isoformat(),
            "uptime_seconds": max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds()),
        },
        "market": None,
        "market_gate": None,
        "validity": {
            "eligible": False,
            "status": "BLOCKED",
            "reasons": [{"code": "mt5_disconnected", "message": message}],
            "valid_reasons": [],
            "original_reason_codes": ["mt5_disconnected"],
        },
        "news_gate": None,
        "ai": {"state": "BLOCKED", "last_advisory": None},
        "usage": None,
        "account": None,
        "chart": {"m1": [], "m5": []},
        "events": events.to_list(),
    }
