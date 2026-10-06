from __future__ import annotations

import unittest

from xauusd_bot.advisory_service import AIAdvisoryOutcome
from xauusd_bot.ai_models import AdvisoryResult, TradeDecision
from xauusd_bot.config import MarketGateConfig
from xauusd_bot.market_gate import MarketGate
from xauusd_bot.output import advisory_to_human
from tests.test_advisor import decision
from tests.test_advisory_flow import safe_news
from tests.test_market_gate import _snapshot


def render(side: TradeDecision, **updates: object) -> str:
    snapshot = _snapshot()
    market = MarketGate(MarketGateConfig()).evaluate(snapshot)
    result = AdvisoryResult(
        decision=decision(side, **updates),
        status="success",
        usage=None,
        estimated_cost_usd=0.0,
        latency_ms=1.0,
    )
    outcome = AIAdvisoryOutcome(
        attempted=True,
        skip_reason=None,
        input_hash="test-hash",
        reservation=None,
        result=result,
    )
    return advisory_to_human(snapshot, market, safe_news(), outcome)


class AdvisoryHumanOutputTests(unittest.TestCase):
    def test_buy_exact_entry(self) -> None:
        output = render(TradeDecision.BUY)
        self.assertIn("Entry               : 2000.00", output)
        self.assertIn("Stop loss           : 1998.00", output)
        self.assertIn("Take profit         : 2004.00", output)
        self.assertIn("Risk/reward         : 2.0", output)

    def test_buy_entry_zone(self) -> None:
        output = render(
            TradeDecision.BUY,
            entry_price=None,
            entry_zone_low=1999.50,
            entry_zone_high=2000.50,
        )
        self.assertIn("Entry               : 1999.50 - 2000.50", output)

    def test_sell_entry_zone(self) -> None:
        output = render(
            TradeDecision.SELL,
            entry_price=None,
            entry_zone_low=1999.25,
            entry_zone_high=2000.25,
        )
        self.assertIn("Entry               : 1999.25 - 2000.25", output)
        self.assertIn("Stop loss           : 2002.00", output)
        self.assertIn("Take profit         : 1996.00", output)

    def test_no_trade_entry_and_geometry_are_na(self) -> None:
        output = render(TradeDecision.NO_TRADE)
        self.assertIn("Entry               : n/a", output)
        self.assertIn("Stop loss           : n/a", output)
        self.assertIn("Take profit         : n/a", output)
        self.assertIn("Risk/reward         : n/a", output)


if __name__ == "__main__":
    unittest.main()
