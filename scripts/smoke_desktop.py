from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from websockets.sync.client import connect


HOST = "127.0.0.1"
PORT = 8765
ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "XAUUSD-AI.exe"
FORBIDDEN_KEYS = {
    "login",
    "account_login",
    "password",
    "broker",
    "server",
    "mcp_token",
    "openai_api_key",
    "authorization",
}


def _request(path: str, *, method: str = "GET", timeout: float = 2.0) -> Any:
    request = urllib.request.Request(
        f"http://{HOST}:{PORT}{path}", method=method
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_ready(process: subprocess.Popen[bytes], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Executable stopped during startup ({process.returncode})")
        try:
            if _request("/api/health").get("ok") is True:
                return
        except (OSError, urllib.error.URLError, ValueError):
            time.sleep(0.2)
    raise RuntimeError("Local dashboard did not become ready")


def _forbidden_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.add(str(key))
            found.update(_forbidden_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_forbidden_keys(item))
    return found


def _listener_addresses() -> list[str]:
    command = (
        f"Get-NetTCPConnection -State Listen -LocalPort {PORT} "
        "| Select-Object -ExpandProperty LocalAddress | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
    )
    raw = completed.stdout.strip()
    if not raw:
        return []
    parsed = json.loads(raw)
    return [parsed] if isinstance(parsed, str) else list(parsed)


def _application_pids() -> list[int]:
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='XAUUSD-AI.exe'\" "
        "| Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
    )
    raw = completed.stdout.strip()
    if not raw:
        return []
    parsed = json.loads(raw)
    return [int(parsed)] if isinstance(parsed, int) else [int(item) for item in parsed]


def _environment() -> tuple[dict[str, str], list[str]]:
    environment = os.environ.copy()
    secrets: list[str] = []
    for key, value in dotenv_values(ROOT / ".env").items():
        if value is not None:
            environment[key] = value
            if key in {"MT5_MCP_TOKEN", "OPENAI_API_KEY"} and value:
                secrets.append(value)
    # The smoke test must be incapable of making a paid request.
    environment["OPENAI_API_KEY"] = ""
    state_path = Path(
        environment.get("XAUUSD_AI_STATE_DB", ".state/xauusd_bot.sqlite3")
    ).expanduser()
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    # Validate the packaged app against the same existing DB as the developer CLI.
    environment["XAUUSD_AI_STATE_DB"] = str(state_path)
    return environment, secrets


def _web_mode(mode: str) -> dict[str, Any]:
    environment, secrets = _environment()
    arguments = [str(EXE)] + (["--browser-only"] if mode == "browser-only" else [])
    process = subprocess.Popen(arguments, env=environment)
    try:
        _wait_ready(process)
        state = _request("/api/state")
        state_deadline = time.monotonic() + 30
        while int(state.get("revision", 0)) == 0 and time.monotonic() < state_deadline:
            time.sleep(0.25)
            state = _request("/api/state")
        with connect(f"ws://{HOST}:{PORT}/ws", open_timeout=3) as websocket:
            websocket_state = json.loads(websocket.recv(timeout=3))
        encoded = json.dumps(state, allow_nan=False)
        if any(secret and secret in encoded for secret in secrets):
            raise RuntimeError("A configured secret appeared in dashboard state")
        forbidden = _forbidden_keys(state)
        if forbidden:
            raise RuntimeError(f"Forbidden dashboard keys: {sorted(forbidden)}")
        if int(websocket_state.get("revision", -1)) < int(state.get("revision", -1)):
            raise RuntimeError("WebSocket state was older than the REST state")
        addresses = _listener_addresses()
        if not addresses or set(addresses) != {HOST}:
            raise RuntimeError(f"Unexpected dashboard listener addresses: {addresses}")
        result = {
            "mode": mode,
            "started": True,
            "rest_websocket_revision": state.get("revision"),
            "listener_addresses": addresses,
            "ai_state": (state.get("ai") or {}).get("state"),
            "mt5_connected": (state.get("system") or {}).get("mt5_connected"),
            "calls_today": ((state.get("usage") or {}).get("summary") or {}).get(
                "calls_today"
            ),
            "known_spend_today_usd": (
                ((state.get("usage") or {}).get("summary") or {}).get(
                    "known_spend_today_usd"
                )
            ),
            "last_advisory_present": bool(
                (state.get("ai") or {}).get("last_advisory")
            ),
            "safe_payload": True,
        }
        _request("/api/exit", method="POST")
        process.wait(timeout=20)
        result["clean_exit"] = process.returncode == 0
        return result
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def _cli_only() -> dict[str, Any]:
    environment, _ = _environment()
    baseline_pids = set(_application_pids())
    launcher = (
        "import subprocess,sys; "
        "raise SystemExit(subprocess.call(sys.argv[1:]))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", launcher, str(EXE), "--cli-only"],
        env=environment,
        creationflags=(
            subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        ),
    )
    try:
        time.sleep(5)
        if process.poll() is not None:
            raise RuntimeError(f"CLI-only executable stopped early ({process.returncode})")
        with socket.socket() as probe:
            probe.settimeout(0.3)
            server_started = probe.connect_ex((HOST, PORT)) == 0
        if server_started:
            raise RuntimeError("CLI-only mode unexpectedly started the web server")
        return {
            "mode": "cli-only",
            "started": True,
            "web_server_started": False,
            "console_control_handler": "covered by Python lifecycle tests",
        }
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        for pid in set(_application_pids()) - baseline_pids:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid}"],
                check=False,
                capture_output=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("default", "browser-only", "cli-only"))
    args = parser.parse_args()
    if not EXE.is_file():
        raise SystemExit(f"Executable is missing: {EXE}")
    result = _cli_only() if args.mode == "cli-only" else _web_mode(args.mode)
    print(json.dumps(result, indent=2))
    if args.mode != "cli-only" and not result.get("clean_exit"):
        raise SystemExit("Executable did not shut down cleanly")


if __name__ == "__main__":
    main()
