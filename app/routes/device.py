from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import Settings
from app.services import db, mtp_helper
from app.state import DeviceState

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/device/status", response_class=HTMLResponse)
def device_status(request: Request):
    from app.main import templates

    return templates.TemplateResponse(
        request,
        "_fragments/device_status.html",
        {"device": request.app.state.device_state.detect},
    )


async def poll_device_loop(settings: Settings, state: DeviceState, interval: float = 5.0) -> None:
    """Run continuously while the app is alive.

    Refreshes the device DetectResult and, on every connect/poll-after-connect,
    rebuilds the on-device filename cache used to render badges.
    """
    while True:
        try:
            detect = await mtp_helper.detect()
        except mtp_helper.MTPHelperError as exc:
            log.debug("mtp detect failed: %s", exc)
            state.detect = None
            state.on_device_filenames = set()
        else:
            state.detect = detect
            if detect.connected:
                try:
                    files = await mtp_helper.list_files()
                    state.on_device_filenames = {Path(f.path).name for f in files}
                except mtp_helper.MTPHelperError as exc:
                    log.debug("mtp list failed: %s", exc)
                    state.on_device_filenames = set()
            else:
                state.on_device_filenames = set()
        await asyncio.sleep(interval)


def is_book_on_device(state: DeviceState, library_path: Path, book_id: int) -> bool:
    book = db.get_book(library_path, book_id)
    if book is None or not state.on_device_filenames:
        return False
    for fmt in book.formats:
        path = db.get_format_path(library_path, book_id, fmt)
        if path and path.name in state.on_device_filenames:
            return True
    return False
