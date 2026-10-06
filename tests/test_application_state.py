from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from xauusd_bot.application_state import (
    EventFeed,
    MeaningfulEventTracker,
    build_application_state,
    human_reason,
)
from xauusd_bot.config import AIConfig, MarketGateConfig, Settings
from xauusd_bot.market_gate import MarketGate
from xauusd_bot.preview import AIPreviewService
from tests.test_advisory_flow import safe_news
from tests.test_market_gate import _snapshot


class ApplicationStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "missing.sqlite3"
        self.snapshot = _snapshot()
        self.gate = MarketGate(MarketGateConfig()).evaluate(self.snapshot)
        self.news = safe_news()
        self.settings = Settings(
            mcp_url="http://127.0.0.1/mcp",
            mcp_token="private-mcp-token",
            ai=AIConfig(api_key="private-openai-key", state_db_path=self.path),
        )
        self.preview = AIPreviewService(self.settings.ai).evaluate(
            self.snapshot, self.gate, self.news
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def state(self, *, last=None, feed=None):
        return build_application_state(
            snapshot=self.snapshot,
            market_gate=self.gate,
            news_gate=self.news,
            preview=self.preview,
            settings=self.settings,
            last_advisory=last,
            events=feed or EventFeed(),
            started_at=datetime.now(timezone.utc),
            mode="combined",
        )

    def test_safe_serialization_excludes_identity_and_secrets(self) -> None:
        encoded = json.dumps(self.state()).lower()
        for forbidden in (
            "private-mcp-token",
            "private-openai-key",
            '"login"',
            '"broker"',
            '"server"',
            "authorization",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertIn('"balance"', encoded)

    def test_reason_codes_have_human_readable_mapping(self) -> None:
        self.assertEqual(
            human_reason("m1_m5_directions_not_aligned"),
            "M1 and M5 directions are not aligned",
        )
        self.assertEqual(
            human_reason("calendar_unavailable:MCPToolError"),
            "Economic calendar data is unavailable or invalid",
        )

    def test_current_candidate_is_separate_from_last_advisory(self) -> None:
        state = self.state(
            last={
                "timestamp": "2026-01-05T12:00:00+00:00",
                "candidate_time": "2026-01-05T11:58:00",
                "input_hash": "old-hash",
                "model": "gpt-6.1-sol",
                "status": "success",
                "decision": "NO_TRADE",
                "usage": {},
                "result": None,
            }
        )
        self.assertNotEqual(
            state["system"]["candidate_time"],
            state["ai"]["last_advisory"]["candidate_time"],
        )

    def test_event_feed_deduplicates_and_remains_bounded(self) -> None:
        feed = EventFeed(limit=3)
        self.assertTrue(feed.add("INFO", "same", key="quote"))
        self.assertFalse(feed.add("INFO", "same", key="quote"))
        for index in range(5):
            feed.add("INFO", f"event {index}", key=f"event-{index}")
        self.assertEqual(len(feed.to_list()), 3)

    def test_same_event_message_can_recur_for_a_new_candidate(self) -> None:
        feed = EventFeed()
        self.assertTrue(
            feed.add("INFO", "New completed M1 candle", key="candidate", fingerprint="12:01")
        )
        self.assertFalse(
            feed.add("INFO", "New completed M1 candle", key="candidate", fingerprint="12:01")
        )
        self.assertTrue(
            feed.add("INFO", "New completed M1 candle", key="candidate", fingerprint="12:02")
        )

    def test_tracker_does_not_emit_events_for_price_only_updates(self) -> None:
        feed = EventFeed()
        tracker = MeaningfulEventTracker(feed)
        state = self.state(feed=feed)
        tracker.update(state)
        count = len(feed.to_list())
        state["market"]["bid"] += 0.01
        tracker.update(state)
        self.assertEqual(len(feed.to_list()), count)

    def test_alignment_rejection_transition_emits_one_semantic_event(self) -> None:
        feed = EventFeed()
        tracker = MeaningfulEventTracker(feed)
        state = self.state(feed=feed)
        tracker.update(state)
        baseline = len(feed.to_list())

        blocked = copy.deepcopy(state)
        blocked["validity"]["eligible"] = False
        blocked["validity"]["reasons"] = [
            {
                "code": "m1_m5_directions_not_aligned",
                "message": "M1 and M5 directions are not aligned",
            }
        ]
        blocked["market_gate"]["directions_aligned"] = False
        blocked["market_gate"]["direction_m5"] = "UP"
        tracker.update(blocked)

        transition = feed.to_list()[baseline:]
        self.assertEqual(len(transition), 1)
        self.assertEqual(transition[0]["message"], "M1/M5 directions not aligned")

    def test_distinct_spread_and_alignment_rejections_remain_separate(self) -> None:
        feed = EventFeed()
        tracker = MeaningfulEventTracker(feed)
        state = self.state(feed=feed)
        tracker.update(state)
        baseline = len(feed.to_list())

        blocked = copy.deepcopy(state)
        blocked["validity"]["eligible"] = False
        blocked["validity"]["reasons"] = [
            {
                "code": "spread_exceeds_absolute_limit",
                "message": "Spread exceeds the configured absolute limit",
            },
            {
                "code": "m1_m5_directions_not_aligned",
                "message": "M1 and M5 directions are not aligned",
            },
        ]
        blocked["market_gate"]["directions_aligned"] = False
        blocked["market_gate"]["direction_m5"] = "UP"
        tracker.update(blocked)

        messages = [item["message"] for item in feed.to_list()[baseline:]]
        self.assertEqual(
            messages,
            [
                "Spread exceeds the configured absolute limit",
                "M1/M5 directions not aligned",
            ],
        )


if __name__ == "__main__":
    unittest.main()
