from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from typing import Awaitable, Callable

from .advisory_service import AIAdvisoryOutcome, AIAdvisoryService
from .config import AutoAdvisoryConfig
from .economic_calendar import EconomicNewsGateResult
from .models import MarketGateResult, XAUUSDMarketSnapshot
from .state_store import SQLiteStateStore


FINAL_DISPOSITIONS = frozenset(
    {"RATE_SPACING_BLOCKED", "DUPLICATE", "BUDGET_BLOCKED", "STATE_BLOCKED", "SENT_TO_AI", "AI_ERROR"}
)


@dataclass(frozen=True, slots=True)
class AutoAdvisoryState:
    enabled: bool
    state: str
    candidate_time: str | None
    last_call_time: str | None
    last_result: str | None
    reason: str | None
    min_interval_minutes: float
    seconds_since_last_call: float | None = None
    next_eligible_time: str | None = None
    seconds_until_eligible: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class AutomaticAdvisoryCoordinator:
    """Research recorder and single-flight automatic advisory coordinator."""

    def __init__(
        self,
        config: AutoAdvisoryConfig,
        service: AIAdvisoryService,
        *,
        state_store: SQLiteStateStore | None = None,
    ) -> None:
        self.config = config
        self.service = service
        self.state_store = state_store
        self._lock = asyncio.Lock()
        self._considered_candidate: str | None = None
        self._state = AutoAdvisoryState(
            config.enabled, "WAITING" if config.enabled else "OFF", None,
            None, None, None, config.min_interval_minutes,
        )
        self._refresh_spacing()

    @property
    def state(self) -> AutoAdvisoryState:
        return self._state

    def _refresh_spacing(self) -> None:
        if self.state_store is None:
            return
        try:
            spacing = self.state_store.auto_spacing_status(self.config.min_interval_minutes)
        except Exception:
            return
        self._state = replace(
            self._state,
            last_call_time=spacing.last_call_time,
            seconds_since_last_call=spacing.seconds_since_last_call,
            next_eligible_time=spacing.next_eligible_time,
            seconds_until_eligible=spacing.seconds_until_eligible,
        )

    def _persist(
        self,
        snapshot: XAUUSDMarketSnapshot,
        gate: MarketGateResult,
        news: EconomicNewsGateResult,
        *,
        candidate_hash: str | None,
        disposition: str,
        reason: str | None = None,
        usage_id: int | None = None,
        decision: str | None = None,
    ) -> dict[str, object] | None:
        if self.state_store is None or not gate.completed_m1_time:
            return None
        created = self.state_store.record_research_candidate(
            snapshot, gate, news, candidate_hash=candidate_hash,
            disposition=disposition, reason=reason,
        )
        self.state_store.record_research_observation(
            snapshot,
            gate,
            news,
            candidate_hash=candidate_hash,
            disposition=disposition,
            reason=reason,
        )
        row = self.state_store.research_candidate(snapshot.symbol.symbol, gate.completed_m1_time)
        if not created and row is not None:
            # A terminal candidate row is historical fact. Later quote refreshes are
            # retained in research_candidate_observations, never in this lifecycle row.
            if row.get("terminal_disposition") or row.get("advisory_usage_id") is not None:
                return row
            changed = (
                row.get("disposition") != disposition
                or bool(row.get("market_gate_eligible")) != gate.eligible_for_ai
                or bool(row.get("news_safe")) != news.safe_for_ai
                or (candidate_hash is not None and row.get("candidate_hash") != candidate_hash)
                or (usage_id is not None and row.get("advisory_usage_id") != usage_id)
                or (decision is not None and row.get("decision") != decision)
            )
            if changed:
                self.state_store.update_research_candidate(
                    snapshot, gate, news, candidate_hash=candidate_hash,
                    disposition=disposition, reason=reason,
                    advisory_usage_id=usage_id, decision=decision,
                )
                row = self.state_store.research_candidate(snapshot.symbol.symbol, gate.completed_m1_time)
        return row

    async def maybe_evaluate(
        self,
        snapshot: XAUUSDMarketSnapshot,
        market_gate: MarketGateResult,
        news_gate: EconomicNewsGateResult,
        *,
        candidate_hash: str | None = None,
        on_analyzing: Callable[[], Awaitable[None]] | None = None,
    ) -> AIAdvisoryOutcome | None:
        candidate = market_gate.completed_m1_time
        self._refresh_spacing()
        if not candidate:
            self._state = replace(
                self._state, state="BLOCKED" if self.config.enabled else "OFF",
                candidate_time=None, reason="missing_completed_m1_candle",
            )
            return None
        if not market_gate.eligible_for_ai:
            self._persist(
                snapshot, market_gate, news_gate, candidate_hash=candidate_hash,
                disposition="NOT_ELIGIBLE", reason="market_gate_rejected",
            )
            self._state = replace(
                self._state, state="BLOCKED" if self.config.enabled else "OFF",
                candidate_time=candidate, reason="market_gate_rejected",
            )
            return None
        if not news_gate.safe_for_ai:
            self._persist(
                snapshot, market_gate, news_gate, candidate_hash=candidate_hash,
                disposition="NEWS_BLOCKED", reason="news_gate_rejected",
            )
            self._state = replace(
                self._state, state="BLOCKED" if self.config.enabled else "OFF",
                candidate_time=candidate, reason="news_gate_rejected",
            )
            return None
        if not self.config.enabled:
            self._persist(
                snapshot, market_gate, news_gate, candidate_hash=candidate_hash,
                disposition="AUTO_DISABLED", reason="automatic_advisory_disabled",
            )
            self._state = replace(
                self._state, state="OFF", candidate_time=candidate,
                reason="automatic_advisory_disabled",
            )
            return None

        async with self._lock:
            if candidate == self._considered_candidate:
                self._refresh_spacing()
                return None
            existing = (
                self.state_store.research_candidate(snapshot.symbol.symbol, candidate)
                if self.state_store is not None else None
            )
            if existing and existing.get("disposition") in FINAL_DISPOSITIONS:
                self._considered_candidate = candidate
                return None

            self._considered_candidate = candidate
            self._persist(
                snapshot, market_gate, news_gate, candidate_hash=candidate_hash,
                disposition="STATE_BLOCKED", reason="advisory_pending",
            )
            self._state = replace(
                self._state, state="ANALYZING", candidate_time=candidate, reason=None,
            )
            if on_analyzing is not None:
                await on_analyzing()
            try:
                outcome = await self.service.evaluate(
                    snapshot, market_gate, news_gate, automatic=True,
                    min_interval_minutes=self.config.min_interval_minutes,
                )
            except Exception as exc:
                reason = f"advisory_error:{type(exc).__name__}"
                self._persist(
                    snapshot, market_gate, news_gate, candidate_hash=candidate_hash,
                    disposition="STATE_BLOCKED", reason=reason,
                )
                self._state = replace(
                    self._state, state="ERROR", candidate_time=candidate,
                    last_result="ERROR", reason=reason,
                )
                return None

            disposition, state = "STATE_BLOCKED", "BLOCKED"
            decision = None
            reason = outcome.skip_reason
            usage_id = outcome.reservation.usage_id if outcome.reservation else None
            if outcome.attempted and outcome.result is not None:
                decision = outcome.result.decision.decision.value
                if outcome.result.status == "success":
                    disposition, state, reason = "SENT_TO_AI", decision, None
                else:
                    disposition, state, reason = "AI_ERROR", "ERROR", outcome.result.status
            elif reason == "rate_spacing_blocked":
                disposition, state = "RATE_SPACING_BLOCKED", "WAITING_SPACING"
            elif reason == "completed_m1_already_consumed":
                disposition = "DUPLICATE"
            elif reason in {"daily_call_limit", "daily_spend_limit", "weekly_spend_limit"}:
                disposition = "BUDGET_BLOCKED"

            self._persist(
                snapshot, market_gate, news_gate,
                candidate_hash=outcome.input_hash or candidate_hash,
                disposition=disposition, reason=reason, usage_id=usage_id,
                decision=decision,
            )
            self._refresh_spacing()
            self._state = replace(
                self._state, state=state, candidate_time=candidate,
                last_result=decision, reason=reason,
            )
            return outcome
