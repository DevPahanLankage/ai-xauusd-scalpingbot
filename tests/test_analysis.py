import unittest

from xauusd_bot.analysis import calculate_metrics
from xauusd_bot.models import Candle


def candle(index: int, close: float, high: float, low: float) -> Candle:
    return Candle(
        time=f"2026-01-01T00:{index:02d}:00",
        open=close,
        high=high,
        low=low,
        close=close,
        tick_volume=10,
    )


class CalculateMetricsTests(unittest.TestCase):
    def test_calculates_ranges_spread_and_up_direction(self) -> None:
        m1 = [candle(i, 100.0 + i * 0.10, 100.5 + i * 0.10, 99.5 + i * 0.10) for i in range(10)]
        m5 = [candle(i, 100.0 + i * 0.20, 101.0 + i * 0.20, 99.0 + i * 0.20) for i in range(10)]

        metrics = calculate_metrics(
            bid=101.00,
            ask=101.05,
            point=0.01,
            digits=2,
            m1_candles=m1,
            m5_candles=m5,
            average_range_bars=5,
            direction_bars=5,
        )

        self.assertEqual(metrics.spread_points, 5.0)
        self.assertEqual(metrics.m1_latest_range_points, 100.0)
        self.assertEqual(metrics.m5_latest_range_points, 200.0)
        self.assertEqual(metrics.short_term_direction, "UP")
        self.assertAlmostEqual(metrics.direction_price_change, 0.4)

    def test_classifies_small_move_as_flat(self) -> None:
        m1 = [candle(i, 100.0, 100.5, 99.5) for i in range(5)]
        metrics = calculate_metrics(
            bid=100.0,
            ask=100.01,
            point=0.01,
            digits=2,
            m1_candles=m1,
            m5_candles=m1,
            average_range_bars=5,
            direction_bars=5,
        )
        self.assertEqual(metrics.short_term_direction, "FLAT")


if __name__ == "__main__":
    unittest.main()
