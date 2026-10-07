from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from xauusd_bot.advisory_service import AIAdvisoryOutcome, AIAdvisoryService
from xauusd_bot.ai_models import AdvisoryResult, TokenUsage, TradeDecision
from xauusd_bot.auto_advisory import AutomaticAdvisoryCoordinator
from xauusd_bot.config import AIConfig, AutoAdvisoryConfig, MarketGateConfig, PaperConfig
from xauusd_bot.market_gate import MarketGate
from xauusd_bot.models import Tick
from xauusd_bot.paper import PaperPerformanceTracker
from xauusd_bot.state_store import SQLiteStateStore
from tests.test_advisor import decision
from tests.test_advisory_flow import _FakeAdvisor, safe_news
from tests.test_market_gate import SERVER_TIME, _snapshot


def advisory(side: TradeDecision, **updates: object) -> AdvisoryResult:
    defaults: dict[str, object] = {
        "entry_price": 2010.0,
        "stop_loss": 2009.0,
        "take_profit": 2012.0,
    }
    if side is TradeDecision.SELL:
        defaults.update(stop_loss=2011.0, take_profit=2008.0)
    defaults.update(updates)
    return AdvisoryResult(
        decision=decision(side, **defaults),
        status="success",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        estimated_cost_usd=0.001,
        latency_ms=1.0,
    )


class _Service:
    def __init__(self, outcome: AIAdvisoryOutcome | None = None, error: Exception | None = None) -> None:
        self.calls = 0
        self.outcome = outcome or AIAdvisoryOutcome(False, "blocked", None, None, None)
        self.error = error

    async def evaluate(self, *_: object, **__: object) -> AIAdvisoryOutcome:
        self.calls += 1
        if self.error:
            raise self.error
        return self.outcome


class AutomaticAdvisoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.snapshot = _snapshot()
        self.gate = MarketGate(MarketGateConfig()).evaluate(self.snapshot)
        self.news = safe_news()

    async def test_disabled_mode_makes_zero_calls(self) -> None:
        service = _Service()
        coordinator = AutomaticAdvisoryCoordinator(AutoAdvisoryConfig(False), service)  # type: ignore[arg-type]
        await coordinator.maybe_evaluate(self.snapshot, self.gate, self.news)
        self.assertEqual(service.calls, 0)
        self.assertEqual(coordinator.state.state, "OFF")

    async def test_one_eligible_candidate_calls_once_and_shows_analyzing(self) -> None:
        result = advisory(TradeDecision.BUY)
        service = _Service(AIAdvisoryOutcome(True, None, "hash", None, result))
        coordinator = AutomaticAdvisoryCoordinator(AutoAdvisoryConfig(True), service)  # type: ignore[arg-type]
        states: list[str] = []
        async def capture() -> None:
            states.append(coordinator.state.state)
        await coordinator.maybe_evaluate(self.snapshot, self.gate, self.news, on_analyzing=capture)
        await coordinator.maybe_evaluate(self.snapshot, self.gate, self.news)
        self.assertEqual(service.calls, 1)
        self.assertEqual(states, ["ANALYZING"])
        self.assertEqual(coordinator.state.state, "BUY")

    async def test_gate_and_news_rejections_never_call_service(self) -> None:
        service = _Service()
        coordinator = AutomaticAdvisoryCoordinator(AutoAdvisoryConfig(True), service)  # type: ignore[arg-type]
        rejected = replace(self.gate, eligible_for_ai=False)
        await coordinator.maybe_evaluate(self.snapshot, rejected, self.news)
        await coordinator.maybe_evaluate(
            self.snapshot, self.gate, replace(self.news, safe_for_ai=False)
        )
        self.assertEqual(service.calls, 0)

    async def test_concurrent_quote_updates_are_single_flight(self) -> None:
        service = _Service()
        coordinator = AutomaticAdvisoryCoordinator(AutoAdvisoryConfig(True), service)  # type: ignore[arg-type]
        await asyncio.gather(
            *(coordinator.maybe_evaluate(self.snapshot, self.gate, self.news) for _ in range(12))
        )
        self.assertEqual(service.calls, 1)

    async def test_service_failure_becomes_error_without_retrying_candidate(self) -> None:
        service = _Service(error=OSError("state unavailable"))
        coordinator = AutomaticAdvisoryCoordinator(AutoAdvisoryConfig(True), service)  # type: ignore[arg-type]
        await coordinator.maybe_evaluate(self.snapshot, self.gate, self.news)
        await coordinator.maybe_evaluate(self.snapshot, self.gate, self.news)
        self.assertEqual(service.calls, 1)
        self.assertEqual(coordinator.state.state, "ERROR")

    async def test_missing_key_never_initializes_or_calls_advisor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = AIAdvisoryService(
                AIConfig(api_key=None, state_db_path=Path(temp) / "state.sqlite3")
            )
            coordinator = AutomaticAdvisoryCoordinator(AutoAdvisoryConfig(True), service)
            outcome = await coordinator.maybe_evaluate(self.snapshot, self.gate, self.news)
            assert outcome is not None
            self.assertFalse(outcome.attempted)
            self.assertEqual(outcome.skip_reason, "missing_openai_api_key")

    async def test_restart_uses_persistent_duplicate_protection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            config = AIConfig(api_key="fake", state_db_path=path)
            first_advisor, second_advisor = _FakeAdvisor(), _FakeAdvisor()
            first = AutomaticAdvisoryCoordinator(
                AutoAdvisoryConfig(True),
                AIAdvisoryService(config, advisor=first_advisor),
            )
            second = AutomaticAdvisoryCoordinator(
                AutoAdvisoryConfig(True),
                AIAdvisoryService(config, advisor=second_advisor),
            )
            await first.maybe_evaluate(self.snapshot, self.gate, self.news)
            outcome = await second.maybe_evaluate(self.snapshot, self.gate, self.news)
            assert outcome is not None
            self.assertEqual(first_advisor.calls, 1)
            self.assertEqual(second_advisor.calls, 0)
            self.assertEqual(outcome.skip_reason, "completed_m1_already_consumed")

    async def test_budget_rejection_makes_zero_advisor_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            config = AIConfig(api_key="fake", state_db_path=path, max_calls_per_day=1)
            store = SQLiteStateStore(path)
            store.begin_ai_attempt(
                symbol="XAUUSD", completed_m1_time="2026-01-05T12:08:00",
                input_hash="prior", config=config,
            )
            fake = _FakeAdvisor()
            coordinator = AutomaticAdvisoryCoordinator(
                AutoAdvisoryConfig(True),
                AIAdvisoryService(config, state_store=store, advisor=fake),
            )
            outcome = await coordinator.maybe_evaluate(self.snapshot, self.gate, self.news)
            assert outcome is not None
            self.assertEqual(outcome.skip_reason, "daily_call_limit")
            self.assertEqual(fake.calls, 0)


class PaperTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = SQLiteStateStore(self.path)
        self.config = AIConfig(api_key="fake", state_db_path=self.path)
        self.paper = PaperPerformanceTracker(
            self.store,
            PaperConfig(
                entry_expiry_minutes=10,
                max_trade_minutes=30,
                max_observation_gap_seconds=3_600,
            ),
        )
        self.snapshot = _snapshot()
        self.gate = MarketGate(MarketGateConfig()).evaluate(self.snapshot)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def register(self, result: AdvisoryResult) -> int:
        reservation = self.store.begin_ai_attempt(
            symbol="XAUUSD",
            completed_m1_time=self.gate.completed_m1_time or "",
            input_hash="safe-hash",
            config=self.config,
        )
        self.store.finish_ai_attempt(reservation, result)
        registration = self.paper.register(
            reservation=reservation,
            result=result,
            snapshot=self.snapshot,
            market_gate=self.gate,
            news_gate=safe_news(),
        )
        assert registration.paper_id is not None
        return registration.paper_id

    def observed(self, minutes: float, ticks: list[tuple[float, float, float]]):
        recent = tuple(
            Tick(
                time=(SERVER_TIME + timedelta(minutes=offset)).isoformat(),
                bid=bid,
                ask=ask,
            )
            for offset, bid, ask in ticks
        )
        last_bid = recent[-1].bid if recent else self.snapshot.symbol.bid
        last_ask = recent[-1].ask if recent else self.snapshot.symbol.ask
        return replace(
            self.snapshot,
            trade_server_time=(SERVER_TIME + timedelta(minutes=minutes)).isoformat(),
            symbol=replace(self.snapshot.symbol, bid=last_bid, ask=last_ask),
            recent_ticks=recent,
        )

    def latest(self) -> dict:
        value = self.store.paper_dashboard()["latest"]
        assert value is not None
        return value

    def test_buy_uses_ask_for_fill_and_bid_for_take_profit(self) -> None:
        self.register(advisory(TradeDecision.BUY))
        self.paper.observe(self.observed(2, [(1, 2009.8, 2009.95), (2, 2012.1, 2012.3)]))
        row = self.latest()
        self.assertEqual(row["status"], "TP_HIT")
        self.assertEqual(row["fill_price"], 2009.95)
        self.assertGreater(float(row["final_r"]), 0)

    def test_sell_uses_bid_for_fill_and_ask_for_stop(self) -> None:
        self.register(advisory(TradeDecision.SELL))
        self.paper.observe(self.observed(2, [(1, 2010.1, 2010.25), (2, 2010.9, 2011.1)]))
        row = self.latest()
        self.assertEqual(row["status"], "SL_HIT")
        self.assertLess(float(row["final_r"]), 0)

    def test_unfilled_entry_expires(self) -> None:
        self.register(advisory(TradeDecision.BUY, entry_price=2000.0, stop_loss=1999.0, take_profit=2002.0))
        self.paper.observe(self.observed(11, [(11, 2009.8, 2010.0)]))
        self.assertEqual(self.latest()["status"], "EXPIRED_UNFILLED")

    def test_restart_gap_cancels_pending_entry_instead_of_manufacturing_expiry(self) -> None:
        self.register(advisory(TradeDecision.BUY, entry_price=2000.0, stop_loss=1999.0, take_profit=2002.0))
        strict = PaperPerformanceTracker(
            self.store,
            replace(self.paper.config, max_observation_gap_seconds=90.0),
        )
        strict.observe(self.observed(11, [(11, 2009.8, 2010.0)]))
        row = self.latest()
        self.assertEqual(row["status"], "CANCELLED_BY_SAFETY")
        self.assertEqual(row["notes"], "observation_gap_exceeded")

    def test_open_trade_expires_and_records_excursions_and_r(self) -> None:
        self.register(advisory(TradeDecision.BUY))
        self.paper.observe(self.observed(31, [(1, 2009.8, 2009.95), (5, 2010.5, 2010.7), (31, 2010.2, 2010.4)]))
        row = self.latest()
        self.assertEqual(row["status"], "EXPIRED_OPEN")
        self.assertGreater(float(row["mfe_r"]), 0)
        self.assertIsNotNone(row["final_r"])

    def test_no_trade_is_observation_not_fake_trade(self) -> None:
        self.register(advisory(TradeDecision.NO_TRADE))
        self.paper.observe(self.observed(31, [(1, 2010.0, 2010.2), (30, 2011.0, 2011.2)]))
        dashboard = self.store.paper_dashboard()
        self.assertEqual(dashboard["latest"]["status"], "OBSERVATION_COMPLETE")
        self.assertEqual(dashboard["stats"]["completed"], 0)
        self.assertEqual(dashboard["stats"]["decision_counts"]["NO_TRADE"], 1)

    def test_checkpoints_persist_across_tracker_restart(self) -> None:
        self.register(advisory(TradeDecision.NO_TRADE))
        self.paper.observe(self.observed(5, [(1, 2010.0, 2010.2), (3, 2010.2, 2010.4), (5, 2010.3, 2010.5)]))
        restarted = PaperPerformanceTracker(SQLiteStateStore(self.path), self.paper.config)
        restarted.observe(self.observed(11, [(10, 2010.5, 2010.7)]))
        minutes = [item["checkpoint_minutes"] for item in self.latest()["checkpoints"]]
        self.assertEqual(minutes, [1, 3, 5, 10])

    def test_no_trade_does_not_complete_without_tick_at_30m_target(self) -> None:
        self.register(advisory(TradeDecision.NO_TRADE))
        self.paper.observe(
            self.observed(
                15,
                [
                    (1, 2010.0, 2010.2),
                    (3, 2010.1, 2010.3),
                    (5, 2010.2, 2010.4),
                    (10, 2010.3, 2010.5),
                    (15, 2010.4, 2010.6),
                ],
            )
        )
        self.paper.observe(self.observed(30, [(29.99, 2010.5, 2010.7)]))
        row = self.latest()
        self.assertEqual(row["status"], "OBSERVING")
        self.assertNotIn(
            30, [item["checkpoint_minutes"] for item in row["checkpoints"]]
        )

    def test_later_valid_30m_tick_records_checkpoint_then_completes(self) -> None:
        self.register(advisory(TradeDecision.NO_TRADE))
        self.paper.observe(
            self.observed(
                30,
                [
                    (1, 2010.0, 2010.2),
                    (3, 2010.1, 2010.3),
                    (5, 2010.2, 2010.4),
                    (10, 2010.3, 2010.5),
                    (15, 2010.4, 2010.6),
                    (29.99, 2010.5, 2010.7),
                ],
            )
        )
        self.assertEqual(self.latest()["status"], "OBSERVING")
        self.paper.observe(self.observed(30.2, [(30.1, 2010.6, 2010.8)]))
        row = self.latest()
        self.assertEqual(row["status"], "OBSERVATION_COMPLETE")
        self.assertEqual(
            [item["checkpoint_minutes"] for item in row["checkpoints"]],
            [1, 3, 5, 10, 15, 30],
        )

    def test_no_trade_excessive_gap_remains_conservatively_incomplete(self) -> None:
        self.register(advisory(TradeDecision.NO_TRADE))
        strict = PaperPerformanceTracker(
            self.store,
            replace(self.paper.config, max_observation_gap_seconds=90.0),
        )
        strict.observe(self.observed(5, [(5, 2010.2, 2010.4)]))
        row = self.latest()
        self.assertEqual(row["status"], "OBSERVATION_INCOMPLETE")
        self.assertEqual(row["notes"], "observation_gap_exceeded")

    def test_invalid_geometry_is_persisted_invalid(self) -> None:
        self.register(advisory(TradeDecision.BUY, stop_loss=2011.0, take_profit=2012.0))
        self.assertEqual(self.latest()["status"], "INVALID")

    def test_same_candle_dual_hit_is_persisted_ambiguous(self) -> None:
        paper_id = self.register(advisory(TradeDecision.BUY))
        self.paper.observe(self.observed(1, [(1, 2009.8, 2009.95)]))
        status = self.paper.apply_candle_exit_evidence(
            paper_id=paper_id,
            candle_time=(SERVER_TIME + timedelta(minutes=2)).isoformat(),
            high=2012.5,
            low=2008.5,
        )
        self.assertEqual(status, "AMBIGUOUS")
        self.assertEqual(self.latest()["status"], "AMBIGUOUS")

    def test_persisted_context_contains_no_identity_or_secrets(self) -> None:
        self.register(advisory(TradeDecision.BUY))
        encoded = json.dumps(self.latest()).lower()
        for forbidden in ("login", "broker", "api_key", "authorization", "mcp_token"):
            self.assertNotIn(forbidden, encoded)


class AdvisoryPaperIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_advisory_creates_exactly_one_paper_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            store = SQLiteStateStore(path)
            fake = _FakeAdvisor()
            snapshot = _snapshot()
            gate = MarketGate(MarketGateConfig()).evaluate(snapshot)
            service = AIAdvisoryService(
                AIConfig(api_key="fake", state_db_path=path),
                state_store=store,
                advisor=fake,
            )
            first = await service.evaluate(snapshot, gate, safe_news())
            second = await service.evaluate(snapshot, gate, safe_news())
            self.assertIsNotNone(first.paper_id)
            self.assertFalse(second.attempted)
            self.assertEqual(len(store.paper_dashboard()["history"]), 1)


if __name__ == "__main__":
    unittest.main()
