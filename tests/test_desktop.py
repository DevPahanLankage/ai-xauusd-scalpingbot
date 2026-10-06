from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from rich.console import Console

from xauusd_bot.application_state import EventFeed, build_application_state
from xauusd_bot.config import AIConfig, MarketGateConfig, Settings
from xauusd_bot.desktop import (
    APP_DATA_DIRECTORY,
    ENABLE_PROCESSED_OUTPUT,
    ENABLE_VIRTUAL_TERMINAL_PROCESSING,
    DesktopApplication,
    LOCAL_HOST,
    _enable_windows_vt_mode,
    parse_launch_mode,
    runtime_state_base_path,
)
from xauusd_bot.economic_calendar import EconomicCalendarGate
from xauusd_bot.market_gate import MarketGate
from xauusd_bot.monitor import MonitoringService, StateHub
from xauusd_bot.preview import AIPreviewService
from xauusd_bot.rich_dashboard import (
    _sync_console_dimensions,
    render_dashboard,
    run_rich_dashboard,
)
from xauusd_bot.webapp import _local, create_web_app
from tests.test_market_gate import _snapshot


class LaunchModeTests(unittest.TestCase):
    def test_default_combined_mode(self) -> None:
        mode = parse_launch_mode([])
        self.assertTrue(mode.show_cli)
        self.assertTrue(mode.serve_browser)
        self.assertTrue(mode.open_browser)

    def test_browser_only_mode(self) -> None:
        mode = parse_launch_mode(["--browser-only"])
        self.assertFalse(mode.show_cli)
        self.assertTrue(mode.serve_browser)

    def test_cli_only_mode(self) -> None:
        mode = parse_launch_mode(["--cli-only"])
        self.assertTrue(mode.show_cli)
        self.assertFalse(mode.serve_browser)
        self.assertFalse(mode.open_browser)

    def test_dashboard_binding_is_loopback_only(self) -> None:
        self.assertEqual(LOCAL_HOST, "127.0.0.1")
        self.assertTrue(_local("127.0.0.1"))
        self.assertFalse(_local("192.168.1.5"))

    def test_packaged_relative_state_base_uses_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            sys, "frozen", True, create=True
        ), patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
            self.assertEqual(
                runtime_state_base_path(), Path(temporary) / APP_DATA_DIRECTORY
            )

    def test_source_state_base_uses_environment_file_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            sys, "frozen", False, create=True
        ), patch("xauusd_bot.desktop.runtime_env_path", return_value=Path(temporary) / ".env"):
            self.assertEqual(runtime_state_base_path(), Path(temporary))

    def test_virtual_terminal_processing_is_enabled_without_dropping_modes(self) -> None:
        kernel32 = MagicMock()

        def get_console_mode(_handle, pointer):
            pointer._obj.value = 0x0002
            return True

        kernel32.GetConsoleMode.side_effect = get_console_mode
        kernel32.SetConsoleMode.return_value = True
        self.assertTrue(_enable_windows_vt_mode(kernel32, 123))
        configured = kernel32.SetConsoleMode.call_args.args[1]
        self.assertEqual(configured & 0x0002, 0x0002)
        self.assertEqual(configured & ENABLE_PROCESSED_OUTPUT, ENABLE_PROCESSED_OUTPUT)
        self.assertEqual(
            configured & ENABLE_VIRTUAL_TERMINAL_PROCESSING,
            ENABLE_VIRTUAL_TERMINAL_PROCESSING,
        )

    def test_virtual_terminal_setup_fails_closed_for_non_console_handle(self) -> None:
        kernel32 = MagicMock()
        kernel32.GetConsoleMode.return_value = False
        self.assertFalse(_enable_windows_vt_mode(kernel32, 123))
        kernel32.SetConsoleMode.assert_not_called()


class WebApplicationTests(unittest.TestCase):
    def test_api_websocket_and_shutdown_share_one_state(self) -> None:
        initial = {"revision": 0, "safe": True, "secret": None}
        hub = StateHub(initial)
        stopped: list[bool] = []
        app = create_web_app(hub, lambda: stopped.append(True))
        with TestClient(app) as client:
            response = client.get("/api/state")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["safe"])
            with client.websocket_connect("/ws") as websocket:
                self.assertTrue(websocket.receive_json()["safe"])
            exit_response = client.post("/api/exit")
        self.assertEqual(exit_response.status_code, 202)
        self.assertEqual(stopped, [True])


class DashboardRenderTests(unittest.TestCase):
    def test_rebound_console_stream_supplies_actual_dimensions(self) -> None:
        console = MagicMock()
        console.file.fileno.return_value = 7
        console.size = SimpleNamespace(width=120, height=30)
        with patch("xauusd_bot.rich_dashboard.os.get_terminal_size") as terminal_size:
            terminal_size.return_value = os.terminal_size((120, 30))
            size = _sync_console_dimensions(console)
        terminal_size.assert_called_once_with(7)
        self.assertEqual(console.width, 120)
        self.assertEqual(console.height, 30)
        self.assertEqual((size.width, size.height), (120, 30))

    def state(self) -> dict:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = Settings(
            mcp_url="http://127.0.0.1/mcp",
            ai=AIConfig(state_db_path=Path(temporary.name) / "missing.sqlite3"),
        )
        snapshot = _snapshot()
        gate = MarketGate(settings.market_gate).evaluate(snapshot)
        news = EconomicCalendarGate(settings.news_gate).evaluate(
            snapshot.trade_server_time, ()
        )
        preview = AIPreviewService(settings.ai).evaluate(snapshot, gate, news)
        state = build_application_state(
            snapshot=snapshot,
            market_gate=gate,
            news_gate=news,
            preview=preview,
            settings=settings,
            last_advisory=None,
            events=EventFeed(),
            started_at=datetime.now(timezone.utc),
            mode="combined",
        )
        state["ai"]["last_advisory"] = {
            "candidate_time": "2026-10-06T12:00:00Z",
            "model": "gpt-6.1-sol",
            "status": "success",
            "decision": "NO_TRADE",
            "confidence": 88,
            "market_regime": "range",
            "setup_summary": "No clean entry",
            "entry_price": None,
            "stop_loss": None,
            "take_profit": None,
            "risk_reward_ratio": None,
            "invalidation_reason": "Spread expansion",
            "warnings": ["Demo advisory"],
            "usage": {"input_tokens": 1000, "output_tokens": 100},
            "estimated_cost_usd": 0.01,
            "latency_ms": 1250,
        }
        state["events"] = [
            {
                "timestamp": f"2026-10-06T12:00:0{index}+00:00",
                "level": "INFO",
                "message": f"Event {index}",
            }
            for index in range(5)
        ]
        return state

    def render(self, state: dict, *, width: int, height: int) -> str:
        output = StringIO()
        console = Console(file=output, width=width, color_system=None)
        console.print(render_dashboard(state, width=width, height=height))
        return output.getvalue()

    def test_full_dashboard_fits_common_windows_viewport(self) -> None:
        rendered = self.render(self.state(), width=120, height=30)
        self.assertLessEqual(len(rendered.splitlines()), 30)
        for title in (
            "MARKET",
            "MARKET GATE",
            "VALIDITY",
            "NEWS GATE",
            "LAST AI ADVISORY",
            "OPENAI / BUDGET",
            "ACCOUNT",
            "SYSTEM",
            "RECENT EVENTS",
        ):
            self.assertIn(title, rendered)

    def test_small_terminal_uses_bounded_safety_summary(self) -> None:
        rendered = self.render(self.state(), width=80, height=20)
        self.assertLessEqual(len(rendered.splitlines()), 20)
        for value in (
            "Eligible / news",
            "Bid / ask",
            "Spread",
            "M1 / M5 / align",
            "Last decision",
            "Terminal too small",
        ):
            self.assertIn(value, rendered)


class MonitorSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_publish_never_initializes_openai(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(
                mcp_url="http://127.0.0.1/mcp",
                ai=AIConfig(api_key="fake", state_db_path=Path(temporary) / "missing.db"),
            )
            service = MonitoringService(settings, mode="combined")
            snapshot = _snapshot()
            news = EconomicCalendarGate(settings.news_gate).evaluate(
                snapshot.trade_server_time, ()
            )
            with patch("xauusd_bot.advisor.OpenAIAdvisor") as advisor:
                await service._publish(snapshot, news)
            advisor.assert_not_called()
            _, state = await service.hub.snapshot()
            self.assertEqual(
                state["market_gate"]["eligible_for_ai"],
                MarketGate(MarketGateConfig()).evaluate(snapshot).eligible_for_ai,
            )


class _FakeMonitor:
    def __init__(self) -> None:
        self.hub = StateHub({"safe": True})
        self.started = False
        self.stopped = False

    async def run(self, stop_event: asyncio.Event) -> None:
        self.started = True
        await stop_event.wait()
        self.stopped = True


class DesktopLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def settings(self) -> Settings:
        return Settings(mcp_url="http://127.0.0.1/mcp")

    async def test_rich_dashboard_forces_modern_terminal_after_vt_setup(self) -> None:
        stop_event = asyncio.Event()
        stop_event.set()
        console = MagicMock()
        console.is_terminal = True
        live = MagicMock()
        with (
            patch("xauusd_bot.rich_dashboard.Console", return_value=console) as factory,
            patch("xauusd_bot.rich_dashboard.Live", return_value=live) as live_factory,
            patch.dict(os.environ, {"TERM": "dumb"}),
        ):
            await run_rich_dashboard(
                MagicMock(), stop_event, force_terminal=True
            )
        console_options = factory.call_args.kwargs
        self.assertTrue(console_options["force_terminal"])
        self.assertFalse(console_options["legacy_windows"])
        self.assertNotIn("TERM", console_options["_environ"])
        live_factory.assert_called_once_with(
            console=console,
            screen=True,
            auto_refresh=False,
            vertical_overflow="crop",
        )

    async def test_rich_dashboard_uses_one_live_across_state_transitions(self) -> None:
        stop_event = asyncio.Event()
        console = MagicMock()
        console.is_terminal = True
        console.size = SimpleNamespace(width=120, height=30)
        live_context = MagicMock()
        live = MagicMock()
        live_context.__enter__.return_value = live
        calls = 0
        states = [
            {"name": "disconnected", "eligible": False, "candidate": None},
            {"name": "connected-valid", "eligible": True, "candidate": "12:00"},
            {"name": "connected-blocked", "eligible": False, "candidate": "12:00"},
            {"name": "connected-valid", "eligible": True, "candidate": "12:01"},
        ]

        async def next_state(*_args):
            nonlocal calls
            state = states[calls]
            calls += 1
            if calls == len(states):
                stop_event.set()
            return calls, state

        with (
            patch("xauusd_bot.rich_dashboard.Console", return_value=console),
            patch("xauusd_bot.rich_dashboard.Live", return_value=live_context) as factory,
            patch("xauusd_bot.rich_dashboard.asyncio_wait_state", side_effect=next_state),
            patch(
                "xauusd_bot.rich_dashboard.render_dashboard", return_value=MagicMock()
            ) as renderer,
        ):
            await run_rich_dashboard(MagicMock(), stop_event, force_terminal=True)

        factory.assert_called_once()
        self.assertEqual(live.update.call_count, len(states))
        self.assertEqual(
            [call.args[0] for call in renderer.call_args_list],
            states,
        )

    async def test_cli_only_starts_no_server_or_browser_and_stops_cleanly(self) -> None:
        application = DesktopApplication(
            self.settings(),
            parse_launch_mode(["--cli-only"]),
            force_terminal=True,
        )
        monitor = _FakeMonitor()
        application.monitor = monitor
        terminal_settings: list[bool | None] = []

        async def cli(_hub, stop_event, *, force_terminal=None):
            terminal_settings.append(force_terminal)
            await stop_event.wait()

        with (
            patch("xauusd_bot.desktop.run_rich_dashboard", side_effect=cli),
            patch("xauusd_bot.desktop.webbrowser.open") as browser,
        ):
            task = asyncio.create_task(application.run())
            await asyncio.sleep(0.02)
            application.request_shutdown()
            await asyncio.wait_for(task, timeout=2)
        self.assertTrue(monitor.started)
        self.assertTrue(monitor.stopped)
        self.assertEqual(terminal_settings, [True])
        browser.assert_not_called()

    async def test_windows_console_control_requests_clean_shutdown(self) -> None:
        application = DesktopApplication(self.settings(), parse_launch_mode(["--cli-only"]))
        handler = application._install_console_shutdown_handler()
        self.assertIsNotNone(handler)
        assert handler is not None
        try:
            self.assertTrue(handler(0))
            await asyncio.sleep(0)
            self.assertTrue(application.stop_event.is_set())
        finally:
            import ctypes

            ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, False)

    async def test_browser_opens_only_after_local_backend_is_ready(self) -> None:
        with socket.socket() as probe:
            probe.bind((LOCAL_HOST, 0))
            port = int(probe.getsockname()[1])
        application = DesktopApplication(
            self.settings(), parse_launch_mode(["--browser-only"]), port=port
        )
        monitor = _FakeMonitor()
        application.monitor = monitor
        with patch(
            "xauusd_bot.desktop.webbrowser.open",
            side_effect=lambda *_args, **_kwargs: application.request_shutdown(),
        ) as browser:
            await asyncio.wait_for(application.run(), timeout=3)
        browser.assert_called_once_with(f"http://{LOCAL_HOST}:{port}", new=2)
        self.assertTrue(monitor.stopped)

    async def test_port_conflict_fails_before_monitor_starts(self) -> None:
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            occupied.bind((LOCAL_HOST, 0))
            port = int(occupied.getsockname()[1])
            application = DesktopApplication(
                self.settings(), parse_launch_mode(["--browser-only"]), port=port
            )
            monitor = _FakeMonitor()
            application.monitor = monitor
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                await application.run()
        self.assertFalse(monitor.started)


if __name__ == "__main__":
    unittest.main()
