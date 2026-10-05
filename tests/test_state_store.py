from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xauusd_bot.ai_models import AdvisoryResult, TokenUsage, TradeDecision
from xauusd_bot.config import AIConfig
from xauusd_bot.state_store import SQLiteStateStore
from tests.test_advisor import decision


NOW = datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)


def result(cost: float) -> AdvisoryResult:
    return AdvisoryResult(
        decision=decision(TradeDecision.NO_TRADE),
        status="success",
        usage=TokenUsage(input_tokens=100, output_tokens=20),
        estimated_cost_usd=cost,
        latency_ms=10.0,
    )


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def reserve(self, store: SQLiteStateStore, candle: str, config: AIConfig):
        return store.begin_ai_attempt(
            symbol="XAUUSD",
            completed_m1_time=candle,
            input_hash="abc",
            config=config,
            now=NOW,
        )

    def test_same_candidate_is_consumed_after_restart(self) -> None:
        config = AIConfig(state_db_path=self.path)
        first = self.reserve(SQLiteStateStore(self.path), "2026-01-07T11:59:00", config)
        second = self.reserve(SQLiteStateStore(self.path), "2026-01-07T11:59:00", config)
        self.assertTrue(first.reserved)
        self.assertFalse(second.reserved)
        self.assertEqual(second.reason, "completed_m1_already_consumed")

    def test_concurrent_duplicate_reservation_has_one_winner(self) -> None:
        config = AIConfig(state_db_path=self.path)
        store = SQLiteStateStore(self.path)
        with ThreadPoolExecutor(max_workers=8) as executor:
            reservations = list(
                executor.map(
                    lambda _: self.reserve(store, "2026-01-07T11:58:00", config),
                    range(8),
                )
            )
        self.assertEqual(sum(item.reserved for item in reservations), 1)

    def test_daily_call_limit(self) -> None:
        config = AIConfig(state_db_path=self.path, max_calls_per_day=1)
        store = SQLiteStateStore(self.path)
        self.assertTrue(self.reserve(store, "2026-01-07T11:57:00", config).reserved)
        blocked = self.reserve(store, "2026-01-07T11:58:00", config)
        self.assertEqual(blocked.reason, "daily_call_limit")

    def test_daily_spend_limit(self) -> None:
        config = AIConfig(state_db_path=self.path, daily_spend_cap_usd=0.01)
        store = SQLiteStateStore(self.path)
        first = self.reserve(store, "2026-01-07T11:57:00", config)
        store.finish_ai_attempt(first, result(0.02))
        blocked = self.reserve(store, "2026-01-07T11:58:00", config)
        self.assertEqual(blocked.reason, "daily_spend_limit")

    def test_weekly_spend_limit(self) -> None:
        config = AIConfig(
            state_db_path=self.path,
            daily_spend_cap_usd=10.0,
            weekly_spend_cap_usd=0.01,
        )
        store = SQLiteStateStore(self.path)
        first = self.reserve(store, "2026-01-07T11:57:00", config)
        store.finish_ai_attempt(first, result(0.02))
        blocked = self.reserve(store, "2026-01-07T11:58:00", config)
        self.assertEqual(blocked.reason, "weekly_spend_limit")

    def test_usage_and_cost_persist(self) -> None:
        config = AIConfig(state_db_path=self.path)
        store = SQLiteStateStore(self.path)
        reservation = self.reserve(store, "2026-01-07T11:57:00", config)
        store.finish_ai_attempt(reservation, result(0.0042))
        summary = SQLiteStateStore(self.path).usage_summary(NOW + timedelta(minutes=1))
        self.assertEqual(summary.calls_today, 1)
        self.assertEqual(summary.estimated_spend_today_usd, 0.0042)
        self.assertEqual(summary.calls_this_week, 1)


if __name__ == "__main__":
    unittest.main()
