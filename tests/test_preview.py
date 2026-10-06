from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xauusd_bot.config import AIConfig, MarketGateConfig
from xauusd_bot.market_gate import MarketGate
from xauusd_bot.output import preview_to_json
from xauusd_bot.preview import AIPreviewService
from xauusd_bot.state_store import SQLiteStateStore
from tests.test_advisory_flow import safe_news
from tests.test_market_gate import _snapshot


class AIPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "preview-must-not-create.sqlite3"
        self.snapshot = _snapshot()
        self.market = MarketGate(MarketGateConfig()).evaluate(self.snapshot)
        self.config = AIConfig(api_key="not-a-real-key", state_db_path=self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def evaluate(self):
        with patch("xauusd_bot.advisor.OpenAIAdvisor") as advisor:
            preview = AIPreviewService(self.config).evaluate(
                self.snapshot, self.market, safe_news()
            )
        advisor.assert_not_called()
        return preview

    def test_missing_database_is_clean_zero_state_without_creating_it(self) -> None:
        preview = self.evaluate()
        self.assertTrue(preview.would_request)
        self.assertTrue(preview.state_available)
        self.assertFalse(preview.candidate_consumed)
        self.assertIsNotNone(preview.input_hash)
        self.assertIsNotNone(preview.sanitized_payload)
        self.assertFalse(self.path.exists())
        assert preview.usage is not None
        self.assertEqual(preview.usage.calls_today, 0)
        self.assertEqual(preview.usage.budget_accounted_spend_today_usd, 0.0)

        encoded = preview_to_json(preview).lower()
        for forbidden in (
            "not-a-real-key",
            "login",
            "broker",
            "balance",
            "equity",
            "free_margin",
            "mcp_token",
            "openai_api_key",
            "authorization",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_preview_does_not_consume_or_modify_existing_candidate_state(self) -> None:
        store = SQLiteStateStore(self.path)
        candidate = self.market.completed_m1_time
        assert candidate is not None
        reservation = store.begin_ai_attempt(
            symbol=self.snapshot.symbol.symbol,
            completed_m1_time=candidate,
            input_hash="existing",
            config=self.config,
        )
        self.assertTrue(reservation.reserved)
        before = store.usage_summary()

        preview = self.evaluate()

        after = SQLiteStateStore.usage_summary_read_only(self.path)
        self.assertEqual(after, before)
        self.assertTrue(preview.state_available)
        self.assertTrue(preview.candidate_consumed)
        self.assertFalse(preview.would_request)
        self.assertEqual(
            preview.skip_reasons.count("completed_m1_already_consumed"), 1
        )

    def test_corrupt_existing_database_fails_closed_without_repair(self) -> None:
        original = b"this is not a sqlite database"
        self.path.write_bytes(original)

        preview = self.evaluate()

        self.assertFalse(preview.state_available)
        self.assertIsNone(preview.usage)
        self.assertIsNone(preview.budget)
        self.assertIsNone(preview.candidate_consumed)
        self.assertFalse(preview.would_request)
        self.assertIn("persistent_state_unavailable", preview.skip_reasons)
        self.assertEqual(self.path.read_bytes(), original)
        rendered = json.loads(preview_to_json(preview))["ai_preview"]
        self.assertFalse(rendered["state_available"])
        self.assertIsNone(rendered["usage"])
        self.assertIsNone(rendered["candidate_consumed"])

    def test_sqlite_inspection_error_fails_closed_without_openai(self) -> None:
        SQLiteStateStore(self.path)
        with patch.object(
            SQLiteStateStore,
            "_connect_read_only",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            preview = self.evaluate()

        self.assertFalse(preview.state_available)
        self.assertIsNone(preview.usage)
        self.assertIsNone(preview.candidate_consumed)
        self.assertFalse(preview.would_request)
        self.assertIn("persistent_state_unavailable", preview.skip_reasons)


if __name__ == "__main__":
    unittest.main()
