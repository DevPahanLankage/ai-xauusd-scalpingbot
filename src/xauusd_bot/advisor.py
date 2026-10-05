from __future__ import annotations

import json
import math
import time
from typing import Any

from .ai_models import (
    AITradeDecision,
    AdvisoryResult,
    TokenUsage,
    TradeDecision,
    safe_no_trade,
)
from .config import AIConfig


SYSTEM_INSTRUCTIONS = """You are a conservative XAUUSD scalping analyst.
Return only the requested structured recommendation. BUY and SELL are advisory labels,
never executable commands. Prefer NO_TRADE whenever evidence is mixed, weak, stale, or
insufficient. Passing deterministic safety gates does not require a trade. Analyze only
the supplied market and economic-event data. Do not invent indicators, news, prices, or
future certainty. Consider spread, recent volatility, short-term structure, M1/M5
context, tick behavior, and the supplied news context. Keep rationale fields concise.
For BUY, place stop loss below entry and take profit above entry. For SELL, place stop
loss above entry and take profit below entry. Use null price fields for NO_TRADE."""


def estimate_cost(usage: TokenUsage, config: AIConfig) -> float:
    total_input = max(usage.input_tokens, 0)
    # If malformed details exceed the total, allocate the finite total to the
    # higher-priced cache-write category first so the estimate stays conservative.
    cache_write = min(
        max(usage.cache_write_tokens, 0),
        total_input,
    )
    cached = min(
        max(usage.cached_input_tokens, 0),
        max(total_input - cache_write, 0),
    )
    ordinary = max(total_input - cached - cache_write, 0)
    return round(
        (
            ordinary * config.input_usd_per_million
            + cached * config.cached_input_usd_per_million
            + cache_write * config.cache_write_usd_per_million
            + max(usage.output_tokens, 0) * config.output_usd_per_million
        )
        / 1_000_000,
        8,
    )


def _integer_attr(value: Any, name: str) -> int:
    item = getattr(value, name, 0) if value is not None else 0
    return int(item or 0)


def extract_usage(response: Any) -> TokenUsage | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return TokenUsage(
        input_tokens=_integer_attr(usage, "input_tokens"),
        cached_input_tokens=_integer_attr(input_details, "cached_tokens"),
        cache_write_tokens=_integer_attr(input_details, "cache_write_tokens"),
        output_tokens=_integer_attr(usage, "output_tokens"),
        reasoning_tokens=_integer_attr(output_details, "reasoning_tokens"),
    )


def _entry(decision: AITradeDecision) -> float | None:
    has_price = decision.entry_price is not None
    has_low = decision.entry_zone_low is not None
    has_high = decision.entry_zone_high is not None
    if has_low != has_high:
        return None
    has_zone = has_low and has_high
    if has_price == has_zone:
        return None
    if has_price:
        return decision.entry_price
    assert decision.entry_zone_low is not None
    assert decision.entry_zone_high is not None
    if decision.entry_zone_low > decision.entry_zone_high:
        return None
    return (decision.entry_zone_low + decision.entry_zone_high) / 2.0


def _market_sanity_limit(payload: dict[str, Any], config: AIConfig) -> tuple[float, float] | None:
    try:
        instrument = payload["instrument"]
        context = payload["market_context"]
        bid = float(instrument["bid"])
        ask = float(instrument["ask"])
        point = float(instrument["point"])
        m1_range = float(context["m1_average_range_points"]) * point
        m5_range = float(context["m5_average_range_points"]) * point
    except (KeyError, TypeError, ValueError):
        return None
    values = (bid, ask, point, m1_range, m5_range)
    if any(not math.isfinite(value) or value <= 0 for value in values) or ask <= bid:
        return None
    reference_range = max(m1_range, m5_range)
    return (bid + ask) / 2.0, reference_range * config.max_price_distance_volatility_multiple


def validate_decision(
    decision: AITradeDecision,
    payload: dict[str, Any],
    config: AIConfig,
) -> AITradeDecision:
    numeric = (
        decision.entry_price,
        decision.entry_zone_low,
        decision.entry_zone_high,
        decision.stop_loss,
        decision.take_profit,
        decision.risk_reward_ratio,
    )
    if any(value is not None and (not math.isfinite(value) or value <= 0) for value in numeric):
        return safe_no_trade("invalid_non_finite_or_non_positive_price")
    if decision.decision is TradeDecision.NO_TRADE:
        return decision.model_copy(
            update={
                "entry_price": None,
                "entry_zone_low": None,
                "entry_zone_high": None,
                "stop_loss": None,
                "take_profit": None,
                "risk_reward_ratio": None,
            }
        )

    entry = _entry(decision)
    stop = decision.stop_loss
    target = decision.take_profit
    reported_ratio = decision.risk_reward_ratio
    if entry is None or stop is None or target is None or reported_ratio is None:
        return safe_no_trade("missing_required_trade_geometry")
    if decision.decision is TradeDecision.BUY:
        geometry_ok = stop < entry < target
    else:
        geometry_ok = target < entry < stop
    if not geometry_ok:
        return safe_no_trade(f"invalid_{decision.decision.value.lower()}_geometry")

    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0 or reward <= 0:
        return safe_no_trade("zero_or_negative_trade_risk")
    calculated = reward / risk
    tolerance = max(0.05, calculated * 0.05)
    if not math.isfinite(calculated) or abs(reported_ratio - calculated) > tolerance:
        return safe_no_trade("invalid_risk_reward_calculation")
    sanity = _market_sanity_limit(payload, config)
    if sanity is None:
        return safe_no_trade("missing_or_invalid_market_sanity_context")
    current_price, maximum_distance = sanity
    proposed_prices = [
        value
        for value in (
            decision.entry_price,
            decision.entry_zone_low,
            decision.entry_zone_high,
            stop,
            target,
        )
        if value is not None
    ]
    if any(abs(value - current_price) > maximum_distance for value in proposed_prices):
        return safe_no_trade("recommendation_detached_from_current_market")
    return decision.model_copy(update={"risk_reward_ratio": round(calculated, 4)})


class OpenAIAdvisor:
    """Responses API adapter. It receives sanitized data and never receives tools."""

    def __init__(self, config: AIConfig, client: Any | None = None) -> None:
        self._config = config
        if client is None:
            if not config.api_key:
                raise ValueError("OPENAI_API_KEY is required for AI advisory mode")
            from openai import OpenAI

            client = OpenAI(api_key=config.api_key, timeout=config.timeout_seconds)
        self._client = client

    def evaluate(self, payload: dict[str, Any]) -> AdvisoryResult:
        started = time.perf_counter()
        try:
            response = self._client.responses.parse(
                model=self._config.model,
                reasoning={"effort": self._config.reasoning_effort},
                input=[
                    {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                    {
                        "role": "user",
                        "content": json.dumps(
                            payload,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                ],
                text_format=AITradeDecision,
                max_output_tokens=self._config.max_output_tokens,
                store=False,
            )
            usage = extract_usage(response)
            parsed = getattr(response, "output_parsed", None)
            if not isinstance(parsed, AITradeDecision):
                status = "refusal_or_unparsed"
                decision = safe_no_trade("model_refused_or_returned_no_parsed_output")
            else:
                decision = validate_decision(parsed, payload, self._config)
                status = (
                    "success"
                    if decision.decision == parsed.decision
                    else "invalid_recommendation"
                )
            cost = estimate_cost(usage, self._config) if usage is not None else None
            return AdvisoryResult(
                decision=decision,
                status=status,
                usage=usage,
                estimated_cost_usd=cost,
                latency_ms=round((time.perf_counter() - started) * 1_000, 3),
            )
        except Exception as exc:
            kind = type(exc).__name__.lower()
            status = "timeout" if "timeout" in kind else "api_error"
            return AdvisoryResult(
                decision=safe_no_trade(f"openai_{status}"),
                status=status,
                usage=None,
                estimated_cost_usd=None,
                latency_ms=round((time.perf_counter() - started) * 1_000, 3),
            )
