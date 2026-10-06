from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .application_state import (
    EventFeed,
    MeaningfulEventTracker,
    build_application_state,
    disconnected_state,
)
from .collector import XAUUSDCollector
from .config import Settings
from .economic_calendar import EconomicCalendarCollector, EconomicCalendarGate
from .market_gate import MarketGate
from .mcp_client import MT5ReadOnlyClient
from .preview import AIPreviewService
from .state_store import PersistentStateUnavailableError, SQLiteStateStore
from .timestamps import TimestampError, parse_timestamp, seconds_between


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    quote_refresh_seconds: float = 5.0
    account_refresh_seconds: float = 15.0
    news_refresh_seconds: float = 60.0
    reconnect_seconds: float = 5.0


class StateHub:
    """Single in-memory state source consumed by CLI and browser clients."""

    def __init__(self, initial: dict[str, Any]) -> None:
        initial["revision"] = 0
        self._state = initial
        self._revision = 0
        self._condition = asyncio.Condition()

    async def publish(self, state: dict[str, Any]) -> None:
        async with self._condition:
            self._revision += 1
            state["revision"] = self._revision
            self._state = state
            self._condition.notify_all()

    async def snapshot(self) -> tuple[int, dict[str, Any]]:
        async with self._condition:
            return self._revision, self._state

    async def wait_after(self, revision: int) -> tuple[int, dict[str, Any]]:
        async with self._condition:
            await self._condition.wait_for(lambda: self._revision > revision)
            return self._revision, self._state


class MonitoringService:
    def __init__(
        self,
        settings: Settings,
        *,
        mode: str,
        config: MonitorConfig | None = None,
    ) -> None:
        self.settings = settings
        self.mode = mode
        self.config = config or MonitorConfig()
        self.started_at = datetime.now(timezone.utc)
        self.events = EventFeed(limit=100)
        self.tracker = MeaningfulEventTracker(self.events)
        self.hub = StateHub(
            disconnected_state(
                mode=mode,
                started_at=self.started_at,
                message="Waiting for MT5 Terminal MCP",
                events=self.events,
            )
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self._connected_session(stop_event)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                kind = type(exc).__name__
                self.events.add(
                    "ERROR", f"MT5 connection unavailable ({kind})", key="connection_error"
                )
                state = disconnected_state(
                    mode=self.mode,
                    started_at=self.started_at,
                    message=f"MT5 connection unavailable ({kind})",
                    events=self.events,
                )
                self.tracker.update(state)
                state["events"] = self.events.to_list()
                await self.hub.publish(state)
                LOGGER.warning("Monitoring connection failed (%s)", kind)
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self.config.reconnect_seconds
                    )
                except TimeoutError:
                    pass

    async def _connected_session(self, stop_event: asyncio.Event) -> None:
        news_engine = EconomicCalendarGate(self.settings.news_gate)
        async with MT5ReadOnlyClient(self.settings) as client:
            collector = XAUUSDCollector(client, self.settings)
            snapshot = await collector.collect()
            news = await self._news(client, snapshot.trade_server_time, news_engine)
            last_account_refresh = time.monotonic()
            last_news_refresh = time.monotonic()
            self.events.add("CONNECTED", "MT5 Terminal MCP connected", key="connection")
            await self._publish(snapshot, news)

            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self.config.quote_refresh_seconds
                    )
                    break
                except TimeoutError:
                    pass
                snapshot = await collector.refresh_light(snapshot)
                now = time.monotonic()
                if now - last_account_refresh >= self.config.account_refresh_seconds:
                    snapshot = await collector.refresh_account(snapshot)
                    last_account_refresh = now
                if self._history_due(snapshot):
                    snapshot = await collector.refresh_history(snapshot)
                    news = await self._news(client, snapshot.trade_server_time, news_engine)
                    last_news_refresh = now
                elif now - last_news_refresh >= self.config.news_refresh_seconds:
                    news = await self._news(client, snapshot.trade_server_time, news_engine)
                    last_news_refresh = now
                await self._publish(snapshot, news)

    async def _news(
        self,
        client: MT5ReadOnlyClient,
        server_time: str,
        engine: EconomicCalendarGate,
    ):
        try:
            events = await EconomicCalendarCollector(client, self.settings.news_gate).collect(
                server_time
            )
            return engine.evaluate(server_time, events)
        except Exception as exc:
            LOGGER.warning("Economic calendar failed closed (%s)", type(exc).__name__)
            return engine.unavailable(f"calendar_unavailable:{type(exc).__name__}")

    def _history_due(self, snapshot: Any) -> bool:
        if not snapshot.m1_candles:
            return True
        try:
            return (
                seconds_between(
                    parse_timestamp(snapshot.trade_server_time),
                    parse_timestamp(snapshot.m1_candles[-1].time),
                )
                >= 120.0
            )
        except TimestampError:
            return True

    async def _publish(self, snapshot: Any, news: Any) -> None:
        gate = MarketGate(self.settings.market_gate).evaluate(snapshot)
        preview = AIPreviewService(self.settings.ai).evaluate(snapshot, gate, news)
        try:
            last = SQLiteStateStore.last_advisory_read_only(
                self.settings.ai.state_db_path
            )
        except PersistentStateUnavailableError:
            last = None
        state = build_application_state(
            snapshot=snapshot,
            market_gate=gate,
            news_gate=news,
            preview=preview,
            settings=self.settings,
            last_advisory=last,
            events=self.events,
            started_at=self.started_at,
            mode=self.mode,
        )
        self.tracker.update(state)
        state["events"] = self.events.to_list()
        await self.hub.publish(state)
