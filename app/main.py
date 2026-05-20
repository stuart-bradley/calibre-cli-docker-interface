from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.handlers import register_handlers
from app.routes import cover as cover_route
from app.routes import device as device_route
from app.routes import health as health_route
from app.routes import jobs as jobs_route
from app.routes import library as library_route
from app.routes import upload as upload_route
from app.services.auth import BasicAuthMiddleware
from app.services.device import poll_device_loop
from app.services.worker import Worker
from app.state import DeviceState
from app.templating import STATIC_DIR

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    if not settings.mtp_usb_ids:
        log.warning(
            "CALIBRE_WEB_CLI_MTP_USB_IDS is unset — device detection disabled. "
            "Set this env var to your Kindle's VID:PID (e.g. 1949:9981) to enable."
        )

    worker = Worker()
    device_state = DeviceState()
    register_handlers(worker, settings, device_state)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        worker.start()
        poll_task = asyncio.create_task(
            poll_device_loop(settings, device_state),
            name="device-poller",
        )
        try:
            yield
        finally:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await poll_task
            await worker.stop()

    app = FastAPI(title="calibre-web-cli", lifespan=lifespan)
    app.add_middleware(BasicAuthMiddleware, password=settings.password)
    app.state.worker = worker
    app.state.device_state = device_state
    app.state.settings = settings

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(library_route.router)
    app.include_router(cover_route.router)
    app.include_router(upload_route.router)
    app.include_router(jobs_route.router)
    app.include_router(device_route.router)
    app.include_router(health_route.router)

    return app


# Module-level `app` for `uvicorn app.main:app`. Tests construct via the fixture
# in tests/routes/conftest.py and don't import this symbol — let production errors
# (missing LIBRARY_PATH, bad settings) surface loudly rather than as `app = None`.
app = create_app()
