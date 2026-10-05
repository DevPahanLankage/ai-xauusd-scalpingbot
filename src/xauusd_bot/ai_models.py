from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TradeDecision(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    NO_TRADE = "NO_TRADE"


class AITradeDecision(BaseModel):
    """Strict Structured Outputs schema returned by the advisory model."""

    model_config = ConfigDict(extra="forbid")

    decision: TradeDecision
    confidence: int = Field(ge=0, le=100)
    market_regime: str
    setup_summary: str
    entry_price: float | None
    entry_zone_low: float | None
    entry_zone_high: float | None
    stop_loss: float | None
    take_profit: float | None
    risk_reward_ratio: float | None
    invalidation_reason: str
    warnings: list[str]


def safe_no_trade(reason: str, *, warning: str | None = None) -> AITradeDecision:
    warnings = [warning] if warning else []
    return AITradeDecision(
        decision=TradeDecision.NO_TRADE,
        confidence=0,
        market_regime="UNKNOWN",
        setup_summary=reason,
        entry_price=None,
        entry_zone_low=None,
        entry_zone_high=None,
        stop_loss=None,
        take_profit=None,
        risk_reward_ratio=None,
        invalidation_reason=reason,
        warnings=warnings,
    )


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AdvisoryResult:
    decision: AITradeDecision
    status: str
    usage: TokenUsage | None
    estimated_cost_usd: float | None
    latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.model_dump(mode="json"),
            "status": self.status,
            "usage": self.usage.to_dict() if self.usage else None,
            "estimated_cost_usd": self.estimated_cost_usd,
            "latency_ms": self.latency_ms,
        }
