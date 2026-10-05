from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from xauusd_bot import cli
from xauusd_bot.advisory_service import AIAdvisoryService
from xauusd_bot.ai_models import AdvisoryResult, TokenUsage, TradeDecision
from xauusd_bot.config import AIConfig, MarketGateConfig, NewsGateConfig, Settings
from xauusd_bot.economic_calendar import EconomicCalendarGate
from xauusd_bot.market_gate import MarketGate
from xauusd_bot.payload import build_ai_payload
from xauusd_bot.state_store import SQLiteStateStore
from tests.test_advisor import decision
from tests.test_market_gate import SERVER_TIME, _snapshot
from tests.test_news_gate import event


class _FakeAdvisor:
    def __init__(self) -> None:
        self.calls = 0
        self.payloads: list[dict] = []

    def evaluate(self, payload: dict) -> AdvisoryResult:
        self.calls += 1
        self.payloads.append(payload)
        return AdvisoryResult(
            decision=decision(TradeDecision.BUY),
            status="success",
            usage=TokenUsage(input_tokens=500, output_tokens=100),
            estimated_cost_usd=0.002,
            latency_ms=12.0,
        )


def safe_news():
    return EconomicCalendarGate(NewsGateConfig()).evaluate(
        SERVER_TIME.isoformat(), ()
    )


class AdvisoryFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.config = AIConfig(api_key="fake", state_db_path=self.path)
        self.store = SQLiteStateStore(self.path)
        self.advisor = _FakeAdvisor()

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_market_gate_rejected_makes_zero_openai_calls(self) -> None:
        snapshot = _snapshot()
        rejected_snapshot = replace(
            snapshot,
            symbol=replace(snapshot.symbol, ask=snapshot.symbol.bid + 2.0),
        )
        market = MarketGate(MarketGateConfig()).evaluate(rejected_snapshot)
        outcome = await AIAdvisoryService(
            self.config, state_store=self.store, advisor=self.advisor
        ).evaluate(rejected_snapshot, market, safe_news())
        self.assertFalse(outcome.attempted)
        self.assertEqual(self.advisor.calls, 0)

    async def test_news_blackout_makes_zero_openai_calls(self) -> None:
        snapshot = _snapshot()
        market = MarketGate(MarketGateConfig()).evaluate(snapshot)
        news = EconomicCalendarGate(NewsGateConfig()).evaluate(
            snapshot.trade_server_time, (event(10),)
        )
        outcome = await AIAdvisoryService(
            self.config, state_store=self.store, advisor=self.advisor
        ).evaluate(snapshot, market, news)
        self.assertFalse(outcome.attempted)
        self.assertEqual(self.advisor.calls, 0)

    async def test_eligible_candidate_makes_exactly_one_call(self) -> None:
        snapshot = _snapshot()
        market = MarketGate(MarketGateConfig()).evaluate(snapshot)
        outcome = await AIAdvisoryService(
            self.config, state_store=self.store, advisor=self.advisor
        ).evaluate(snapshot, market, safe_news())
        self.assertTrue(outcome.attempted)
        self.assertEqual(self.advisor.calls, 1)

    async def test_same_candidate_twice_makes_only_one_call(self) -> None:
        snapshot = _snapshot()
        market = MarketGate(MarketGateConfig()).evaluate(snapshot)
        service = AIAdvisoryService(
            self.config, state_store=self.store, advisor=self.advisor
        )
        first = await service.evaluate(snapshot, market, safe_news())
        second = await service.evaluate(snapshot, market, safe_news())
        self.assertTrue(first.attempted)
        self.assertFalse(second.attempted)
        self.assertEqual(second.skip_reason, "completed_m1_already_consumed")
        self.assertEqual(self.advisor.calls, 1)

    async def test_missing_api_key_skips_request(self) -> None:
        snapshot = _snapshot()
        market = MarketGate(MarketGateConfig()).evaluate(snapshot)
        outcome = await AIAdvisoryService(
            replace(self.config, api_key=None), state_store=self.store
        ).evaluate(snapshot, market, safe_news())
        self.assertFalse(outcome.attempted)
        self.assertEqual(outcome.skip_reason, "missing_openai_api_key")

    async def test_invalid_payload_is_rejected_before_candidate_reservation(self) -> None:
        snapshot = _snapshot()
        market = MarketGate(MarketGateConfig()).evaluate(snapshot)
        malformed = replace(
            snapshot,
            m1_candles=(
                *snapshot.m1_candles[:-2],
                snapshot.m1_candles[-1],
                snapshot.m1_candles[-2],
            ),
        )
        outcome = await AIAdvisoryService(
            self.config, state_store=self.store, advisor=self.advisor
        ).evaluate(malformed, market, safe_news())
        self.assertFalse(outcome.attempted)
        self.assertEqual(outcome.skip_reason, "invalid_sanitized_payload")
        self.assertEqual(self.advisor.calls, 0)
        self.assertEqual(self.store.usage_summary().calls_today, 0)

    def test_payload_excludes_account_login_funds_and_secrets(self) -> None:
        snapshot = _snapshot()
        market = MarketGate(MarketGateConfig()).evaluate(snapshot)
        payload = build_ai_payload(
            snapshot, market, safe_news(), m1_limit=30, m5_limit=20
        )
        encoded = json.dumps(payload).lower()
        for forbidden in (
            "login",
            "balance",
            "equity",
            "free_margin",
            "broker",
            "api_key",
            "authorization",
            "mcp_token",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(len(payload["completed_m1_candles"]), 30)
        self.assertEqual(len(payload["completed_m5_candles"]), 20)


class CLISafetyTests(unittest.TestCase):
    def test_normal_cli_never_selects_ai_path(self) -> None:
        settings = Settings(mcp_url="http://127.0.0.1/mcp")
        with (
            patch.object(cli.Settings, "from_environment", return_value=settings),
            patch.object(cli, "_run", new=AsyncMock()) as normal,
            patch.object(cli, "_run_ai", new=AsyncMock()) as ai,
            patch.object(cli, "_run_ai_preview", new=AsyncMock()) as preview,
            patch.object(cli, "load_dotenv"),
        ):
            cli.main([])
        normal.assert_awaited_once()
        ai.assert_not_awaited()
        preview.assert_not_awaited()

    def test_preview_cli_never_selects_paid_ai_path(self) -> None:
        settings = Settings(mcp_url="http://127.0.0.1/mcp")
        with (
            patch.object(cli.Settings, "from_environment", return_value=settings),
            patch.object(cli, "_run", new=AsyncMock()) as normal,
            patch.object(cli, "_run_ai", new=AsyncMock()) as ai,
            patch.object(cli, "_run_ai_preview", new=AsyncMock()) as preview,
            patch.object(cli, "load_dotenv"),
        ):
            cli.main(["--ai-preview"])
        preview.assert_awaited_once()
        normal.assert_not_awaited()
        ai.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
