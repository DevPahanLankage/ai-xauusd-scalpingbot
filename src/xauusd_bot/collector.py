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


LOGGER = logging.getLogger(__name__)
REQUIRED_CANDLES = 100


def _number(data: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = data.get(key, default)
    return float(default if value is None else value)


def _integer(data: dict[str, Any], key: str, default: int = 0) -> int:
    value = data.get(key, default)
    return int(default if value is None else value)


def _parse_server_time(value: str) -> datetime:
    normalized = value.rstrip("Z")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise MCPToolError(f"Invalid trade-server time: {value}") from exc


def _server_timestamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


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

        m1 = await self._fetch_candles(symbol_name, "M1", server_time, timedelta(hours=12))
        m5 = await self._fetch_candles(symbol_name, "M5", server_time, timedelta(days=3))
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
    ) -> list[Candle]:
        history = await self._request_history(symbol, period, server_time, initial_lookback)
        if len(history) < REQUIRED_CANDLES:
            expanded = timedelta(days=7 if period == "M1" else 14)
            history = await self._request_history(symbol, period, server_time, expanded)
        if len(history) < REQUIRED_CANDLES:
            raise MCPToolError(
                f"Only {len(history)} {period} candles were available for {symbol}; "
                f"{REQUIRED_CANDLES} are required"
            )
        history.sort(key=lambda candle: candle.time)
        return history[-REQUIRED_CANDLES:]

    async def _request_history(
        self,
        symbol: str,
        period: str,
        server_time: datetime,
        lookback: timedelta,
    ) -> list[Candle]:
        response = await self._client.call_tool(
            "get_chart_history",
            {
                "symbol": symbol,
                "period": period,
                "datetime_from": _server_timestamp(server_time - lookback),
                "datetime_to": _server_timestamp(server_time + timedelta(minutes=1)),
                "limit": 10_000,
            },
        )
        return [self._build_candle(item) for item in response.get("history", [])]

    async def _fetch_ticks(self, symbol: str, server_time: datetime) -> list[Tick]:
        response = await self._client.call_tool(
            "get_chart_ticks_history",
            {
                "symbol": symbol,
                "datetime_from": _server_timestamp(
                    server_time - timedelta(minutes=self._settings.tick_lookback_minutes)
                ),
                "datetime_to": _server_timestamp(server_time + timedelta(minutes=1)),
                "limit": self._settings.tick_request_limit,
            },
        )
        ticks = [self._build_tick(item) for item in response.get("history", [])]
        ticks.sort(key=lambda tick: tick.time)
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
