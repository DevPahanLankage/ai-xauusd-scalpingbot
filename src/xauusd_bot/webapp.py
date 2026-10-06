from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .monitor import StateHub


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def frontend_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "xauusd_bot" / "web_dist"
    return Path(__file__).resolve().parent / "web_dist"


def _local(host: str | None) -> bool:
    return bool(host and host.casefold() in LOOPBACK_HOSTS)


def create_web_app(hub: StateHub, request_shutdown: Callable[[], None]) -> FastAPI:
    app = FastAPI(
        title="XAUUSD AI Monitor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    assets = frontend_directory()

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, object]:
        if not _local(request.client.host if request.client else None):
            raise HTTPException(status_code=403, detail="Local access only")
        revision, _ = await hub.snapshot()
        return {"ok": True, "revision": revision, "advisory_only": True}

    @app.get("/api/state")
    async def state(request: Request) -> JSONResponse:
        if not _local(request.client.host if request.client else None):
            raise HTTPException(status_code=403, detail="Local access only")
        _, current = await hub.snapshot()
        return JSONResponse(current)

    @app.post("/api/exit", status_code=202)
    async def exit_application(request: Request) -> dict[str, bool]:
        if not _local(request.client.host if request.client else None):
            raise HTTPException(status_code=403, detail="Local access only")
        request_shutdown()
        return {"accepted": True}

    @app.websocket("/ws")
    async def websocket_state(websocket: WebSocket) -> None:
        if not _local(websocket.client.host if websocket.client else None):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        revision = -1
        try:
            while True:
                revision, current = await (
                    hub.snapshot() if revision < 0 else hub.wait_after(revision)
                )
                await websocket.send_json(current)
        except WebSocketDisconnect:
            return

    if assets.is_dir():
        static_dir = assets / "assets"
        if static_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

        @app.get("/{path:path}")
        async def frontend(path: str) -> FileResponse:
            candidate = (assets / path).resolve()
            if path and candidate.is_file() and assets.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(assets / "index.html")

    else:

        @app.get("/{path:path}")
        async def frontend_missing(path: str) -> HTMLResponse:
            return HTMLResponse(
                "<h1>XAUUSD AI</h1><p>Frontend assets are unavailable. "
                "Run the frontend production build.</p>",
                status_code=503,
            )

    return app
