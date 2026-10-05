from __future__ import annotations

from collections.abc import Sequence

from .models import Candle, DerivedMetrics, Direction


def candle_range(candle: Candle) -> float:
    return candle.high - candle.low


def average_candle_range(candles: Sequence[Candle], bars: int) -> float:
    if not candles:
        raise ValueError("At least one candle is required")
    selected = candles[-min(bars, len(candles)) :]
    return sum(candle_range(candle) for candle in selected) / len(selected)


def short_term_direction(
    candles: Sequence[Candle],
    lookback_bars: int,
    average_range: float,
    flat_range_fraction: float = 0.10,
) -> tuple[Direction, float, float, int]:
    if len(candles) < 2:
        raise ValueError("At least two candles are required for direction")
    selected = candles[-min(lookback_bars, len(candles)) :]
    price_change = selected[-1].close - selected[0].close
    flat_threshold = average_range * flat_range_fraction
    direction: Direction
    if price_change > flat_threshold:
        direction = "UP"
    elif price_change < -flat_threshold:
        direction = "DOWN"
    else:
        direction = "FLAT"
    return direction, price_change, flat_threshold, len(selected)


def calculate_metrics(
    *,
    bid: float,
    ask: float,
    point: float,
    digits: int,
    m1_candles: Sequence[Candle],
    m5_candles: Sequence[Candle],
    average_range_bars: int,
    direction_bars: int,
) -> DerivedMetrics:
    if point <= 0:
        raise ValueError("Symbol point size must be greater than zero")
    if not m1_candles or not m5_candles:
        raise ValueError("M1 and M5 candles are required")

    m1_latest = candle_range(m1_candles[-1])
    m5_latest = candle_range(m5_candles[-1])
    m1_average = average_candle_range(m1_candles, average_range_bars)
    m5_average = average_candle_range(m5_candles, average_range_bars)
    direction, change, threshold, used_bars = short_term_direction(
        m1_candles,
        direction_bars,
        m1_average,
    )
    precision = max(digits + 3, 8)
    spread_price = ask - bid

    return DerivedMetrics(
        spread_price=round(spread_price, precision),
        spread_points=round(spread_price / point, 3),
        m1_latest_range=round(m1_latest, precision),
        m1_latest_range_points=round(m1_latest / point, 3),
        m5_latest_range=round(m5_latest, precision),
        m5_latest_range_points=round(m5_latest / point, 3),
        m1_average_range=round(m1_average, precision),
        m1_average_range_points=round(m1_average / point, 3),
        m5_average_range=round(m5_average, precision),
        m5_average_range_points=round(m5_average / point, 3),
        average_range_bars=min(average_range_bars, len(m1_candles), len(m5_candles)),
        short_term_direction=direction,
        direction_lookback_bars=used_bars,
        direction_price_change=round(change, precision),
        direction_flat_threshold=round(threshold, precision),
    )
