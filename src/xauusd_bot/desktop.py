from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import uvicorn
from dotenv import load_dotenv

from .config import ConfigurationError, Settings
from .logging_utils import configure_logging
from .monitor import MonitoringService
from .rich_dashboard import run_rich_dashboard
from .webapp import create_web_app


LOCAL_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
APP_DATA_DIRECTORY = "XAUUSD-AI"
ENABLE_PROCESSED_OUTPUT = 0x0001
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


@dataclass(frozen=True, slots=True)
class LaunchMode:
    name: str
    show_cli: bool
    serve_browser: bool
    open_browser: bool


def parse_launch_mode(argv: Sequence[str] | None = None) -> LaunchMode:
    parser = argparse.ArgumentParser(description="XAUUSD advisory-only desktop monitor")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--browser-only", action="store_true")
    modes.add_argument("--cli-only", action="store_true")
    args = parser.parse_args(argv)
    if args.browser_only:
        return LaunchMode("browser-only", False, True, True)
    if args.cli_only:
        return LaunchMode("cli-only", True, False, False)
    return LaunchMode("combined", True, True, True)


def runtime_env_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".env"
    return Path.cwd() / ".env"


def runtime_state_base_path() -> Path:
    """Return the deterministic base for a relative desktop state DB path."""

    if getattr(sys, "frozen", False):
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / APP_DATA_DIRECTORY
        return Path(sys.executable).resolve().parent / "data"
    return runtime_env_path().resolve().parent


def _enable_windows_vt_mode(kernel32: object, handle: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32.GetConsoleMode.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetConsoleMode.restype = wintypes.BOOL
    mode = wintypes.DWORD()
    if not handle or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    target = mode.value | ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
    return bool(kernel32.SetConsoleMode(handle, target))


def _enable_windows_vt_for_stream(kernel32: object, stream: object) -> bool:
    try:
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return False
    return _enable_windows_vt_mode(kernel32, handle)


def _ensure_console() -> tuple[bool, bool]:
    if os.name != "nt":
        return (False, False)
    import ctypes

    kernel32 = ctypes.windll.kernel32
    console_was_present = bool(kernel32.GetConsoleWindow())
    console_available = console_was_present
    if not console_available:
        console_available = bool(kernel32.AttachConsole(-1))
        if not console_available:
            console_available = bool(kernel32.AllocConsole())
    if not console_available:
        return (False, False)

    # A windowed PyInstaller process has no Python console streams even after
    # AttachConsole/AllocConsole. Rebind only for that case; preserve source-mode
    # streams and redirection when Python already owns a console.
    if not console_was_present or sys.stdout is None or sys.stderr is None:
        try:
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stdin = open("CONIN$", "r", encoding="utf-8")
        except OSError:
            return (False, False)

    kernel32.SetConsoleOutputCP(65001)
    kernel32.SetConsoleCP(65001)
    stdout_vt = _enable_windows_vt_for_stream(kernel32, sys.stdout)
    stderr_vt = _enable_windows_vt_for_stream(kernel32, sys.stderr)
    return (stdout_vt, stderr_vt)


def _show_error(message: str, *, browser_only: bool) -> None:
    if browser_only and os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "XAUUSD AI", 0x10)
    else:
        print(message, file=sys.stderr)


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind((LOCAL_HOST, port))
        except OSError:
            return False
    return True


class DesktopApplication:
    def __init__(
        self,
        settings: Settings,
        mode: LaunchMode,
        *,
        port: int = DEFAULT_PORT,
        force_terminal: bool | None = None,
    ) -> None:
        self.settings = settings
        self.mode = mode
        self.port = port
        self.force_terminal = force_terminal
        self.stop_event = asyncio.Event()
        self.monitor = MonitoringService(settings, mode=mode.name)

    def request_shutdown(self) -> None:
        self.stop_event.set()

    def _install_console_shutdown_handler(self):
        if os.name != "nt" or not self.mode.show_cli:
            return None
        import ctypes

        loop = asyncio.get_running_loop()
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        @handler_type
        def handler(control_type: int) -> bool:
            if control_type in {0, 1, 2, 5, 6}:
                loop.call_soon_threadsafe(self.request_shutdown)
                return True
            return False

        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True):
            return None
        return handler

    async def run(self) -> None:
        if self.mode.serve_browser and not _port_available(self.port):
            raise RuntimeError(
                f"Dashboard port {LOCAL_HOST}:{self.port} is already in use"
            )
        console_handler = self._install_console_shutdown_handler()
        tasks: list[asyncio.Task[None]] = []
        server: uvicorn.Server | None = None
        if self.mode.serve_browser:
            app = create_web_app(self.monitor.hub, self.request_shutdown)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=LOCAL_HOST,
                    port=self.port,
                    log_level="warning",
                    access_log=False,
                    log_config=None,
                )
            )
            server_task = asyncio.create_task(server.serve(), name="web-server")
            tasks.append(server_task)
            await self._wait_for_backend(server, server_task)
        tasks.append(asyncio.create_task(self.monitor.run(self.stop_event), name="monitor"))
        if self.mode.serve_browser and self.mode.open_browser:
            webbrowser.open(f"http://{LOCAL_HOST}:{self.port}", new=2)
        if self.mode.show_cli:
            tasks.append(
                asyncio.create_task(
                    run_rich_dashboard(
                        self.monitor.hub,
                        self.stop_event,
                        force_terminal=self.force_terminal,
                    ),
                    name="rich-cli",
                )
            )
        stop_task = asyncio.create_task(self.stop_event.wait(), name="shutdown-waiter")
        try:
            done, _ = await asyncio.wait(
                {stop_task, *tasks}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task not in done:
                failed = next((task for task in done if task.exception() is not None), None)
                if failed is not None:
                    raise RuntimeError(
                        f"Desktop service {failed.get_name()} stopped unexpectedly"
                    ) from failed.exception()
                raise RuntimeError("A desktop service stopped unexpectedly")
        finally:
            self.stop_event.set()
            if server is not None:
                server.should_exit = True
            if not stop_task.done():
                stop_task.cancel()
            _, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(stop_task, return_exceptions=True)
            if console_handler is not None:
                import ctypes

                ctypes.windll.kernel32.SetConsoleCtrlHandler(console_handler, False)

    async def _wait_for_backend(
        self, server: uvicorn.Server, server_task: asyncio.Task[None]
    ) -> None:
        for _ in range(100):
            if server.started:
                return
            if server_task.done():
                error = server_task.exception()
                raise RuntimeError("Local dashboard server failed to start") from error
            await asyncio.sleep(0.05)
        raise RuntimeError("Local dashboard server did not become ready")


def main(argv: Sequence[str] | None = None) -> None:
    mode = parse_launch_mode(argv)
    force_terminal: bool | None = None
    if mode.show_cli:
        stdout_vt, _ = _ensure_console()
        force_terminal = True if stdout_vt else None
    environment_path = runtime_env_path()
    load_dotenv(environment_path, override=False)
    try:
        settings = Settings.from_environment(
            state_base_path=runtime_state_base_path()
        )
    except ConfigurationError as exc:
        _show_error(f"Configuration error: {exc}", browser_only=not mode.show_cli)
        raise SystemExit(2) from exc
    configure_logging(
        settings.log_level,
        secrets=(settings.mcp_token or "", settings.ai.api_key or ""),
        console=mode.show_cli,
    )
    try:
        asyncio.run(
            DesktopApplication(
                settings,
                mode,
                force_terminal=force_terminal,
            ).run()
        )
    except KeyboardInterrupt:
        return
    except Exception as exc:
        _show_error(
            f"XAUUSD AI stopped: {type(exc).__name__}: {exc}",
            browser_only=not mode.show_cli,
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
