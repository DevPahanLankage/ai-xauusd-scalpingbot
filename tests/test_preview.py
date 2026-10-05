from __future__ import annotations

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

    def test_eligible_preview_builds_exact_payload_without_openai_or_state(self) -> None:
        with patch("xauusd_bot.advisor.OpenAIAdvisor") as advisor:
            preview = AIPreviewService(self.config).evaluate(
                self.snapshot, self.market, safe_news()
            )
        advisor.assert_not_called()
        self.assertTrue(preview.would_request)
        self.assertIsNotNone(preview.input_hash)
        self.assertIsNotNone(preview.sanitized_payload)
        self.assertFalse(self.path.exists())
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

        preview = AIPreviewService(self.config).evaluate(
            self.snapshot, self.market, safe_news()
        )

        after = SQLiteStateStore.usage_summary_read_only(self.path)
        self.assertEqual(after, before)
        self.assertTrue(preview.candidate_consumed)
        self.assertFalse(preview.would_request)
        self.assertEqual(
            preview.skip_reasons.count("completed_m1_already_consumed"), 1
        )


if __name__ == "__main__":
    unittest.main()
