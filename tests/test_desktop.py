from __future__ import annotations

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from xauusd_bot.config import AIConfig, MarketGateConfig, Settings
from xauusd_bot.desktop import DesktopApplication, LOCAL_HOST, parse_launch_mode
from xauusd_bot.economic_calendar import EconomicCalendarGate
from xauusd_bot.market_gate import MarketGate
from xauusd_bot.monitor import MonitoringService, StateHub
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

    async def test_cli_only_starts_no_server_or_browser_and_stops_cleanly(self) -> None:
        application = DesktopApplication(self.settings(), parse_launch_mode(["--cli-only"]))
        monitor = _FakeMonitor()
        application.monitor = monitor

        async def cli(_hub, stop_event):
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
