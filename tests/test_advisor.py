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


def market_payload() -> dict:
    return {
        "instrument": {"bid": 1999.9, "ask": 2000.1, "point": 0.01},
        "market_context": {
            "m1_average_range_points": 100.0,
            "m5_average_range_points": 200.0,
        },
    }


class AdvisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AIConfig(api_key="fake-test-key")

    def test_buy_structured_response(self) -> None:
        fake = client(decision(TradeDecision.BUY))
        result = OpenAIAdvisor(self.config, fake).evaluate(market_payload())
        self.assertEqual(result.decision.decision, TradeDecision.BUY)
        self.assertEqual(len(fake.responses.calls), 1)
        self.assertNotIn("tools", fake.responses.calls[0])
        self.assertIs(fake.responses.calls[0]["store"], False)
        self.assertNotIn("previous_response_id", fake.responses.calls[0])

    def test_buy_entry_zone_response(self) -> None:
        parsed = decision(
            TradeDecision.BUY,
            entry_price=None,
            entry_zone_low=1999.5,
            entry_zone_high=2000.5,
        )
        result = OpenAIAdvisor(self.config, client(parsed)).evaluate(market_payload())
        self.assertEqual(result.decision.decision, TradeDecision.BUY)

    def test_sell_structured_response(self) -> None:
        result = OpenAIAdvisor(
            self.config, client(decision(TradeDecision.SELL))
        ).evaluate(market_payload())
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

    def test_cost_without_cache(self) -> None:
        usage = TokenUsage(input_tokens=1000)
        self.assertEqual(estimate_cost(usage, self.config), 0.002)

    def test_cost_with_cache_read(self) -> None:
        usage = TokenUsage(input_tokens=1000, cached_input_tokens=400)
        self.assertEqual(estimate_cost(usage, self.config), 0.00124)

    def test_cost_with_cache_write(self) -> None:
        usage = TokenUsage(input_tokens=1000, cache_write_tokens=400)
        self.assertEqual(estimate_cost(usage, self.config), 0.0022)

    def test_cost_with_mixed_input_categories(self) -> None:
        usage = TokenUsage(
            input_tokens=1000,
            cached_input_tokens=200,
            cache_write_tokens=300,
        )
        self.assertEqual(estimate_cost(usage, self.config), 0.00177)

    def test_malformed_input_details_are_safely_clamped(self) -> None:
        usage = TokenUsage(
            input_tokens=100,
            cached_input_tokens=200,
            cache_write_tokens=300,
        )
        self.assertEqual(estimate_cost(usage, self.config), 0.00025)

    def test_reasoning_tokens_are_not_double_charged(self) -> None:
        usage = TokenUsage(output_tokens=200, reasoning_tokens=150)
        self.assertEqual(estimate_cost(usage, self.config), 0.002)

    def test_sdk_usage_is_extracted_and_costed(self) -> None:
        usage = SimpleNamespace(
            input_tokens=1000,
            input_tokens_details=SimpleNamespace(
                cached_tokens=400, cache_write_tokens=100
            ),
            output_tokens=200,
            output_tokens_details=SimpleNamespace(reasoning_tokens=50),
        )
        result = OpenAIAdvisor(
            self.config, client(decision(TradeDecision.NO_TRADE), usage)
        ).evaluate({})
        self.assertEqual(
            result.usage,
            TokenUsage(
                input_tokens=1000,
                cached_input_tokens=400,
                cache_write_tokens=100,
                output_tokens=200,
                reasoning_tokens=50,
            ),
        )
        self.assertEqual(result.estimated_cost_usd, 0.00329)

    def test_absurd_but_geometric_buy_becomes_no_trade(self) -> None:
        parsed = decision(
            TradeDecision.BUY,
            entry_price=6000.0,
            stop_loss=5990.0,
            take_profit=6020.0,
        )
        result = OpenAIAdvisor(self.config, client(parsed)).evaluate(market_payload())
        self.assertEqual(result.decision.decision, TradeDecision.NO_TRADE)
        self.assertEqual(
            result.decision.invalidation_reason,
            "recommendation_detached_from_current_market",
        )

    def test_absurd_but_geometric_sell_becomes_no_trade(self) -> None:
        parsed = decision(
            TradeDecision.SELL,
            entry_price=500.0,
            stop_loss=510.0,
            take_profit=480.0,
        )
        result = OpenAIAdvisor(self.config, client(parsed)).evaluate(market_payload())
        self.assertEqual(result.decision.decision, TradeDecision.NO_TRADE)


if __name__ == "__main__":
    unittest.main()
