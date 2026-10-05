from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from datetime import datetime, timedelta

from .analysis import average_candle_range, candle_range, short_term_direction
from .config import MarketGateConfig
from .models import (
    Candle,
    Direction,
    MarketGateMetrics,
    MarketGateResult,
    XAUUSDMarketSnapshot,
)
from .timestamps import TimestampError, parse_timestamp, seconds_between


LOGGER = logging.getLogger(__name__)


def _completed_candles(
    candles: Sequence[Candle],
    server_time: datetime,
    duration: timedelta,
) -> list[Candle]:
    completed: list[Candle] = []
    for item in candles:
        opening_time = parse_timestamp(item.time)
        if seconds_between(server_time, opening_time) >= duration.total_seconds():
            completed.append(item)
    completed.sort(key=lambda item: parse_timestamp(item.time))
    return completed


def _age_seconds(server_time: datetime, value: str | None) -> float | None:
    if not value:
        return None
    try:
        return seconds_between(server_time, parse_timestamp(value))
    except TimestampError:
        return None


def _latest_tick_time(snapshot: XAUUSDMarketSnapshot, server_time: datetime) -> str | None:
    if not snapshot.recent_ticks:
        return None
    parsed: list[tuple[datetime, str]] = []
    for tick in snapshot.recent_ticks:
        value = parse_timestamp(tick.time)
        # This explicit compatibility check prevents max() from comparing mixed
        # naive and aware datetimes.
        seconds_between(server_time, value)
        parsed.append((value, tick.time))
    parsed.sort(key=lambda pair: pair[0])
    return parsed[-1][1]


def _ratio(numerator: float, denominator: float) -> float | None:
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator <= 0:
        return None
    return numerator / denominator


def _rounded(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else value


def _spike_metrics(
    candles: Sequence[Candle],
    point: float,
    lookback_bars: int,
) -> tuple[float | None, float | None, float | None]:
    if len(candles) < 2 or point <= 0:
        return None, None, None
    latest_range = candle_range(candles[-1])
    baseline_source = candles[-(lookback_bars + 1) : -1]
    if not baseline_source:
        return latest_range / point, None, None
    baseline = average_candle_range(baseline_source, lookback_bars)
    return latest_range / point, baseline / point, _ratio(latest_range, baseline)


def _direction(
    candles: Sequence[Candle],
    bars: int,
    baseline_range: float,
    flat_range_fraction: float,
) -> Direction:
    if len(candles) < 2 or baseline_range <= 0:
        return "FLAT"
    direction, _, _, _ = short_term_direction(
        candles,
        bars,
        baseline_range,
        flat_range_fraction,
    )
    return direction


class MarketGate:
    """Fail-closed deterministic eligibility gate for future AI evaluation."""

    def __init__(self, config: MarketGateConfig) -> None:
        self._config = config

    def evaluate(self, snapshot: XAUUSDMarketSnapshot) -> MarketGateResult:
        try:
            return self._evaluate(snapshot)
        except Exception as exc:  # A gate must never fail open.
            LOGGER.debug("Market gate failed closed due to %s", type(exc).__name__)
            return self._failed_closed(snapshot, type(exc).__name__)

    def _evaluate(self, snapshot: XAUUSDMarketSnapshot) -> MarketGateResult:
        config = self._config
        server_time = parse_timestamp(snapshot.trade_server_time)
        point = snapshot.symbol.point
        reasons: list[str] = []

        quote_age = _age_seconds(server_time, snapshot.symbol.quote_time)
        latest_tick_time = _latest_tick_time(snapshot, server_time)
        tick_age = _age_seconds(server_time, latest_tick_time)
        quote_fresh = self._is_fresh(quote_age, config.max_quote_age_seconds)
        tick_fresh = self._is_fresh(tick_age, config.max_tick_age_seconds)

        if not quote_fresh:
            reasons.append("stale_or_missing_quote")
        if not tick_fresh:
            reasons.append("stale_or_missing_tick")

        terminal_trade_active = (
            snapshot.symbol.selected
            and snapshot.symbol.trade_mode.lower() == "full"
            and snapshot.symbol.bid > 0
            and snapshot.symbol.ask > snapshot.symbol.bid
        )
        market_active = terminal_trade_active and quote_fresh and tick_fresh
        if not market_active:
            reasons.append("market_inactive")

        tick_history_sufficient = len(snapshot.recent_ticks) >= config.min_recent_ticks
        if not tick_history_sufficient:
            reasons.append("insufficient_recent_tick_history")

        completed_m1 = _completed_candles(snapshot.m1_candles, server_time, timedelta(minutes=1))
        completed_m5 = _completed_candles(snapshot.m5_candles, server_time, timedelta(minutes=5))
        completed_m1_time = completed_m1[-1].time if completed_m1 else None
        if completed_m1_time is None:
            reasons.append("no_completed_m1_candle")

        required_m1 = max(
            config.min_m1_candles,
            config.spike_lookback_bars + 1,
            config.direction_m1_bars,
        )
        required_m5 = max(
            config.min_m5_candles,
            config.spike_lookback_bars + 1,
            config.direction_m5_bars,
        )
        history_sufficient = len(completed_m1) >= required_m1 and len(completed_m5) >= required_m5
        if len(completed_m1) < required_m1:
            reasons.append("insufficient_m1_history")
        if len(completed_m5) < required_m5:
            reasons.append("insufficient_m5_history")

        m1_latest_points, m1_baseline_points, m1_spike_ratio = _spike_metrics(
            completed_m1, point, config.spike_lookback_bars
        )
        m5_latest_points, m5_baseline_points, m5_spike_ratio = _spike_metrics(
            completed_m5, point, config.spike_lookback_bars
        )
        volatility_acceptable = (
            m1_spike_ratio is not None
            and m5_spike_ratio is not None
            and m1_spike_ratio <= config.max_m1_spike_ratio
            and m5_spike_ratio <= config.max_m5_spike_ratio
        )
        if m1_spike_ratio is None or m1_spike_ratio > config.max_m1_spike_ratio:
            reasons.append("abnormal_or_unknown_m1_volatility")
        if m5_spike_ratio is None or m5_spike_ratio > config.max_m5_spike_ratio:
            reasons.append("abnormal_or_unknown_m5_volatility")

        spread_points = None
        if point > 0 and math.isfinite(snapshot.symbol.ask - snapshot.symbol.bid):
            spread_points = (snapshot.symbol.ask - snapshot.symbol.bid) / point
        spread_to_range = _ratio(
            spread_points if spread_points is not None else math.nan,
            m1_baseline_points if m1_baseline_points is not None else math.nan,
        )
        absolute_spread_ok = (
            spread_points is not None
            and spread_points >= 0
            and spread_points <= config.max_spread_points
        )
        relative_spread_ok = (
            spread_to_range is not None
            and spread_to_range <= config.max_spread_to_m1_range_ratio
        )
        spread_acceptable = absolute_spread_ok and relative_spread_ok
        if not absolute_spread_ok:
            reasons.append("spread_exceeds_absolute_limit")
        if not relative_spread_ok:
            reasons.append("spread_too_large_relative_to_m1_volatility")

        m1_baseline_price = (m1_baseline_points or 0.0) * point
        m5_baseline_price = (m5_baseline_points or 0.0) * point
        direction_m1 = _direction(
            completed_m1,
            config.direction_m1_bars,
            m1_baseline_price,
            config.direction_flat_range_fraction,
        )
        direction_m5 = _direction(
            completed_m5,
            config.direction_m5_bars,
            m5_baseline_price,
            config.direction_flat_range_fraction,
        )
        directions_aligned = direction_m1 == direction_m5 and direction_m1 in {"UP", "DOWN"}
        if config.require_directional_alignment and not directions_aligned:
            reasons.append("m1_m5_directions_not_aligned")

        symbol_key = snapshot.symbol.symbol.upper()
        existing_position = any(
            position.symbol.upper() == symbol_key for position in snapshot.positions
        )
        if config.reject_existing_position and existing_position:
            reasons.append("existing_xauusd_position")

        free_margin_sufficient = (
            math.isfinite(snapshot.account.free_margin)
            and snapshot.account.free_margin >= config.min_free_margin
        )
        if not free_margin_sufficient:
            reasons.append("insufficient_free_margin")

        unique_reasons = tuple(dict.fromkeys(reasons))
        return MarketGateResult(
            eligible_for_ai=not unique_reasons,
            market_active=market_active,
            quote_fresh=quote_fresh,
            tick_fresh=tick_fresh,
            spread_acceptable=spread_acceptable,
            volatility_acceptable=volatility_acceptable,
            history_sufficient=history_sufficient,
            tick_history_sufficient=tick_history_sufficient,
            free_margin_sufficient=free_margin_sufficient,
            direction_m1=direction_m1,
            direction_m5=direction_m5,
            directions_aligned=directions_aligned,
            existing_position=existing_position,
            completed_m1_time=completed_m1_time,
            rejection_reasons=unique_reasons,
            metrics=MarketGateMetrics(
                quote_age_seconds=_rounded(quote_age, 3),
                tick_age_seconds=_rounded(tick_age, 3),
                spread_points=_rounded(spread_points, 3),
                spread_to_m1_range_ratio=_rounded(spread_to_range),
                m1_latest_completed_range_points=_rounded(m1_latest_points, 3),
                m1_baseline_range_points=_rounded(m1_baseline_points, 3),
                m1_spike_ratio=_rounded(m1_spike_ratio),
                m5_latest_completed_range_points=_rounded(m5_latest_points, 3),
                m5_baseline_range_points=_rounded(m5_baseline_points, 3),
                m5_spike_ratio=_rounded(m5_spike_ratio),
                m1_candle_count=len(snapshot.m1_candles),
                m5_candle_count=len(snapshot.m5_candles),
                m1_completed_candle_count=len(completed_m1),
                m5_completed_candle_count=len(completed_m5),
                recent_tick_count=len(snapshot.recent_ticks),
                free_margin=snapshot.account.free_margin,
            ),
        )

    def _is_fresh(self, age: float | None, maximum_age: float) -> bool:
        return (
            age is not None
            and -self._config.max_future_clock_skew_seconds <= age <= maximum_age
        )

    @staticmethod
    def _failed_closed(
        snapshot: XAUUSDMarketSnapshot,
        error_type: str,
    ) -> MarketGateResult:
        return MarketGateResult(
            eligible_for_ai=False,
            market_active=False,
            quote_fresh=False,
            tick_fresh=False,
            spread_acceptable=False,
            volatility_acceptable=False,
            history_sufficient=False,
            tick_history_sufficient=False,
            free_margin_sufficient=False,
            direction_m1="FLAT",
            direction_m5="FLAT",
            directions_aligned=False,
            existing_position=bool(snapshot.positions),
            completed_m1_time=None,
            rejection_reasons=(f"market_gate_internal_error:{error_type}",),
            metrics=MarketGateMetrics(
                quote_age_seconds=None,
                tick_age_seconds=None,
                spread_points=None,
                spread_to_m1_range_ratio=None,
                m1_latest_completed_range_points=None,
                m1_baseline_range_points=None,
                m1_spike_ratio=None,
                m5_latest_completed_range_points=None,
                m5_baseline_range_points=None,
                m5_spike_ratio=None,
                m1_candle_count=len(snapshot.m1_candles),
                m5_candle_count=len(snapshot.m5_candles),
                m1_completed_candle_count=0,
                m5_completed_candle_count=0,
                recent_tick_count=len(snapshot.recent_ticks),
                free_margin=snapshot.account.free_margin,
            ),
        )
