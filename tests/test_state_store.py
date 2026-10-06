from __future__ import annotations

import sqlite3
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


def result(cost: float | None) -> AdvisoryResult:
    return AdvisoryResult(
        decision=decision(TradeDecision.NO_TRADE),
        status="success" if cost is not None else "api_error",
        usage=(TokenUsage(input_tokens=100, output_tokens=20) if cost is not None else None),
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

    def test_crash_restart_retains_candidate_and_budget_reservation(self) -> None:
        config = AIConfig(state_db_path=self.path, budget_reserve_per_call_usd=0.05)
        first = self.reserve(SQLiteStateStore(self.path), "2026-01-07T11:59:00", config)
        self.assertTrue(first.reserved)

        restarted = SQLiteStateStore(self.path)
        second = self.reserve(restarted, "2026-01-07T11:59:00", config)
        summary = restarted.usage_summary(NOW)
        self.assertFalse(second.reserved)
        self.assertEqual(second.reason, "completed_m1_already_consumed")
        self.assertEqual(summary.known_spend_today_usd, 0.0)
        self.assertEqual(summary.budget_accounted_spend_today_usd, 0.05)

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

    def test_concurrent_requests_cannot_over_reserve_budget(self) -> None:
        config = AIConfig(
            state_db_path=self.path,
            budget_reserve_per_call_usd=0.05,
            daily_spend_cap_usd=0.10,
            weekly_spend_cap_usd=10.0,
            max_calls_per_day=99,
        )
        store = SQLiteStateStore(self.path)
        candles = [f"2026-01-07T11:{minute:02d}:00" for minute in range(50, 58)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            reservations = list(
                executor.map(lambda candle: self.reserve(store, candle, config), candles)
            )
        self.assertEqual(sum(item.reserved for item in reservations), 2)
        summary = store.usage_summary(NOW)
        self.assertEqual(summary.budget_accounted_spend_today_usd, 0.10)

    def test_daily_call_limit_still_applies(self) -> None:
        config = AIConfig(state_db_path=self.path, max_calls_per_day=1)
        store = SQLiteStateStore(self.path)
        self.assertTrue(self.reserve(store, "2026-01-07T11:57:00", config).reserved)
        blocked = self.reserve(store, "2026-01-07T11:58:00", config)
        self.assertEqual(blocked.reason, "daily_call_limit")

    def test_reservation_cannot_cross_daily_cap(self) -> None:
        config = AIConfig(
            state_db_path=self.path,
            budget_reserve_per_call_usd=0.05,
            daily_spend_cap_usd=0.09,
            weekly_spend_cap_usd=10.0,
        )
        store = SQLiteStateStore(self.path)
        self.assertTrue(self.reserve(store, "2026-01-07T11:57:00", config).reserved)
        blocked = self.reserve(store, "2026-01-07T11:58:00", config)
        self.assertEqual(blocked.reason, "daily_spend_limit")

    def test_reservation_cannot_cross_weekly_cap(self) -> None:
        config = AIConfig(
            state_db_path=self.path,
            budget_reserve_per_call_usd=0.05,
            daily_spend_cap_usd=10.0,
            weekly_spend_cap_usd=0.09,
        )
        store = SQLiteStateStore(self.path)
        self.assertTrue(self.reserve(store, "2026-01-07T11:57:00", config).reserved)
        blocked = self.reserve(store, "2026-01-07T11:58:00", config)
        self.assertEqual(blocked.reason, "weekly_spend_limit")

    def test_known_cost_reconciles_reserve_to_lower_actual(self) -> None:
        config = AIConfig(state_db_path=self.path, budget_reserve_per_call_usd=0.05)
        store = SQLiteStateStore(self.path)
        reservation = self.reserve(store, "2026-01-07T11:57:00", config)
        store.finish_ai_attempt(reservation, result(0.0042))
        summary = SQLiteStateStore(self.path).usage_summary(NOW + timedelta(minutes=1))
        self.assertEqual(summary.calls_today, 1)
        self.assertEqual(summary.known_spend_today_usd, 0.0042)
        self.assertEqual(summary.budget_accounted_spend_today_usd, 0.0042)
        self.assertEqual(summary.calls_this_week, 1)
        latest = SQLiteStateStore.last_advisory_read_only(self.path)
        assert latest is not None
        self.assertEqual(latest["decision"], "NO_TRADE")
        self.assertEqual(latest["result"]["decision"]["confidence"], 75)

    def test_unknown_cost_failure_retains_reserve(self) -> None:
        config = AIConfig(state_db_path=self.path, budget_reserve_per_call_usd=0.05)
        store = SQLiteStateStore(self.path)
        reservation = self.reserve(store, "2026-01-07T11:57:00", config)
        store.finish_ai_attempt(reservation, result(None))
        summary = store.usage_summary(NOW)
        self.assertEqual(summary.known_spend_today_usd, 0.0)
        self.assertEqual(summary.budget_accounted_spend_today_usd, 0.05)

    def test_legacy_database_is_migrated_conservatively(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute(
            """
            CREATE TABLE api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                completed_m1_time TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER,
                cached_input_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_tokens INTEGER,
                estimated_cost_usd REAL,
                status TEXT NOT NULL,
                latency_ms REAL
            )
            """
        )
        rows = [
            ("11:57", 0.0042, "success"),
            ("11:58", None, "api_error"),
        ]
        for minute, cost, status in rows:
            connection.execute(
                """
                INSERT INTO api_usage
                    (timestamp, symbol, completed_m1_time, model,
                     estimated_cost_usd, status)
                VALUES (?, 'XAUUSD', ?, 'gpt-6.1-sol', ?, ?)
                """,
                (NOW.isoformat(), f"2026-01-07T{minute}:00", cost, status),
            )
        connection.commit()
        connection.close()

        store = SQLiteStateStore(self.path, legacy_reserve_usd=0.07)
        summary = store.usage_summary(NOW)
        self.assertEqual(summary.known_spend_today_usd, 0.0042)
        self.assertEqual(summary.budget_accounted_spend_today_usd, 0.0742)
        migrated = sqlite3.connect(self.path)
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(api_usage)")}
        migrated.close()
        self.assertTrue(
            {"cache_write_tokens", "budget_reserved_usd", "budget_accounted_usd"}
            <= columns
        )


if __name__ == "__main__":
    unittest.main()
