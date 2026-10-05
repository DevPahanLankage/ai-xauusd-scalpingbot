from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from xauusd_bot.config import Settings
from xauusd_bot.mcp_client import (
    READ_ONLY_TOOL_ALLOWLIST,
    MT5ReadOnlyClient,
    UnsafeToolError,
)


class _FakeProtocolClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(name)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"ok": true}')],
            structured_content=None,
            is_error=False,
        )


class MCPReadOnlySafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = MT5ReadOnlyClient(Settings(mcp_url="http://127.0.0.1/mcp"))
        self.protocol = _FakeProtocolClient()
        self.client._client = self.protocol  # type: ignore[assignment]

    async def test_trading_and_unknown_tools_are_blocked_locally(self) -> None:
        blocked = (
            "trade_send_market_order",
            "trade_send_pending_order",
            "trade_modify_sl_tp",
            "trade_close_single_position",
            "arbitrary_unknown_tool",
        )
        for name in blocked:
            with self.subTest(name=name):
                with self.assertRaises(UnsafeToolError):
                    await self.client.call_tool(name, {})
        self.assertEqual(self.protocol.calls, [])

    async def test_every_allowlisted_tool_can_reach_protocol_client(self) -> None:
        for name in sorted(READ_ONLY_TOOL_ALLOWLIST):
            result = await self.client.call_tool(name, {})
            self.assertEqual(result, {"ok": True})
        self.assertEqual(self.protocol.calls, sorted(READ_ONLY_TOOL_ALLOWLIST))

    def test_allowlist_is_exactly_the_reviewed_read_only_surface(self) -> None:
        self.assertEqual(
            READ_ONLY_TOOL_ALLOWLIST,
            {
                "get_workspace_info",
                "get_marketwatch_symbols",
                "get_trading_account_info",
                "get_trading_open_positions",
                "get_time_information",
                "get_chart_history",
                "get_chart_ticks_history",
                "economic_calendar_list_events_by_currency",
                "economic_calendar_list_values",
            },
        )


if __name__ == "__main__":
    unittest.main()
