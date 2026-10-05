from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .config import Settings


LOGGER = logging.getLogger(__name__)

READ_ONLY_TOOL_ALLOWLIST = frozenset(
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
    }
)


class MCPToolError(RuntimeError):
    """Raised when an allowed MCP tool fails or returns unusable data."""


class UnsafeToolError(PermissionError):
    """Raised before a non-allowlisted MCP tool can reach the terminal."""


class MT5ReadOnlyClient:
    """Small safety-focused wrapper around the official MCP Python client."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self.workspace_info: dict[str, Any] | None = None

    async def __aenter__(self) -> "MT5ReadOnlyClient":
        stack = AsyncExitStack()
        self._stack = stack
        headers: dict[str, str] = {}
        if self._settings.mcp_token:
            headers["Authorization"] = f"Bearer {self._settings.mcp_token}"

        try:
            http_client = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers=headers,
                    timeout=httpx2.Timeout(
                        self._settings.timeout_seconds,
                        read=max(300.0, self._settings.timeout_seconds),
                    ),
                )
            )
            transport = streamable_http_client(
                self._settings.mcp_url,
                http_client=http_client,
            )
            self._client = await stack.enter_async_context(Client(transport))

            # The Terminal MCP requires this to be the first tool call in a session.
            self.workspace_info = await self.call_tool("get_workspace_info", {})
            await self._verify_required_tools()
        except BaseException:
            await stack.aclose()
            self._stack = None
            self._client = None
            raise

        LOGGER.info("Connected to the MT5 Terminal MCP in read-only mode")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._client = None

    async def _verify_required_tools(self) -> None:
        if self._client is None:
            raise RuntimeError("MCP client is not connected")
        response = await self._client.list_tools()
        advertised = {tool.name for tool in response.tools}
        missing = READ_ONLY_TOOL_ALLOWLIST - advertised
        if missing:
            names = ", ".join(sorted(missing))
            raise MCPToolError(f"Terminal MCP is missing required read-only tools: {names}")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in READ_ONLY_TOOL_ALLOWLIST:
            raise UnsafeToolError(f"Blocked non-read-only MCP tool: {name}")
        if name.startswith("trade_"):
            raise UnsafeToolError(f"Blocked trading MCP tool: {name}")
        if self._client is None:
            raise RuntimeError("MCP client is not connected")

        LOGGER.debug("Calling allowed read-only MCP tool %s", name)
        result = await self._client.call_tool(name, arguments)
        texts = [
            block.text
            for block in result.content
            if getattr(block, "type", None) == "text" and hasattr(block, "text")
        ]
        if getattr(result, "is_error", False):
            message = " ".join(texts) or "unspecified MCP error"
            raise MCPToolError(f"{name} failed: {message}")

        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict) and structured:
            return self._normalise_payload(structured, name)
        for text in texts:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        raise MCPToolError(f"{name} returned no JSON object")

    @staticmethod
    def _normalise_payload(payload: dict[str, Any], tool_name: str) -> dict[str, Any]:
        if set(payload) == {"result"}:
            nested = payload["result"]
            if isinstance(nested, dict):
                return nested
            if isinstance(nested, str):
                try:
                    decoded = json.loads(nested)
                except json.JSONDecodeError as exc:
                    raise MCPToolError(f"{tool_name} returned invalid JSON") from exc
                if isinstance(decoded, dict):
                    return decoded
        return payload
