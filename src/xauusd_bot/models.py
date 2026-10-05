from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


Direction = Literal["UP", "DOWN", "FLAT"]


@dataclass(frozen=True, slots=True)
class Candle:
    time: str
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread: int | None = None


@dataclass(frozen=True, slots=True)
class Tick:
    time: str
    bid: float
    ask: float
    last: float | None = None
    volume: float | None = None


@dataclass(frozen=True, slots=True)
class SymbolSpecification:
    symbol: str
    description: str
    selected: bool
    bid: float
    ask: float
    point: float
    digits: int
    volume_min: float
    volume_max: float
    volume_step: float
    contract_size: float
    trade_stops_level: int
    trade_freeze_level: int
    trade_mode: str
    execution_mode: str
    calculation_mode: str
    order_mode: int
    filling_mode: int
    expiration_mode: int
    quote_time: str | None


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    server: str
    broker: str
    login: str
    account_type: str
    currency: str
    balance: float
    equity: float
    free_margin: float
    used_margin: float
    floating_profit: float


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    position_id: str
    symbol: str
    side: str
    volume: float
    price_open: float
    price_last: float
    stop_loss: float | None
    take_profit: float | None
    profit: float
    create_time: str | None
    comment: str


@dataclass(frozen=True, slots=True)
class DerivedMetrics:
    spread_price: float
    spread_points: float
    m1_latest_range: float
    m1_latest_range_points: float
    m5_latest_range: float
    m5_latest_range_points: float
    m1_average_range: float
    m1_average_range_points: float
    m5_average_range: float
    m5_average_range_points: float
    average_range_bars: int
    short_term_direction: Direction
    direction_lookback_bars: int
    direction_price_change: float
    direction_flat_threshold: float


@dataclass(frozen=True, slots=True)
class XAUUSDMarketSnapshot:
    schema_version: str
    captured_at_utc: str
    trade_server_time: str
    symbol: SymbolSpecification
    account: AccountSnapshot
    positions: tuple[PositionSnapshot, ...]
    m1_candles: tuple[Candle, ...]
    m5_candles: tuple[Candle, ...]
    recent_ticks: tuple[Tick, ...]
    metrics: DerivedMetrics

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
@dataclass(frozen=True, slots=True)
class MarketGateMetrics:
    quote_age_seconds: float | None
    tick_age_seconds: float | None
    spread_points: float | None
    spread_to_m1_range_ratio: float | None
    m1_latest_completed_range_points: float | None
    m1_baseline_range_points: float | None
    m1_spike_ratio: float | None
    m5_latest_completed_range_points: float | None
    m5_baseline_range_points: float | None
    m5_spike_ratio: float | None
    m1_candle_count: int
    m5_candle_count: int
    recent_tick_count: int
    free_margin: float


@dataclass(frozen=True, slots=True)
class MarketGateResult:
    eligible_for_ai: bool
    market_active: bool
    quote_fresh: bool
    tick_fresh: bool
    spread_acceptable: bool
    volatility_acceptable: bool
    history_sufficient: bool
    tick_history_sufficient: bool
    free_margin_sufficient: bool
    direction_m1: Direction
    direction_m5: Direction
    directions_aligned: bool
    existing_position: bool
    duplicate_completed_m1: bool
    completed_m1_time: str | None
    rejection_reasons: tuple[str, ...]
    metrics: MarketGateMetrics

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
