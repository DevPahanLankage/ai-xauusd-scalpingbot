from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from xauusd_bot.config import NewsGateConfig
from xauusd_bot.economic_calendar import (
    EconomicCalendarCollector,
    EconomicCalendarGate,
    EconomicNewsEvent,
)
from xauusd_bot.mcp_client import MCPToolError


SERVER = datetime(2026, 1, 5, 12, 0)


def event(minutes: int, *, name: str = "US CPI") -> EconomicNewsEvent:
    return EconomicNewsEvent(
        event_id="8401",
        value_id=f"value-{minutes}",
        name=name,
        currency="USD",
        importance="High importance",
        scheduled_time=(SERVER + timedelta(minutes=minutes)).isoformat(),
        minutes_to_event=None,
        actual=None,
        forecast=2.1,
        previous=2.0,
        revised_previous=None,
    )


class _FakeCalendarClient:
    def __init__(self, metadata: dict, values: dict) -> None:
        self.metadata = metadata
        self.values = values

    async def call_tool(self, name: str, arguments: dict) -> dict:
        if name == "economic_calendar_list_events_by_currency":
            return self.metadata
        if name == "economic_calendar_list_values":
            return self.values
        raise AssertionError(name)


class EconomicCalendarGateTests(unittest.IsolatedAsyncioTestCase):
    def test_high_impact_event_inside_blackout_rejects(self) -> None:
        result = EconomicCalendarGate(NewsGateConfig()).evaluate(
            SERVER.isoformat(), (event(20),)
        )
        self.assertFalse(result.safe_for_ai)
        self.assertTrue(result.blackout_active)
        self.assertEqual(result.minutes_to_nearest_event, 20.0)

    def test_high_impact_event_outside_blackout_is_preserved(self) -> None:
        result = EconomicCalendarGate(NewsGateConfig()).evaluate(
            SERVER.isoformat(), (event(-45), event(90, name="FOMC"))
        )
        self.assertTrue(result.safe_for_ai)
        self.assertEqual(len(result.recent_high_impact_events), 1)
        self.assertEqual(len(result.upcoming_high_impact_events), 1)

    def test_calendar_unavailable_fails_closed(self) -> None:
        result = EconomicCalendarGate.unavailable("calendar_unavailable:test")
        self.assertFalse(result.safe_for_ai)
        self.assertFalse(result.calendar_available)

    async def test_malformed_calendar_data_fails_closed(self) -> None:
        client = _FakeCalendarClient(
            {"ok": True, "events": [{"id": "8401", "name": "CPI"}]},
            {"ok": True, "truncated": False, "values": []},
        )
        collector = EconomicCalendarCollector(client, NewsGateConfig())  # type: ignore[arg-type]
        with self.assertRaises(MCPToolError):
            await collector.collect(SERVER.isoformat())

    async def test_truncated_calendar_data_fails_closed(self) -> None:
        client = _FakeCalendarClient(
            {
                "ok": True,
                "events": [
                    {
                        "id": "8401",
                        "name": "CPI",
                        "importance": "High importance",
                    }
                ],
            },
            {"ok": True, "truncated": True, "values": []},
        )
        collector = EconomicCalendarCollector(client, NewsGateConfig())  # type: ignore[arg-type]
        with self.assertRaises(MCPToolError):
            await collector.collect(SERVER.isoformat())


if __name__ == "__main__":
    unittest.main()
