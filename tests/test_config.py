import unittest

from xauusd_bot.config import MarketGateConfig, Settings


class HistoryRequirementTests(unittest.TestCase):
    def test_default_completed_history_matches_current_defaults(self) -> None:
        settings = Settings(mcp_url="http://127.0.0.1/mcp")
        self.assertEqual(settings.required_m1_completed_candles, 100)
        self.assertEqual(settings.required_m5_completed_candles, 100)

    def test_history_requirement_uses_all_configured_lookbacks(self) -> None:
        gate = MarketGateConfig(
            min_m1_candles=40,
            min_m5_candles=50,
            spike_lookback_bars=120,
            direction_m1_bars=130,
            direction_m5_bars=140,
        )
        settings = Settings(
            mcp_url="http://127.0.0.1/mcp",
            average_range_bars=150,
            direction_bars=160,
            market_gate=gate,
        )
        self.assertEqual(settings.required_m1_completed_candles, 160)
        self.assertEqual(settings.required_m5_completed_candles, 150)


if __name__ == "__main__":
    unittest.main()
