import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from xauusd_bot.config import AIConfig, MarketGateConfig, Settings


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


class StatePathConfigurationTests(unittest.TestCase):
    def test_relative_state_path_uses_explicit_runtime_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"XAUUSD_AI_STATE_DB": ".state/custom.sqlite3"},
            clear=True,
        ):
            base = Path(temporary)
            config = AIConfig.from_environment(state_base_path=base)
        self.assertEqual(config.state_db_path, base / ".state" / "custom.sqlite3")

    def test_absolute_state_path_is_honored_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            absolute = Path(temporary) / "existing.sqlite3"
            with patch.dict(
                os.environ,
                {"XAUUSD_AI_STATE_DB": str(absolute)},
                clear=True,
            ):
                config = AIConfig.from_environment(
                    state_base_path=Path(temporary) / "different-base"
                )
        self.assertEqual(config.state_db_path, absolute)

    def test_developer_cli_keeps_relative_state_path_without_a_base(self) -> None:
        with patch.dict(
            os.environ,
            {"XAUUSD_AI_STATE_DB": ".state/xauusd_bot.sqlite3"},
            clear=True,
        ):
            config = AIConfig.from_environment()
        self.assertEqual(config.state_db_path, Path(".state/xauusd_bot.sqlite3"))


if __name__ == "__main__":
    unittest.main()
