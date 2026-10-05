from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _non_negative_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < 0:
        raise ConfigurationError(f"{name} must be zero or greater")
    return value


def _non_negative_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < 0:
        raise ConfigurationError(f"{name} must be zero or greater")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class MarketGateConfig:
    """All deterministic candidate and safety thresholds in one structure."""

    max_quote_age_seconds: float = 15.0
    max_tick_age_seconds: float = 15.0
    max_future_clock_skew_seconds: float = 2.0
    max_spread_points: float = 50.0
    max_spread_to_m1_range_ratio: float = 0.35
    min_m1_candles: int = 100
    min_m5_candles: int = 100
    min_recent_ticks: int = 20
    spike_lookback_bars: int = 20
    max_m1_spike_ratio: float = 3.0
    max_m5_spike_ratio: float = 3.0
    direction_m1_bars: int = 5
    direction_m5_bars: int = 5
    direction_flat_range_fraction: float = 0.10
    require_directional_alignment: bool = True
    reject_existing_position: bool = True
    min_free_margin: float = 50.0

    @classmethod
    def from_environment(cls) -> "MarketGateConfig":
        return cls(
            max_quote_age_seconds=_positive_float("XAUUSD_GATE_MAX_QUOTE_AGE_SECONDS", 15.0),
            max_tick_age_seconds=_positive_float("XAUUSD_GATE_MAX_TICK_AGE_SECONDS", 15.0),
            max_future_clock_skew_seconds=_non_negative_float(
                "XAUUSD_GATE_MAX_FUTURE_CLOCK_SKEW_SECONDS", 2.0
            ),
            max_spread_points=_positive_float("XAUUSD_GATE_MAX_SPREAD_POINTS", 50.0),
            max_spread_to_m1_range_ratio=_positive_float(
                "XAUUSD_GATE_MAX_SPREAD_TO_M1_RANGE_RATIO", 0.35
            ),
            min_m1_candles=_positive_int("XAUUSD_GATE_MIN_M1_CANDLES", 100),
            min_m5_candles=_positive_int("XAUUSD_GATE_MIN_M5_CANDLES", 100),
            min_recent_ticks=_positive_int("XAUUSD_GATE_MIN_RECENT_TICKS", 20),
            spike_lookback_bars=_positive_int("XAUUSD_GATE_SPIKE_LOOKBACK_BARS", 20),
            max_m1_spike_ratio=_positive_float("XAUUSD_GATE_MAX_M1_SPIKE_RATIO", 3.0),
            max_m5_spike_ratio=_positive_float("XAUUSD_GATE_MAX_M5_SPIKE_RATIO", 3.0),
            direction_m1_bars=_positive_int("XAUUSD_GATE_DIRECTION_M1_BARS", 5),
            direction_m5_bars=_positive_int("XAUUSD_GATE_DIRECTION_M5_BARS", 5),
            direction_flat_range_fraction=_non_negative_float(
                "XAUUSD_GATE_DIRECTION_FLAT_RANGE_FRACTION", 0.10
            ),
            require_directional_alignment=_boolean(
                "XAUUSD_GATE_REQUIRE_DIRECTIONAL_ALIGNMENT", True
            ),
            reject_existing_position=_boolean(
                "XAUUSD_GATE_REJECT_EXISTING_POSITION", True
            ),
            min_free_margin=_non_negative_float("XAUUSD_GATE_MIN_FREE_MARGIN", 50.0),
        )


@dataclass(frozen=True, slots=True)
class NewsGateConfig:
    """Conservative USD economic-calendar safety configuration."""

    currency: str = "USD"
    blackout_before_minutes: int = 30
    blackout_after_minutes: int = 30
    lookback_minutes: int = 120
    lookahead_minutes: int = 1_440
    request_limit: int = 1_000

    @classmethod
    def from_environment(cls) -> "NewsGateConfig":
        before = _non_negative_int("XAUUSD_NEWS_BLACKOUT_BEFORE_MINUTES", 30)
        after = _non_negative_int("XAUUSD_NEWS_BLACKOUT_AFTER_MINUTES", 30)
        lookback = _positive_int("XAUUSD_NEWS_LOOKBACK_MINUTES", 120)
        lookahead = _positive_int("XAUUSD_NEWS_LOOKAHEAD_MINUTES", 1_440)
        if lookback < after:
            raise ConfigurationError(
                "XAUUSD_NEWS_LOOKBACK_MINUTES must cover the post-event blackout"
            )
        if lookahead < before:
            raise ConfigurationError(
                "XAUUSD_NEWS_LOOKAHEAD_MINUTES must cover the pre-event blackout"
            )
        return cls(
            blackout_before_minutes=before,
            blackout_after_minutes=after,
            lookback_minutes=lookback,
            lookahead_minutes=lookahead,
            request_limit=_positive_int("XAUUSD_NEWS_REQUEST_LIMIT", 1_000),
        )


@dataclass(frozen=True, slots=True)
class AIConfig:
    """OpenAI advisory, persistence, payload, and cost-control settings."""

    api_key: str | None = field(default=None, repr=False)
    model: str = "gpt-6.1-sol"
    reasoning_effort: str = "low"
    timeout_seconds: float = 60.0
    max_output_tokens: int = 1_200
    payload_m1_candles: int = 30
    payload_m5_candles: int = 20
    max_calls_per_day: int = 30
    daily_spend_cap_usd: float = 0.65
    weekly_spend_cap_usd: float = 5.00
    input_usd_per_million: float = 2.00
    cached_input_usd_per_million: float = 0.10
    output_usd_per_million: float = 10.00
    state_db_path: Path = Path(".state/xauusd_bot.sqlite3")

    @classmethod
    def from_environment(cls) -> "AIConfig":
        effort = os.getenv("OPENAI_REASONING_EFFORT", "low").strip().lower()
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ConfigurationError(
                "OPENAI_REASONING_EFFORT must be low, medium, high, xhigh, or max"
            )
        state_path = os.getenv(
            "XAUUSD_AI_STATE_DB", ".state/xauusd_bot.sqlite3"
        ).strip()
        if not state_path:
            raise ConfigurationError("XAUUSD_AI_STATE_DB must not be empty")
        return cls(
            api_key=os.getenv("OPENAI_API_KEY", "").strip() or None,
            model=os.getenv("OPENAI_MODEL", "gpt-6.1-sol").strip() or "gpt-6.1-sol",
            reasoning_effort=effort,
            timeout_seconds=_positive_float("OPENAI_TIMEOUT_SECONDS", 60.0),
            max_output_tokens=_positive_int("OPENAI_MAX_OUTPUT_TOKENS", 1_200),
            payload_m1_candles=_positive_int("OPENAI_PAYLOAD_M1_CANDLES", 30),
            payload_m5_candles=_positive_int("OPENAI_PAYLOAD_M5_CANDLES", 20),
            max_calls_per_day=_positive_int("OPENAI_MAX_CALLS_PER_DAY", 30),
            daily_spend_cap_usd=_positive_float("OPENAI_DAILY_SPEND_CAP_USD", 0.65),
            weekly_spend_cap_usd=_positive_float("OPENAI_WEEKLY_SPEND_CAP_USD", 5.00),
            input_usd_per_million=_non_negative_float(
                "OPENAI_INPUT_USD_PER_MILLION", 2.00
            ),
            cached_input_usd_per_million=_non_negative_float(
                "OPENAI_CACHED_INPUT_USD_PER_MILLION", 0.10
            ),
            output_usd_per_million=_non_negative_float(
                "OPENAI_OUTPUT_USD_PER_MILLION", 10.00
            ),
            state_db_path=Path(state_path),
        )


@dataclass(frozen=True, slots=True)
class Settings:
    mcp_url: str
    mcp_token: str | None = field(default=None, repr=False)
    timeout_seconds: float = 30.0
    tick_lookback_minutes: int = 10
    tick_request_limit: int = 10_000
    tick_snapshot_limit: int = 200
    average_range_bars: int = 20
    direction_bars: int = 5
    market_gate: MarketGateConfig = field(default_factory=MarketGateConfig)
    news_gate: NewsGateConfig = field(default_factory=NewsGateConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    log_level: str = "INFO"

    @property
    def required_m1_completed_candles(self) -> int:
        return max(
            self.market_gate.min_m1_candles,
            self.market_gate.spike_lookback_bars + 1,
            self.market_gate.direction_m1_bars,
            self.average_range_bars,
            self.direction_bars,
        )

    @property
    def required_m5_completed_candles(self) -> int:
        return max(
            self.market_gate.min_m5_candles,
            self.market_gate.spike_lookback_bars + 1,
            self.market_gate.direction_m5_bars,
            self.average_range_bars,
        )

    @classmethod
    def from_environment(cls) -> "Settings":
        url = os.getenv("MT5_MCP_URL", "").strip()
        if not url:
            raise ConfigurationError("MT5_MCP_URL is required")
        if not url.startswith(("http://", "https://")):
            raise ConfigurationError("MT5_MCP_URL must be an HTTP(S) URL")

        token = os.getenv("MT5_MCP_TOKEN", "").strip() or None
        market_gate = MarketGateConfig.from_environment()
        news_gate = NewsGateConfig.from_environment()
        ai = AIConfig.from_environment()
        tick_snapshot_limit = _positive_int("MT5_TICK_SNAPSHOT_LIMIT", 200)
        if tick_snapshot_limit < market_gate.min_recent_ticks:
            raise ConfigurationError(
                "MT5_TICK_SNAPSHOT_LIMIT must be at least XAUUSD_GATE_MIN_RECENT_TICKS"
            )
        return cls(
            mcp_url=url,
            mcp_token=token,
            timeout_seconds=_positive_float("MT5_MCP_TIMEOUT_SECONDS", 30.0),
            tick_lookback_minutes=_positive_int("MT5_TICK_LOOKBACK_MINUTES", 10),
            tick_request_limit=_positive_int("MT5_TICK_REQUEST_LIMIT", 10_000),
            tick_snapshot_limit=tick_snapshot_limit,
            average_range_bars=_positive_int("MT5_AVERAGE_RANGE_BARS", 20),
            direction_bars=_positive_int("MT5_DIRECTION_BARS", 5),
            market_gate=market_gate,
            news_gate=news_gate,
            ai=ai,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )
