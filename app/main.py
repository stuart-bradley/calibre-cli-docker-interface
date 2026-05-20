from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.handlers import register_handlers
from app.routes import cover as cover_route
from app.routes import device as device_route
from app.routes import health as health_route
from app.routes import jobs as jobs_route
from app.routes import library as library_route
from app.routes import upload as upload_route
from app.services.auth import BasicAuthMiddleware
from app.services.worker import Worker
from app.state import DeviceState

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    worker = Worker()
    register_handlers(worker, settings)
    device_state = DeviceState()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker.start()
        poll_task = asyncio.create_task(
            device_route.poll_device_loop(settings, device_state),
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


# Module-level `app` for `uvicorn app.main:app` invocation.
try:
    app = create_app()
except Exception:
    # During test collection settings may be unavailable; create_app called by fixture.
    app = None  # type: ignore[assignment]
