from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from typing import Any

from .config import NewsGateConfig
from .mcp_client import MCPToolError, MT5ReadOnlyClient
from .timestamps import (
    TimestampError,
    is_aware,
    mcp_timestamp,
    parse_timestamp,
    seconds_between,
)


class CalendarDataError(ValueError):
    """Raised when calendar data cannot support a safe decision."""


@dataclass(frozen=True, slots=True)
class EconomicNewsEvent:
    event_id: str
    value_id: str
    name: str
    currency: str
    importance: str
    scheduled_time: str
    minutes_to_event: float | None
    actual: float | None
    forecast: float | None
    previous: float | None
    revised_previous: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EconomicNewsGateResult:
    safe_for_ai: bool
    blackout_active: bool
    calendar_available: bool
    nearest_event: EconomicNewsEvent | None
    minutes_to_nearest_event: float | None
    upcoming_high_impact_events: tuple[EconomicNewsEvent, ...]
    recent_high_impact_events: tuple[EconomicNewsEvent, ...]
    rejection_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _optional_number(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise CalendarDataError(f"{key} is not numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CalendarDataError(f"{key} is not numeric") from exc
    if not math.isfinite(parsed):
        raise CalendarDataError(f"{key} is not finite")
    return parsed


class EconomicCalendarCollector:
    """Reads and joins authoritative USD event metadata and scheduled values."""

    def __init__(self, client: MT5ReadOnlyClient, config: NewsGateConfig) -> None:
        self._client = client
        self._config = config

    async def collect(self, trade_server_time: str) -> tuple[EconomicNewsEvent, ...]:
        try:
            server_time = parse_timestamp(trade_server_time)
        except TimestampError as exc:
            raise MCPToolError("Economic calendar has invalid trade-server time") from exc
        if is_aware(server_time):
            raise MCPToolError(
                "Economic calendar requires a timezone-free MT5 trade-server time"
            )

        metadata_payload = await self._client.call_tool(
            "economic_calendar_list_events_by_currency",
            {"currency_code": self._config.currency},
        )
        values_payload = await self._client.call_tool(
            "economic_calendar_list_values",
            {
                "datetime_from": mcp_timestamp(
                    server_time - timedelta(minutes=self._config.lookback_minutes)
                ),
                "datetime_to": mcp_timestamp(
                    server_time + timedelta(minutes=self._config.lookahead_minutes)
                ),
                "limit": self._config.request_limit,
            },
        )
        try:
            return self._join(metadata_payload, values_payload, server_time)
        except (CalendarDataError, TimestampError) as exc:
            raise MCPToolError(f"Economic calendar data is unusable: {exc}") from exc

    def _join(
        self,
        metadata_payload: dict[str, Any],
        values_payload: dict[str, Any],
        server_time: Any,
    ) -> tuple[EconomicNewsEvent, ...]:
        if metadata_payload.get("ok") is False or values_payload.get("ok") is False:
            raise CalendarDataError("terminal reported a calendar failure")
        if bool(values_payload.get("truncated", False)):
            raise CalendarDataError("calendar value response was truncated")

        raw_metadata = metadata_payload.get("events")
        raw_values = values_payload.get("values")
        if not isinstance(raw_metadata, list) or not raw_metadata:
            raise CalendarDataError("USD event metadata is missing")
        if not isinstance(raw_values, list):
            raise CalendarDataError("calendar values are missing")
        if metadata_payload.get("count") not in (None, len(raw_metadata)):
            raise CalendarDataError("USD event metadata count is inconsistent")
        if values_payload.get("count") not in (None, len(raw_values)):
            raise CalendarDataError("calendar value count is inconsistent")

        metadata: dict[str, dict[str, str]] = {}
        for item in raw_metadata:
            if not isinstance(item, dict):
                raise CalendarDataError("event metadata contains a non-object")
            event_id = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip()
            importance = str(item.get("importance", "")).strip()
            if not event_id or not name or not importance:
                raise CalendarDataError("event metadata is missing id, name, or importance")
            metadata[event_id] = {"name": name, "importance": importance}

        high_ids = {
            event_id
            for event_id, item in metadata.items()
            if item["importance"].casefold() == "high importance"
        }
        if not high_ids:
            raise CalendarDataError("USD calendar exposes no high-importance classification")

        minimum = server_time - timedelta(minutes=self._config.lookback_minutes)
        maximum = server_time + timedelta(minutes=self._config.lookahead_minutes)
        joined: list[EconomicNewsEvent] = []
        for item in raw_values:
            if not isinstance(item, dict):
                raise CalendarDataError("calendar values contain a non-object")
            event_id = str(item.get("event_id", "")).strip()
            if event_id not in high_ids:
                continue
            if bool(item.get("time_unknown", False)):
                raise CalendarDataError(
                    f"high-impact USD event {event_id} has an unknown release time"
                )
            scheduled_time = str(item.get("time", "")).strip()
            value_id = str(item.get("id", "")).strip()
            if not scheduled_time or not value_id:
                raise CalendarDataError("high-impact USD value lacks id or release time")
            parsed_time = parse_timestamp(scheduled_time)
            seconds_between(parsed_time, server_time)
            if parsed_time < minimum or parsed_time > maximum:
                continue
            detail = metadata[event_id]
            joined.append(
                EconomicNewsEvent(
                    event_id=event_id,
                    value_id=value_id,
                    name=detail["name"],
                    currency=self._config.currency,
                    importance=detail["importance"],
                    scheduled_time=scheduled_time,
                    minutes_to_event=None,
                    actual=_optional_number(item, "actual_value"),
                    forecast=_optional_number(item, "forecast_value"),
                    previous=_optional_number(item, "prev_value"),
                    revised_previous=_optional_number(item, "revised_prev_value"),
                )
            )
        joined.sort(key=lambda event: parse_timestamp(event.scheduled_time))
        return tuple(joined)


class EconomicCalendarGate:
    """Pure fail-closed high-impact USD blackout gate."""

    def __init__(self, config: NewsGateConfig) -> None:
        self._config = config

    def evaluate(
        self,
        trade_server_time: str,
        events: tuple[EconomicNewsEvent, ...],
    ) -> EconomicNewsGateResult:
        try:
            server_time = parse_timestamp(trade_server_time)
            enriched: list[EconomicNewsEvent] = []
            for event in events:
                if event.currency.upper() != self._config.currency.upper():
                    raise CalendarDataError("calendar event currency is not USD")
                if event.importance.casefold() != "high importance":
                    raise CalendarDataError("calendar event impact is not high importance")
                event_time = parse_timestamp(event.scheduled_time)
                minutes = seconds_between(event_time, server_time) / 60.0
                enriched.append(replace(event, minutes_to_event=round(minutes, 3)))
            enriched.sort(key=lambda event: event.minutes_to_event or 0.0)
            nearest = min(
                enriched,
                key=lambda event: abs(event.minutes_to_event or 0.0),
                default=None,
            )
            upcoming = tuple(
                event
                for event in enriched
                if event.minutes_to_event is not None and event.minutes_to_event >= 0
            )
            recent = tuple(
                sorted(
                    (
                        event
                        for event in enriched
                        if event.minutes_to_event is not None and event.minutes_to_event < 0
                    ),
                    key=lambda event: event.minutes_to_event or 0.0,
                    reverse=True,
                )
            )
            blackout = any(
                event.minutes_to_event is not None
                and -self._config.blackout_after_minutes
                <= event.minutes_to_event
                <= self._config.blackout_before_minutes
                for event in enriched
            )
            reasons = ("high_impact_usd_news_blackout",) if blackout else ()
            return EconomicNewsGateResult(
                safe_for_ai=not blackout,
                blackout_active=blackout,
                calendar_available=True,
                nearest_event=nearest,
                minutes_to_nearest_event=(
                    nearest.minutes_to_event if nearest is not None else None
                ),
                upcoming_high_impact_events=upcoming,
                recent_high_impact_events=recent,
                rejection_reasons=reasons,
            )
        except Exception as exc:
            return self.unavailable(f"calendar_interpretation_error:{type(exc).__name__}")

    @staticmethod
    def unavailable(reason: str) -> EconomicNewsGateResult:
        return EconomicNewsGateResult(
            safe_for_ai=False,
            blackout_active=False,
            calendar_available=False,
            nearest_event=None,
            minutes_to_nearest_event=None,
            upcoming_high_impact_events=(),
            recent_high_impact_events=(),
            rejection_reasons=(reason,),
        )
