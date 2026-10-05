from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .analysis import calculate_metrics
from .config import Settings
from .mcp_client import MCPToolError, MT5ReadOnlyClient
from .models import (
    AccountSnapshot,
    Candle,
    PositionSnapshot,
    SymbolSpecification,
    Tick,
    XAUUSDMarketSnapshot,
)
from .timestamps import TimestampError, mcp_timestamp, parse_timestamp, seconds_between


LOGGER = logging.getLogger(__name__)


def _number(data: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = data.get(key, default)
    return float(default if value is None else value)


def _integer(data: dict[str, Any], key: str, default: int = 0) -> int:
    value = data.get(key, default)
    return int(default if value is None else value)


def _parse_server_time(value: str) -> datetime:
    try:
        return parse_timestamp(value)
    except TimestampError as exc:
        raise MCPToolError(f"Invalid trade-server time: {value}") from exc


def _ordered_history(
    history: list[Candle],
    server_time: datetime,
    duration: timedelta,
) -> tuple[list[Candle], list[Candle]]:
    try:
        parsed = [(parse_timestamp(candle.time), candle) for candle in history]
        for opening_time, _ in parsed:
            seconds_between(server_time, opening_time)
    except TimestampError as exc:
        raise MCPToolError("Candle timestamps cannot be compared safely") from exc
    parsed.sort(key=lambda pair: pair[0])
    ordered = [candle for _, candle in parsed]
    completed = [
        candle
        for opening_time, candle in parsed
        if seconds_between(server_time, opening_time) >= duration.total_seconds()
    ]
    return ordered, completed


def _gold_score(symbol: dict[str, Any]) -> int | None:
    name = str(symbol.get("symbol", "")).upper()
    base = str(symbol.get("currency_base", "")).upper()
    profit = str(symbol.get("currency_profit", "")).upper()
    description = str(symbol.get("description", "")).upper()

    is_xau_usd = base == "XAU" and profit == "USD"
    looks_like_gold_usd = "GOLD" in description and (
        profit == "USD" or "US DOLLAR" in description or "USD" in name
    )
    if not is_xau_usd and not looks_like_gold_usd and "XAUUSD" not in name:
        return None

    score = 0
    if name == "XAUUSD":
        score += 10_000
    elif name.startswith("XAUUSD"):
        score += 8_000
    elif "XAUUSD" in name:
        score += 6_000
    if is_xau_usd:
        score += 2_000
    if symbol.get("selected"):
        score += 500
    if symbol.get("trade_mode_name") not in (None, "disabled"):
        score += 100
    return score


def select_gold_symbol(symbols: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [(score, item) for item in symbols if (score := _gold_score(item)) is not None]
    if not candidates:
        return None
    candidates.sort(key=lambda pair: (-pair[0], str(pair[1].get("symbol", ""))))
    return candidates[0][1]


class XAUUSDCollector:
    def __init__(self, client: MT5ReadOnlyClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def collect(self) -> XAUUSDMarketSnapshot:
        symbol_data = await self._detect_symbol()
        symbol_name = str(symbol_data["symbol"])
        LOGGER.info("Detected broker gold symbol: %s", symbol_name)

        time_data = await self._client.call_tool("get_time_information", {})
        account_data = await self._client.call_tool("get_trading_account_info", {})
        positions_data = await self._client.call_tool(
            "get_trading_open_positions",
            {"symbol": symbol_name, "include_orders": False},
        )

        server_time_text = str(
            time_data.get("trade_server_last_known_time")
            or time_data.get("local_time")
            or time_data.get("utc_time")
            or ""
        )
        if not server_time_text:
            raise MCPToolError("get_time_information returned no usable server time")
        server_time = _parse_server_time(server_time_text)

        m1_duration = timedelta(minutes=1)
        m5_duration = timedelta(minutes=5)
        m1_required = self._settings.required_m1_completed_candles
        m5_required = self._settings.required_m5_completed_candles
        m1 = await self._fetch_candles(
            symbol_name,
            "M1",
            server_time,
            max(timedelta(hours=12), m1_duration * (m1_required * 2)),
            m1_duration,
            m1_required,
        )
        m5 = await self._fetch_candles(
            symbol_name,
            "M5",
            server_time,
            max(timedelta(days=3), m5_duration * (m5_required * 2)),
            m5_duration,
            m5_required,
        )
        tick_reference_time = server_time
        if symbol_data.get("update_time"):
            tick_reference_time = _parse_server_time(str(symbol_data["update_time"]))
        ticks = await self._fetch_ticks(symbol_name, tick_reference_time)

        # Refresh the quote after history collection so the snapshot uses the newest
        # read-only bid/ask available from Market Watch.
        quote_data = await self._exact_visible_symbol(symbol_name)
        if quote_data is None:
            raise MCPToolError(f"Detected symbol {symbol_name} is no longer visible in Market Watch")

        symbol = self._build_symbol(quote_data)
        account = self._build_account(account_data)
        positions = tuple(
            self._build_position(item)
            for item in positions_data.get("positions", [])
            if str(item.get("symbol", "")).upper() == symbol_name.upper()
        )
        metrics = calculate_metrics(
            bid=symbol.bid,
            ask=symbol.ask,
            point=symbol.point,
            digits=symbol.digits,
            m1_candles=m1,
            m5_candles=m5,
            average_range_bars=self._settings.average_range_bars,
            direction_bars=self._settings.direction_bars,
        )

        return XAUUSDMarketSnapshot(
            schema_version="1.0",
            captured_at_utc=datetime.now(timezone.utc).isoformat(),
            trade_server_time=server_time_text,
            symbol=symbol,
            account=account,
            positions=positions,
            m1_candles=tuple(m1),
            m5_candles=tuple(m5),
            recent_ticks=tuple(ticks),
            metrics=metrics,
        )

    async def _detect_symbol(self) -> dict[str, Any]:
        exact = await self._exact_visible_symbol("XAUUSD")
        if exact is not None and _gold_score(exact) is not None:
            return exact

        visible = await self._client.call_tool(
            "get_marketwatch_symbols",
            {"include_hidden": False, "limit": 100_000},
        )
        selected = select_gold_symbol(list(visible.get("symbols", [])))
        if selected is not None:
            return selected

        catalog = await self._client.call_tool(
            "get_marketwatch_symbols",
            {"include_hidden": True, "limit": 100_000},
        )
        hidden = select_gold_symbol(list(catalog.get("symbols", [])))
        if hidden is not None:
            raise MCPToolError(
                f"Gold symbol {hidden.get('symbol')} exists but is not visible in Market Watch; "
                "make it visible manually because this collector will not change terminal state"
            )
        raise MCPToolError("No XAU/USD gold symbol was found in the connected terminal")

    async def _exact_visible_symbol(self, symbol: str) -> dict[str, Any] | None:
        response = await self._client.call_tool(
            "get_marketwatch_symbols",
            {"symbol": symbol, "include_hidden": False, "limit": 10},
        )
        symbols = list(response.get("symbols", []))
        return symbols[0] if symbols else None

    async def _fetch_candles(
        self,
        symbol: str,
        period: str,
        server_time: datetime,
        initial_lookback: timedelta,
        duration: timedelta,
        required_completed: int,
    ) -> list[Candle]:
        request_limit = max(10_000, required_completed * 2 + 10)
        history = await self._request_history(
            symbol, period, server_time, initial_lookback, request_limit
        )
        _, completed = _ordered_history(history, server_time, duration)
        if len(completed) < required_completed:
            expanded = max(
                timedelta(days=7 if period == "M1" else 14),
                duration * (required_completed * 5),
            )
            history = await self._request_history(
                symbol, period, server_time, expanded, request_limit
            )
            _, completed = _ordered_history(history, server_time, duration)
        if len(completed) < required_completed:
            raise MCPToolError(
                f"Only {len(completed)} completed {period} candles were available for {symbol}; "
                f"{required_completed} are required by configuration"
            )

        # Snapshot history intentionally contains completed bars only. This keeps
        # defaults at 100 while guaranteeing that all configured lookbacks are usable.
        return completed[-required_completed:]

    async def _request_history(
        self,
        symbol: str,
        period: str,
        server_time: datetime,
        lookback: timedelta,
        limit: int,
    ) -> list[Candle]:
        response = await self._client.call_tool(
            "get_chart_history",
            {
                "symbol": symbol,
                "period": period,
                "datetime_from": mcp_timestamp(server_time - lookback),
                "datetime_to": mcp_timestamp(server_time + timedelta(minutes=1)),
                "limit": limit,
            },
        )
        return [self._build_candle(item) for item in response.get("history", [])]

    async def _fetch_ticks(self, symbol: str, server_time: datetime) -> list[Tick]:
        response = await self._client.call_tool(
            "get_chart_ticks_history",
            {
                "symbol": symbol,
                "datetime_from": mcp_timestamp(
                    server_time - timedelta(minutes=self._settings.tick_lookback_minutes)
                ),
                "datetime_to": mcp_timestamp(server_time + timedelta(minutes=1)),
                "limit": self._settings.tick_request_limit,
            },
        )
        ticks = [self._build_tick(item) for item in response.get("history", [])]
        try:
            parsed_ticks = [(parse_timestamp(tick.time), tick) for tick in ticks]
            for tick_time, _ in parsed_ticks:
                seconds_between(server_time, tick_time)
        except TimestampError as exc:
            raise MCPToolError("Tick timestamps cannot be compared safely") from exc
        parsed_ticks.sort(key=lambda pair: pair[0])
        ticks = [tick for _, tick in parsed_ticks]
        return ticks[-self._settings.tick_snapshot_limit :]

    @staticmethod
    def _build_candle(data: dict[str, Any]) -> Candle:
        return Candle(
            time=str(data["time"]),
            open=_number(data, "open"),
            high=_number(data, "high"),
            low=_number(data, "low"),
            close=_number(data, "close"),
            tick_volume=_integer(data, "tick_volume"),
            spread=_integer(data, "spread") if "spread" in data else None,
        )

    @staticmethod
    def _build_tick(data: dict[str, Any]) -> Tick:
        return Tick(
            time=str(data.get("time_ms") or data.get("time") or ""),
            bid=_number(data, "bid"),
            ask=_number(data, "ask"),
            last=_number(data, "last") if "last" in data else None,
            volume=_number(data, "volume") if "volume" in data else None,
        )

    @staticmethod
    def _build_symbol(data: dict[str, Any]) -> SymbolSpecification:
        bid = _number(data, "bid")
        ask = _number(data, "ask")
        point = _number(data, "point")
        if bid <= 0 or ask <= 0 or point <= 0:
            raise MCPToolError("Selected gold symbol has no usable bid, ask, or point size")
        return SymbolSpecification(
            symbol=str(data["symbol"]),
            description=str(data.get("description", "")),
            selected=bool(data.get("selected", False)),
            bid=bid,
            ask=ask,
            point=point,
            digits=_integer(data, "digits"),
            volume_min=_number(data, "volume_min"),
            volume_max=_number(data, "volume_max"),
            volume_step=_number(data, "volume_step"),
            contract_size=_number(data, "contract_size"),
            trade_stops_level=_integer(data, "trade_stops_level"),
            trade_freeze_level=_integer(data, "trade_freeze_level"),
            trade_mode=str(data.get("trade_mode_name", data.get("trade_mode", ""))),
            execution_mode=str(data.get("trade_execution_mode", data.get("trade_exemode", ""))),
            calculation_mode=str(data.get("trade_calculation_mode", data.get("calculation_mode", ""))),
            order_mode=_integer(data, "trade_order_mode"),
            filling_mode=_integer(data, "trade_filling_mode"),
            expiration_mode=_integer(data, "expiration_mode"),
            quote_time=str(data["update_time"]) if data.get("update_time") else None,
        )

    @staticmethod
    def _build_account(payload: dict[str, Any]) -> AccountSnapshot:
        data = payload.get("account", payload)
        return AccountSnapshot(
            server=str(data.get("server", "")),
            broker=str(data.get("broker", "")),
            login=str(data.get("login", "")),
            account_type=str(data.get("type", "")),
            currency=str(data.get("currency", "")),
            balance=_number(data, "balance"),
            equity=_number(data, "equity"),
            free_margin=_number(data, "margin_free"),
            used_margin=_number(data, "margin"),
            floating_profit=_number(data, "profit"),
        )

    @staticmethod
    def _build_position(data: dict[str, Any]) -> PositionSnapshot:
        return PositionSnapshot(
            position_id=str(data.get("position_id", "")),
            symbol=str(data.get("symbol", "")),
            side=str(data.get("action", "")),
            volume=_number(data, "volume"),
            price_open=_number(data, "price_open"),
            price_last=_number(data, "price_last"),
            stop_loss=_number(data, "stop_loss") if data.get("stop_loss") else None,
            take_profit=_number(data, "take_profit") if data.get("take_profit") else None,
            profit=_number(data, "profit"),
            create_time=str(data["create_time"]) if data.get("create_time") else None,
            comment=str(data.get("comment", "")),
        )
