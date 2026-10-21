"""One FastAPI application for lifecycle, administration and reverse OneBot WS."""
from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.runtime import Runtime


def configure_logging(settings):
    """Rotate daily and retain every file; disk alarms request operator action."""
    folder = Path(str(getattr(settings, "local_log_directory", "logs") or "logs")).expanduser()
    if not folder.is_absolute():
        folder = Path(getattr(settings, "app_root", Path.cwd())) / folder
    folder.mkdir(parents=True, exist_ok=True)
    path = (folder / "bot.log").resolve()
    root_logger = logging.getLogger()
    if any(getattr(handler, "baseFilename", "") == str(path) for handler in root_logger.handlers):
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.close(descriptor)
    path.chmod(0o600)
    handler = TimedRotatingFileHandler(path, when="midnight", backupCount=0, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root_logger.addHandler(handler)
    if not any(isinstance(item, logging.StreamHandler) and not isinstance(item, logging.FileHandler)
               for item in root_logger.handlers):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        root_logger.addHandler(console)
    root_logger.setLevel(getattr(logging, str(getattr(settings, "log_level", "INFO")).upper(), logging.INFO))
    return handler


def create_app(runtime=None):
    runtime = runtime or Runtime()

    @asynccontextmanager
    async def lifespan(application):
        log_handler = configure_logging(runtime.settings) if getattr(runtime, "background_enabled", True) else None
        try:
            await runtime.start()
            yield
        finally:
            await runtime.close()
            if log_handler is not None:
                logging.getLogger().removeHandler(log_handler)
                log_handler.close()

    application = FastAPI(title="Bilibili QQ Group Gatekeeper", lifespan=lifespan)
    application.state.runtime = runtime

    @application.get("/healthz")
    async def health():
        return {"status": "ok" if runtime.started else "starting",
                "onebot_connected": sum(client.connected for client in runtime.clients.values())}

    async def reverse(websocket: WebSocket):
        configured = str(getattr(runtime.settings, "onebot_access_token", ""))
        authorization = websocket.headers.get("authorization", "")
        supplied = authorization[7:] if authorization.lower().startswith("bearer ") else websocket.query_params.get("access_token", "")
        if configured and not hmac.compare_digest(configured, supplied):
            await websocket.close(code=1008, reason="invalid access token")
            return
        self_id = websocket.headers.get("x-self-id", websocket.query_params.get("self_id", ""))
        if not str(self_id).isdigit():
            await websocket.close(code=1008, reason="X-Self-ID is required")
            return
        await websocket.accept()
        try:
            await runtime.attach_reverse(websocket, str(self_id))
        except WebSocketDisconnect:
            return

    application.add_api_websocket_route("/onebot/v11/ws", reverse)
    application.add_api_websocket_route("/onebot/v11/ws/", reverse)

    from app.web.admin_api import install_admin_routes
    from app.web.verify_h5 import install_verify_routes

    install_admin_routes(application, runtime)
    install_verify_routes(application, runtime)
    return application


def main():
    import uvicorn
    from app.settings import get_settings

    settings = get_settings()
    configure_logging(settings)
    uvicorn.run(create_app(), host=str(getattr(settings, "host", getattr(settings, "webui_host", "127.0.0.1"))),
                port=int(getattr(settings, "port", getattr(settings, "webui_port", 1128))), proxy_headers=False)


if __name__ == "__main__":
    main()
