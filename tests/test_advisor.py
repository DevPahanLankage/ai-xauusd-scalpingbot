from __future__ import annotations

import unittest
from types import SimpleNamespace

from xauusd_bot.advisor import OpenAIAdvisor, estimate_cost
from xauusd_bot.ai_models import AITradeDecision, TokenUsage, TradeDecision
from xauusd_bot.config import AIConfig


def decision(side: TradeDecision, **updates: object) -> AITradeDecision:
    values: dict[str, object] = {
        "decision": side,
        "confidence": 75,
        "market_regime": "trend",
        "setup_summary": "aligned momentum",
        "entry_price": 2000.0,
        "entry_zone_low": None,
        "entry_zone_high": None,
        "stop_loss": 1998.0,
        "take_profit": 2004.0,
        "risk_reward_ratio": 2.0,
        "invalidation_reason": "structure break",
        "warnings": [],
    }
    if side is TradeDecision.SELL:
        values.update(stop_loss=2002.0, take_profit=1996.0)
    if side is TradeDecision.NO_TRADE:
        values.update(
            entry_price=None,
            stop_loss=None,
            take_profit=None,
            risk_reward_ratio=None,
        )
    values.update(updates)
    return AITradeDecision(**values)  # type: ignore[arg-type]


class _Responses:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def client(parsed: object, usage: object | None = None) -> SimpleNamespace:
    response = SimpleNamespace(output_parsed=parsed, usage=usage)
    return SimpleNamespace(responses=_Responses(response))


class AdvisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AIConfig(api_key="fake-test-key")

    def test_buy_structured_response(self) -> None:
        fake = client(decision(TradeDecision.BUY))
        result = OpenAIAdvisor(self.config, fake).evaluate({"symbol": "XAUUSD"})
        self.assertEqual(result.decision.decision, TradeDecision.BUY)
        self.assertEqual(len(fake.responses.calls), 1)
        self.assertNotIn("tools", fake.responses.calls[0])

    def test_buy_entry_zone_response(self) -> None:
        parsed = decision(
            TradeDecision.BUY,
            entry_price=None,
            entry_zone_low=1999.5,
            entry_zone_high=2000.5,
        )
        result = OpenAIAdvisor(self.config, client(parsed)).evaluate({})
        self.assertEqual(result.decision.decision, TradeDecision.BUY)

    def test_sell_structured_response(self) -> None:
        result = OpenAIAdvisor(
            self.config, client(decision(TradeDecision.SELL))
        ).evaluate({})
        self.assertEqual(result.decision.decision, TradeDecision.SELL)

    def test_no_trade_structured_response(self) -> None:
        result = OpenAIAdvisor(
            self.config, client(decision(TradeDecision.NO_TRADE))
        ).evaluate({})
        self.assertEqual(result.decision.decision, TradeDecision.NO_TRADE)

    def test_invalid_buy_geometry_becomes_no_trade(self) -> None:
        parsed = decision(TradeDecision.BUY, stop_loss=2001.0)
        result = OpenAIAdvisor(self.config, client(parsed)).evaluate({})
        self.assertEqual(result.decision.decision, TradeDecision.NO_TRADE)
        self.assertEqual(result.status, "invalid_recommendation")

    def test_invalid_sell_geometry_becomes_no_trade(self) -> None:
        parsed = decision(TradeDecision.SELL, take_profit=2001.0)
        result = OpenAIAdvisor(self.config, client(parsed)).evaluate({})
        self.assertEqual(result.decision.decision, TradeDecision.NO_TRADE)

    def test_invalid_risk_reward_becomes_no_trade(self) -> None:
        parsed = decision(TradeDecision.BUY, risk_reward_ratio=9.0)
        result = OpenAIAdvisor(self.config, client(parsed)).evaluate({})
        self.assertEqual(result.decision.decision, TradeDecision.NO_TRADE)

    def test_non_finite_price_becomes_no_trade(self) -> None:
        parsed = decision(TradeDecision.BUY, entry_price=float("nan"))
        result = OpenAIAdvisor(self.config, client(parsed)).evaluate({})
        self.assertEqual(result.decision.decision, TradeDecision.NO_TRADE)

    def test_unparsed_response_becomes_no_trade(self) -> None:
        result = OpenAIAdvisor(self.config, client(None)).evaluate({})
        self.assertEqual(result.decision.decision, TradeDecision.NO_TRADE)
        self.assertEqual(result.status, "refusal_or_unparsed")

    def test_timeout_becomes_no_trade(self) -> None:
        fake = SimpleNamespace(responses=_Responses(error=TimeoutError("test")))
        result = OpenAIAdvisor(self.config, fake).evaluate({})
        self.assertEqual(result.decision.decision, TradeDecision.NO_TRADE)
        self.assertEqual(result.status, "timeout")

    def test_api_error_becomes_no_trade(self) -> None:
        fake = SimpleNamespace(responses=_Responses(error=RuntimeError("test")))
        result = OpenAIAdvisor(self.config, fake).evaluate({})
        self.assertEqual(result.decision.decision, TradeDecision.NO_TRADE)
        self.assertEqual(result.status, "api_error")

    def test_actual_usage_cost_calculation(self) -> None:
        usage = TokenUsage(
            input_tokens=1000,
            cached_input_tokens=400,
            output_tokens=200,
            reasoning_tokens=50,
        )
        self.assertEqual(estimate_cost(usage, self.config), 0.00324)

    def test_sdk_usage_is_extracted_and_costed(self) -> None:
        usage = SimpleNamespace(
            input_tokens=1000,
            input_tokens_details=SimpleNamespace(cached_tokens=400),
            output_tokens=200,
            output_tokens_details=SimpleNamespace(reasoning_tokens=50),
        )
        result = OpenAIAdvisor(
            self.config, client(decision(TradeDecision.NO_TRADE), usage)
        ).evaluate({})
        self.assertEqual(result.usage, TokenUsage(1000, 400, 200, 50))
        self.assertEqual(result.estimated_cost_usd, 0.00324)


if __name__ == "__main__":
    unittest.main()
