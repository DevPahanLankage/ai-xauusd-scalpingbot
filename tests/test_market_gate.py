from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from xauusd_bot.analysis import calculate_metrics
from xauusd_bot.candidate_tracker import CandidateEvaluationTracker
from xauusd_bot.config import MarketGateConfig
from xauusd_bot.market_gate import MarketGate
from xauusd_bot.models import (
    AccountSnapshot,
    Candle,
    PositionSnapshot,
    SymbolSpecification,
    Tick,
    XAUUSDMarketSnapshot,
)


SERVER_TIME = datetime(2026, 1, 5, 12, 10, 30)


def _candles(
    *,
    count: int,
    period_minutes: int,
    last_open: datetime,
    step: float,
    candle_range: float,
) -> tuple[Candle, ...]:
    start = last_open - timedelta(minutes=period_minutes * (count - 1))
    result = []
    for index in range(count):
        opening_time = start + timedelta(minutes=period_minutes * index)
        close = 2000.0 + index * step
        result.append(
            Candle(
                time=opening_time.isoformat(),
                open=close - step / 2,
                high=close + candle_range / 2,
                low=close - candle_range / 2,
                close=close,
                tick_volume=100,
            )
        )
    return tuple(result)


def _snapshot() -> XAUUSDMarketSnapshot:
    m1 = _candles(
        count=100,
        period_minutes=1,
        last_open=datetime(2026, 1, 5, 12, 9),
        step=0.10,
        candle_range=1.0,
    )
    m5 = _candles(
        count=100,
        period_minutes=5,
        last_open=datetime(2026, 1, 5, 12, 5),
        step=0.20,
        candle_range=2.0,
    )
    ticks = tuple(
        Tick(
            time=(SERVER_TIME - timedelta(seconds=30 - index)).isoformat(),
            bid=2009.90,
            ask=2010.10,
        )
        for index in range(30)
    )
    symbol = SymbolSpecification(
        symbol="XAUUSD",
        description="Gold vs US Dollar",
        selected=True,
        bid=2009.90,
        ask=2010.10,
        point=0.01,
        digits=2,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        contract_size=100.0,
        trade_stops_level=0,
        trade_freeze_level=0,
        trade_mode="full",
        execution_mode="market",
        calculation_mode="cfd leverage",
        order_mode=127,
        filling_mode=1,
        expiration_mode=15,
        quote_time=(SERVER_TIME - timedelta(seconds=5)).isoformat(),
    )
    account = AccountSnapshot(
        server="Demo",
        broker="Broker",
        login="1",
        account_type="demo",
        currency="USD",
        balance=1000.0,
        equity=1000.0,
        free_margin=1000.0,
        used_margin=0.0,
        floating_profit=0.0,
    )
    metrics = calculate_metrics(
        bid=symbol.bid,
        ask=symbol.ask,
        point=symbol.point,
        digits=symbol.digits,
        m1_candles=m1,
        m5_candles=m5,
        average_range_bars=20,
        direction_bars=5,
    )
    return XAUUSDMarketSnapshot(
        schema_version="1.0",
        captured_at_utc="2026-01-05T12:10:30+00:00",
        trade_server_time=SERVER_TIME.isoformat(),
        symbol=symbol,
        account=account,
        positions=(),
        m1_candles=m1,
        m5_candles=m5,
        recent_ticks=ticks,
        metrics=metrics,
    )


def _position() -> PositionSnapshot:
    return PositionSnapshot(
        position_id="123",
        symbol="XAUUSD",
        side="buy",
        volume=0.01,
        price_open=2000.0,
        price_last=2010.0,
        stop_loss=None,
        take_profit=None,
        profit=10.0,
        create_time="2026-01-05T11:00:00",
        comment="",
    )


def _timestamp_style(snapshot: XAUUSDMarketSnapshot, style: str) -> XAUUSDMarketSnapshot:
    def convert(value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if style == "z":
            return f"{parsed.isoformat()}Z"
        if style == "offset":
            offset = timezone(timedelta(hours=5, minutes=30))
            return parsed.replace(tzinfo=offset).isoformat()
        raise ValueError(style)

    return replace(
        snapshot,
        trade_server_time=convert(snapshot.trade_server_time),
        symbol=replace(snapshot.symbol, quote_time=convert(snapshot.symbol.quote_time or "")),
        m1_candles=tuple(replace(item, time=convert(item.time)) for item in snapshot.m1_candles),
        m5_candles=tuple(replace(item, time=convert(item.time)) for item in snapshot.m5_candles),
        recent_ticks=tuple(replace(item, time=convert(item.time)) for item in snapshot.recent_ticks),
    )


class MarketGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = MarketGateConfig()

    def test_fresh_active_market_is_eligible(self) -> None:
        result = MarketGate(self.config).evaluate(_snapshot())
        self.assertTrue(result.eligible_for_ai)
        self.assertTrue(result.market_active)
        self.assertEqual(result.rejection_reasons, ())

    def test_stale_quote_fails_closed(self) -> None:
        snapshot = _snapshot()
        stale_symbol = replace(
            snapshot.symbol,
            quote_time=(SERVER_TIME - timedelta(minutes=2)).isoformat(),
        )
        result = MarketGate(self.config).evaluate(replace(snapshot, symbol=stale_symbol))
        self.assertFalse(result.eligible_for_ai)
        self.assertFalse(result.quote_fresh)
        self.assertIn("stale_or_missing_quote", result.rejection_reasons)

    def test_excessive_spread_is_rejected(self) -> None:
        snapshot = _snapshot()
        wide_symbol = replace(snapshot.symbol, ask=snapshot.symbol.bid + 1.0)
        result = MarketGate(self.config).evaluate(replace(snapshot, symbol=wide_symbol))
        self.assertFalse(result.eligible_for_ai)
        self.assertFalse(result.spread_acceptable)
        self.assertIn("spread_exceeds_absolute_limit", result.rejection_reasons)

    def test_abnormal_volatility_spike_is_rejected(self) -> None:
        snapshot = _snapshot()
        latest = snapshot.m1_candles[-1]
        spike = replace(latest, high=latest.close + 3.0, low=latest.close - 3.0)
        result = MarketGate(self.config).evaluate(
            replace(snapshot, m1_candles=(*snapshot.m1_candles[:-1], spike))
        )
        self.assertFalse(result.eligible_for_ai)
        self.assertFalse(result.volatility_acceptable)
        self.assertIn("abnormal_or_unknown_m1_volatility", result.rejection_reasons)

    def test_missing_candle_history_is_rejected(self) -> None:
        snapshot = _snapshot()
        result = MarketGate(self.config).evaluate(
            replace(snapshot, m1_candles=snapshot.m1_candles[-50:])
        )
        self.assertFalse(result.eligible_for_ai)
        self.assertFalse(result.history_sufficient)
        self.assertIn("insufficient_m1_history", result.rejection_reasons)

    def test_insufficient_tick_history_is_rejected(self) -> None:
        snapshot = _snapshot()
        result = MarketGate(self.config).evaluate(
            replace(snapshot, recent_ticks=snapshot.recent_ticks[-5:])
        )
        self.assertFalse(result.eligible_for_ai)
        self.assertFalse(result.tick_history_sufficient)
        self.assertIn("insufficient_recent_tick_history", result.rejection_reasons)

    def test_m1_m5_directional_misalignment_is_rejected(self) -> None:
        snapshot = _snapshot()
        reversed_m5 = tuple(
            replace(item, close=2200.0 - index * 0.20, open=2200.1 - index * 0.20)
            for index, item in enumerate(snapshot.m5_candles)
        )
        result = MarketGate(self.config).evaluate(replace(snapshot, m5_candles=reversed_m5))
        self.assertFalse(result.eligible_for_ai)
        self.assertFalse(result.directions_aligned)
        self.assertIn("m1_m5_directions_not_aligned", result.rejection_reasons)

    def test_existing_position_is_rejected(self) -> None:
        snapshot = replace(_snapshot(), positions=(_position(),))
        result = MarketGate(self.config).evaluate(snapshot)
        self.assertFalse(result.eligible_for_ai)
        self.assertTrue(result.existing_position)
        self.assertIn("existing_xauusd_position", result.rejection_reasons)

    def test_insufficient_free_margin_is_rejected(self) -> None:
        snapshot = _snapshot()
        account = replace(snapshot.account, free_margin=10.0)
        result = MarketGate(self.config).evaluate(replace(snapshot, account=account))
        self.assertFalse(result.eligible_for_ai)
        self.assertFalse(result.free_margin_sufficient)
        self.assertIn("insufficient_free_margin", result.rejection_reasons)

    def test_rejected_candle_can_become_eligible_then_be_explicitly_reserved(self) -> None:
        gate = MarketGate(self.config)
        tracker = CandidateEvaluationTracker()
        snapshot = _snapshot()
        wide_spread = replace(snapshot.symbol, ask=snapshot.symbol.bid + 1.0)

        rejected = gate.evaluate(replace(snapshot, symbol=wide_spread))
        rejected_reservation = tracker.reserve_for_ai(snapshot.symbol.symbol, rejected)
        consumed_after_rejection = tracker.is_consumed(
            snapshot.symbol.symbol, rejected.completed_m1_time or ""
        )
        improved = gate.evaluate(snapshot)
        first_reservation = tracker.reserve_for_ai(snapshot.symbol.symbol, improved)
        second_reservation = tracker.reserve_for_ai(snapshot.symbol.symbol, improved)

        self.assertFalse(rejected.eligible_for_ai)
        self.assertFalse(rejected_reservation.reserved_for_ai)
        self.assertFalse(consumed_after_rejection)
        self.assertTrue(improved.eligible_for_ai)
        self.assertTrue(first_reservation.reserved_for_ai)
        self.assertFalse(second_reservation.reserved_for_ai)
        self.assertTrue(second_reservation.already_consumed)
        self.assertEqual(second_reservation.reason, "completed_m1_already_consumed")

    def test_naive_mt5_timestamps_are_supported(self) -> None:
        result = MarketGate(self.config).evaluate(_snapshot())
        self.assertTrue(result.eligible_for_ai)

    def test_utc_z_timestamps_are_supported(self) -> None:
        result = MarketGate(self.config).evaluate(_timestamp_style(_snapshot(), "z"))
        self.assertTrue(result.eligible_for_ai)

    def test_offset_aware_timestamps_are_supported(self) -> None:
        result = MarketGate(self.config).evaluate(_timestamp_style(_snapshot(), "offset"))
        self.assertTrue(result.eligible_for_ai)

    def test_future_timestamp_within_skew_is_fresh(self) -> None:
        snapshot = _snapshot()
        future = (SERVER_TIME + timedelta(seconds=1)).isoformat()
        ticks = (*snapshot.recent_ticks[:-1], replace(snapshot.recent_ticks[-1], time=future))
        result = MarketGate(self.config).evaluate(
            replace(snapshot, symbol=replace(snapshot.symbol, quote_time=future), recent_ticks=ticks)
        )
        self.assertTrue(result.quote_fresh)
        self.assertTrue(result.tick_fresh)
        self.assertTrue(result.eligible_for_ai)

    def test_future_timestamp_beyond_skew_fails_closed(self) -> None:
        snapshot = _snapshot()
        future = (SERVER_TIME + timedelta(seconds=3)).isoformat()
        result = MarketGate(self.config).evaluate(
            replace(snapshot, symbol=replace(snapshot.symbol, quote_time=future))
        )
        self.assertFalse(result.quote_fresh)
        self.assertFalse(result.eligible_for_ai)
        self.assertIn("stale_or_missing_quote", result.rejection_reasons)

    def test_malformed_timestamp_fails_closed(self) -> None:
        snapshot = _snapshot()
        result = MarketGate(self.config).evaluate(
            replace(snapshot, symbol=replace(snapshot.symbol, quote_time="not-a-timestamp"))
        )
        self.assertFalse(result.quote_fresh)
        self.assertFalse(result.eligible_for_ai)

    def test_mixed_naive_and_aware_timestamps_fail_closed(self) -> None:
        snapshot = _snapshot()
        aware_quote = f"{snapshot.symbol.quote_time}Z"
        result = MarketGate(self.config).evaluate(
            replace(snapshot, symbol=replace(snapshot.symbol, quote_time=aware_quote))
        )
        self.assertFalse(result.quote_fresh)
        self.assertFalse(result.eligible_for_ai)

    def test_malformed_server_timestamp_fails_closed(self) -> None:
        result = MarketGate(self.config).evaluate(
            replace(_snapshot(), trade_server_time="malformed")
        )
        self.assertFalse(result.eligible_for_ai)
        self.assertTrue(result.rejection_reasons[0].startswith("market_gate_internal_error:"))


if __name__ == "__main__":
    unittest.main()
