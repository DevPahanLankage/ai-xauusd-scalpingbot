from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path

from xauusd_bot.advisory_service import AIAdvisoryOutcome
from xauusd_bot.application_state import EventFeed, build_application_state
from xauusd_bot.auto_advisory import AutomaticAdvisoryCoordinator
from xauusd_bot.config import AIConfig, AutoAdvisoryConfig, MarketGateConfig, Settings
from xauusd_bot.market_gate import MarketGate
from xauusd_bot.preview import AIPreviewService
from xauusd_bot.state_store import SQLiteStateStore
from tests.test_advisory_flow import safe_news
from tests.test_market_gate import _snapshot


BASE = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)


class _NeverService:
    def __init__(self) -> None:
        self.calls = 0

    async def evaluate(self, *_: object, **__: object) -> AIAdvisoryOutcome:
        self.calls += 1
        return AIAdvisoryOutcome(False, "blocked", None, None, None)


class ResearchCandidatePersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = SQLiteStateStore(self.path)
        self.snapshot = _snapshot()
        self.gate = MarketGate(MarketGateConfig()).evaluate(self.snapshot)
        self.news = safe_news()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def count(self) -> int:
        with closing(sqlite3.connect(self.path)) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM research_candidates").fetchone()[0])

    async def test_quote_refresh_persists_candidate_once(self) -> None:
        service = _NeverService()
        coordinator = AutomaticAdvisoryCoordinator(
            AutoAdvisoryConfig(False), service, state_store=self.store  # type: ignore[arg-type]
        )
        await coordinator.maybe_evaluate(self.snapshot, self.gate, self.news, candidate_hash="hash")
        await coordinator.maybe_evaluate(self.snapshot, self.gate, self.news, candidate_hash="hash")
        self.assertEqual(self.count(), 1)
        row = self.store.research_candidate("XAUUSD", self.gate.completed_m1_time or "")
        assert row is not None
        self.assertEqual(row["disposition"], "AUTO_DISABLED")
        self.assertEqual(row["candidate_hash"], "hash")

    async def test_rejected_candidate_is_persisted(self) -> None:
        gate = replace(self.gate, eligible_for_ai=False, rejection_reasons=("spread_too_wide",))
        coordinator = AutomaticAdvisoryCoordinator(
            AutoAdvisoryConfig(True), _NeverService(), state_store=self.store  # type: ignore[arg-type]
        )
        await coordinator.maybe_evaluate(self.snapshot, gate, self.news)
        row = self.store.research_candidate("XAUUSD", gate.completed_m1_time or "")
        assert row is not None
        self.assertEqual(row["disposition"], "NOT_ELIGIBLE")
        self.assertFalse(bool(row["market_gate_eligible"]))

    async def test_browser_reconnect_does_not_duplicate_candidate(self) -> None:
        for _ in range(2):
            coordinator = AutomaticAdvisoryCoordinator(
                AutoAdvisoryConfig(False), _NeverService(), state_store=SQLiteStateStore(self.path)  # type: ignore[arg-type]
            )
            await coordinator.maybe_evaluate(self.snapshot, self.gate, self.news)
        self.assertEqual(self.count(), 1)

    async def test_rejected_candidate_can_become_eligible_without_duplicate_row(self) -> None:
        coordinator = AutomaticAdvisoryCoordinator(
            AutoAdvisoryConfig(False), _NeverService(), state_store=self.store  # type: ignore[arg-type]
        )
        rejected = replace(self.gate, eligible_for_ai=False, rejection_reasons=("spread_too_wide",))
        await coordinator.maybe_evaluate(self.snapshot, rejected, self.news)
        await coordinator.maybe_evaluate(self.snapshot, self.gate, self.news)
        row = self.store.research_candidate("XAUUSD", self.gate.completed_m1_time or "")
        assert row is not None
        self.assertEqual(self.count(), 1)
        self.assertEqual(row["disposition"], "AUTO_DISABLED")
        self.assertTrue(bool(row["market_gate_eligible"]))

    async def test_research_row_contains_no_account_identity(self) -> None:
        coordinator = AutomaticAdvisoryCoordinator(
            AutoAdvisoryConfig(False), _NeverService(), state_store=self.store  # type: ignore[arg-type]
        )
        await coordinator.maybe_evaluate(self.snapshot, self.gate, self.news)
        row = self.store.research_candidate("XAUUSD", self.gate.completed_m1_time or "")
        encoded = str(row).lower()
        for forbidden in ("'login':", "'broker':", "'server':", "api_key", "mcp_token", "authorization"):
            self.assertNotIn(forbidden, encoded)


class PaidCallSpacingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = SQLiteStateStore(self.path)
        self.config = AIConfig(api_key="fake", state_db_path=self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def reserve(self, candidate: str, now: datetime, interval: float):
        return self.store.begin_ai_attempt(
            symbol="XAUUSD", completed_m1_time=candidate, input_hash=candidate,
            config=self.config, now=now, automatic=True,
            min_interval_minutes=interval,
        )

    def test_spacing_disabled_preserves_old_behavior(self) -> None:
        self.assertTrue(self.reserve("2026-10-06T10:00:00", BASE, 0).reserved)
        self.assertTrue(self.reserve("2026-10-06T10:01:00", BASE + timedelta(seconds=1), 0).reserved)

    def test_spacing_blocks_without_consuming_candidate(self) -> None:
        self.assertTrue(self.reserve("2026-10-06T10:00:00", BASE, 10).reserved)
        blocked = self.reserve("2026-10-06T10:01:00", BASE + timedelta(minutes=5), 10)
        self.assertFalse(blocked.reserved)
        self.assertEqual(blocked.reason, "rate_spacing_blocked")
        inspection = SQLiteStateStore.inspect_read_only(
            self.path, symbol="XAUUSD", completed_m1_time="2026-10-06T10:01:00", now=BASE
        )
        self.assertFalse(inspection.candidate_consumed)

    def test_spacing_survives_restart_and_next_candidate_can_call(self) -> None:
        self.assertTrue(self.reserve("2026-10-06T10:00:00", BASE, 10).reserved)
        restarted = SQLiteStateStore(self.path)
        blocked = restarted.begin_ai_attempt(
            symbol="XAUUSD", completed_m1_time="2026-10-06T10:05:00",
            input_hash="blocked", config=self.config,
            now=BASE + timedelta(minutes=5), automatic=True,
            min_interval_minutes=10,
        )
        self.assertEqual(blocked.reason, "rate_spacing_blocked")
        allowed = restarted.begin_ai_attempt(
            symbol="XAUUSD", completed_m1_time="2026-10-06T10:11:00",
            input_hash="allowed", config=self.config,
            now=BASE + timedelta(minutes=11), automatic=True,
            min_interval_minutes=10,
        )
        self.assertTrue(allowed.reserved)

    def test_spacing_status_exposes_countdown(self) -> None:
        reservation = self.reserve("2026-10-06T10:00:00", BASE, 10)
        assert reservation.usage_id is not None
        self.store.mark_auto_call_started(reservation.usage_id, BASE + timedelta(seconds=2))
        status = self.store.auto_spacing_status(10, BASE + timedelta(minutes=4, seconds=2))
        self.assertEqual(status.seconds_since_last_call, 240)
        self.assertEqual(status.seconds_until_eligible, 360)

    def test_spacing_state_does_not_mark_market_invalid(self) -> None:
        snapshot = _snapshot()
        gate = MarketGate(MarketGateConfig()).evaluate(snapshot)
        news = safe_news()
        preview = AIPreviewService(self.config).evaluate(snapshot, gate, news)
        state = build_application_state(
            snapshot=snapshot,
            market_gate=gate,
            news_gate=news,
            preview=preview,
            settings=Settings(mcp_url="http://127.0.0.1/mcp", ai=self.config),
            last_advisory=None,
            events=EventFeed(),
            started_at=BASE,
            mode="test",
            auto_advisory={
                "enabled": True,
                "state": "WAITING_SPACING",
                "candidate_time": gate.completed_m1_time,
                "last_call_time": BASE.isoformat(),
                "last_result": None,
                "reason": "rate_spacing_blocked",
                "min_interval_minutes": 10,
                "seconds_since_last_call": 120,
                "next_eligible_time": (BASE + timedelta(minutes=10)).isoformat(),
                "seconds_until_eligible": 480,
            },
        )
        self.assertTrue(state["validity"]["eligible"])
        self.assertEqual(state["validity"]["status"], "VALID")
        self.assertEqual(state["ai"]["state"], "WAITING_SPACING")


if __name__ == "__main__":
    unittest.main()
