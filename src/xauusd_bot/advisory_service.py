from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .advisor import OpenAIAdvisor
from .ai_models import AdvisoryResult, safe_no_trade
from .config import AIConfig, PaperConfig
from .economic_calendar import EconomicNewsGateResult
from .models import MarketGateResult, XAUUSDMarketSnapshot
from .payload import build_validated_ai_payload, payload_hash
from .paper import PaperPerformanceTracker
from .state_store import AttemptReservation, SQLiteStateStore
from .timestamps import parse_timestamp


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AIAdvisoryOutcome:
    attempted: bool
    skip_reason: str | None
    input_hash: str | None
    reservation: AttemptReservation | None
    result: AdvisoryResult | None
    paper_id: int | None = None


class AIAdvisoryService:
    """Enforces deterministic gates, persistence, and budgets before OpenAI."""

    def __init__(
        self,
        config: AIConfig,
        *,
        state_store: SQLiteStateStore | None = None,
        advisor: Any | None = None,
        paper_config: PaperConfig | None = None,
    ) -> None:
        self._config = config
        self._state_store = state_store
        self._advisor = advisor
        self._paper_config = paper_config or PaperConfig()

    async def evaluate(
        self,
        snapshot: XAUUSDMarketSnapshot,
        market_gate: MarketGateResult,
        news_gate: EconomicNewsGateResult,
    ) -> AIAdvisoryOutcome:
        if not market_gate.eligible_for_ai:
            return self._skipped("market_gate_rejected")
        if not news_gate.safe_for_ai:
            return self._skipped("news_gate_rejected")
        if not self._config.api_key and self._advisor is None:
            return self._skipped("missing_openai_api_key")
        if not market_gate.completed_m1_time:
            return self._skipped("missing_completed_m1_candle")

        try:
            payload = build_validated_ai_payload(
                snapshot,
                market_gate,
                news_gate,
                m1_limit=self._config.payload_m1_candles,
                m5_limit=self._config.payload_m5_candles,
            )
            input_hash = payload_hash(payload)
        except Exception as exc:
            LOGGER.warning("AI payload rejected locally (%s)", type(exc).__name__)
            return self._skipped("invalid_sanitized_payload")

        try:
            store = self._state_store or SQLiteStateStore(
                self._config.state_db_path,
                legacy_reserve_usd=self._config.budget_reserve_per_call_usd,
            )
            advisor = self._advisor or OpenAIAdvisor(self._config)
        except Exception as exc:
            LOGGER.warning("AI advisory initialization failed closed (%s)", type(exc).__name__)
            return self._skipped("ai_advisory_initialization_failed")
        reservation = store.begin_ai_attempt(
            symbol=snapshot.symbol.symbol,
            completed_m1_time=market_gate.completed_m1_time,
            input_hash=input_hash,
            config=self._config,
        )
        if not reservation.reserved:
            return AIAdvisoryOutcome(
                attempted=False,
                skip_reason=reservation.reason,
                input_hash=input_hash,
                reservation=reservation,
                result=None,
            )

        call_started = time.monotonic()
        try:
            result = await asyncio.to_thread(advisor.evaluate, payload)
            if not isinstance(result, AdvisoryResult):
                raise TypeError("Advisor returned an unexpected result type")
        except Exception as exc:
            result = AdvisoryResult(
                decision=safe_no_trade("openai_api_error"),
                status="api_error",
                usage=None,
                estimated_cost_usd=None,
                latency_ms=0.0,
            )
            LOGGER.warning("AI advisor failed closed (%s)", type(exc).__name__)
        call_elapsed = max(0.0, time.monotonic() - call_started)
        store.finish_ai_attempt(reservation, result)
        paper_id = None
        if result.status == "success":
            try:
                paper_id = PaperPerformanceTracker(store, self._paper_config).register(
                    reservation=reservation,
                    result=result,
                    snapshot=snapshot,
                    market_gate=market_gate,
                    news_gate=news_gate,
                    tracking_start_market_time=(
                        parse_timestamp(snapshot.trade_server_time)
                        + timedelta(seconds=call_elapsed)
                    ).isoformat(),
                ).paper_id
            except Exception as exc:
                LOGGER.warning("Paper advisory registration failed (%s)", type(exc).__name__)
        decision = result.decision
        LOGGER.info(
            "AI advisory candidate=%s input_hash=%s model=%s effort=%s "
            "decision=%s confidence=%d entry=%s sl=%s tp=%s rr=%s "
            "input_tokens=%s cached_tokens=%s cache_write_tokens=%s "
            "output_tokens=%s reasoning_tokens=%s "
            "cost_usd=%s latency_ms=%.3f status=%s",
            reservation.completed_m1_time,
            input_hash,
            self._config.model,
            self._config.reasoning_effort,
            decision.decision.value,
            decision.confidence,
            decision.entry_price,
            decision.stop_loss,
            decision.take_profit,
            decision.risk_reward_ratio,
            result.usage.input_tokens if result.usage else None,
            result.usage.cached_input_tokens if result.usage else None,
            result.usage.cache_write_tokens if result.usage else None,
            result.usage.output_tokens if result.usage else None,
            result.usage.reasoning_tokens if result.usage else None,
            result.estimated_cost_usd,
            result.latency_ms,
            result.status,
        )
        return AIAdvisoryOutcome(
            attempted=True,
            skip_reason=None,
            input_hash=input_hash,
            reservation=reservation,
            result=result,
            paper_id=paper_id,
        )

    @staticmethod
    def _skipped(reason: str) -> AIAdvisoryOutcome:
        return AIAdvisoryOutcome(
            attempted=False,
            skip_reason=reason,
            input_hash=None,
            reservation=None,
            result=None,
            paper_id=None,
        )
