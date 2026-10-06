from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .advisory_service import AIAdvisoryOutcome, AIAdvisoryService
from .config import AutoAdvisoryConfig
from .economic_calendar import EconomicNewsGateResult
from .models import MarketGateResult, XAUUSDMarketSnapshot


@dataclass(frozen=True, slots=True)
class AutoAdvisoryState:
    enabled: bool
    state: str
    candidate_time: str | None
    last_call_time: str | None
    last_result: str | None
    reason: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class AutomaticAdvisoryCoordinator:
    """Single-flight automatic caller around the existing advisory service."""

    def __init__(
        self,
        config: AutoAdvisoryConfig,
        service: AIAdvisoryService,
    ) -> None:
        self.config = config
        self.service = service
        self._lock = asyncio.Lock()
        self._considered_candidate: str | None = None
        self._state = AutoAdvisoryState(
            config.enabled,
            "WAITING" if config.enabled else "OFF",
            None,
            None,
            None,
            None,
        )

    @property
    def state(self) -> AutoAdvisoryState:
        return self._state

    async def maybe_evaluate(
        self,
        snapshot: XAUUSDMarketSnapshot,
        market_gate: MarketGateResult,
        news_gate: EconomicNewsGateResult,
        *,
        on_analyzing: Callable[[], Awaitable[None]] | None = None,
    ) -> AIAdvisoryOutcome | None:
        if not self.config.enabled:
            return None
        candidate = market_gate.completed_m1_time
        if not market_gate.eligible_for_ai or not news_gate.safe_for_ai or not candidate:
            reason = (
                "market_gate_rejected" if not market_gate.eligible_for_ai
                else "news_gate_rejected" if not news_gate.safe_for_ai
                else "missing_completed_m1_candle"
            )
            self._state = AutoAdvisoryState(True, "BLOCKED", candidate, self._state.last_call_time, self._state.last_result, reason)
            return None
        async with self._lock:
            if candidate == self._considered_candidate:
                return None
            self._considered_candidate = candidate
            self._state = AutoAdvisoryState(True, "ANALYZING", candidate, self._state.last_call_time, self._state.last_result, None)
            if on_analyzing is not None:
                await on_analyzing()
            try:
                outcome = await self.service.evaluate(snapshot, market_gate, news_gate)
            except Exception as exc:
                self._state = AutoAdvisoryState(True, "ERROR", candidate, datetime.now(timezone.utc).isoformat(), "ERROR", f"advisory_error:{type(exc).__name__}")
                return None
            now = datetime.now(timezone.utc).isoformat()
            if outcome.result is not None:
                decision = outcome.result.decision.decision.value
                state = decision if outcome.result.status == "success" else "ERROR"
                reason = None if state != "ERROR" else outcome.result.status
            else:
                decision = "ERROR" if outcome.skip_reason and "failed" in outcome.skip_reason else None
                state = "ERROR" if decision else "BLOCKED"
                reason = outcome.skip_reason
            self._state = AutoAdvisoryState(True, state, candidate, now, decision, reason)
            return outcome
