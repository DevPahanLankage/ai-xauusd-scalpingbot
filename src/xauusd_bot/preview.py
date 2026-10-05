from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .config import AIConfig
from .economic_calendar import EconomicNewsGateResult
from .models import MarketGateResult, XAUUSDMarketSnapshot
from .payload import build_validated_ai_payload, payload_hash
from .state_store import (
    BudgetStatus,
    SQLiteStateStore,
    UsageSummary,
    evaluate_budget,
)


@dataclass(frozen=True, slots=True)
class AIPreviewResult:
    """Read-only description of whether the exact candidate would be requested."""

    symbol: str
    completed_m1_time: str | None
    market_gate_eligible: bool
    market_gate_reasons: tuple[str, ...]
    news_gate_safe: bool
    news_gate_reasons: tuple[str, ...]
    model: str
    reasoning_effort: str
    credentials_configured: bool
    usage: UsageSummary
    budget: BudgetStatus
    candidate_consumed: bool
    would_request: bool
    skip_reasons: tuple[str, ...]
    input_hash: str | None
    sanitized_payload: dict[str, Any] | None

    def to_dict(self, *, include_payload: bool) -> dict[str, Any]:
        result = asdict(self)
        if not include_payload:
            result.pop("sanitized_payload", None)
        return result


class AIPreviewService:
    """Build the first-call preview without initializing OpenAI or writing state."""

    def __init__(self, config: AIConfig) -> None:
        self._config = config

    def evaluate(
        self,
        snapshot: XAUUSDMarketSnapshot,
        market_gate: MarketGateResult,
        news_gate: EconomicNewsGateResult,
    ) -> AIPreviewResult:
        usage = SQLiteStateStore.usage_summary_read_only(self._config.state_db_path)
        budget = evaluate_budget(usage, self._config)
        candidate = market_gate.completed_m1_time
        consumed = bool(
            candidate
            and SQLiteStateStore.candidate_consumed_read_only(
                self._config.state_db_path, snapshot.symbol.symbol, candidate
            )
        )
        reasons: list[str] = []
        payload: dict[str, Any] | None = None
        digest: str | None = None
        if not market_gate.eligible_for_ai:
            reasons.append("market_gate_rejected")
        if not news_gate.safe_for_ai:
            reasons.append("news_gate_rejected")
        if not self._config.api_key:
            reasons.append("missing_openai_credentials")
        if not candidate:
            reasons.append("missing_completed_m1_candle")
        if consumed:
            reasons.append("completed_m1_already_consumed")
        reasons.extend(budget.rejection_reasons)

        if market_gate.eligible_for_ai and news_gate.safe_for_ai and candidate:
            try:
                payload = build_validated_ai_payload(
                    snapshot,
                    market_gate,
                    news_gate,
                    m1_limit=self._config.payload_m1_candles,
                    m5_limit=self._config.payload_m5_candles,
                )
                digest = payload_hash(payload)
            except Exception:
                reasons.append("invalid_sanitized_payload")

        return AIPreviewResult(
            symbol=snapshot.symbol.symbol,
            completed_m1_time=candidate,
            market_gate_eligible=market_gate.eligible_for_ai,
            market_gate_reasons=market_gate.rejection_reasons,
            news_gate_safe=news_gate.safe_for_ai,
            news_gate_reasons=news_gate.rejection_reasons,
            model=self._config.model,
            reasoning_effort=self._config.reasoning_effort,
            credentials_configured=bool(self._config.api_key),
            usage=usage,
            budget=budget,
            candidate_consumed=consumed,
            would_request=payload is not None and not reasons,
            skip_reasons=tuple(dict.fromkeys(reasons)),
            input_hash=digest,
            sanitized_payload=payload,
        )
