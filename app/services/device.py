"""Device-state polling and on-device helpers.

Lives in services/ so the route module only owns the HTTP handler. The poller
is scheduled from main.py's lifespan.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.config import Settings
from app.services import mtp_helper
from app.state import DeviceState

log = logging.getLogger(__name__)


async def poll_device_loop(settings: Settings, state: DeviceState, interval: float = 5.0) -> None:
    """Run continuously while the app is alive.

    Refreshes the device DetectResult and, on every connect/poll-after-connect,
    rebuilds the on-device filename cache used to render badges. Any unexpected
    exception is caught and logged so the loop keeps ticking — a poller that
    silently dies leaves the UI showing stale device state forever.
    """
    while True:
        try:
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
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("device poller tick failed; continuing")
            state.detect = None
            state.on_device_filenames = set()
        await asyncio.sleep(interval)


def books_on_device(state: DeviceState, books) -> set[int]:
    """Compute the set of book ids whose filenames currently sit on the device.

    Pure in-memory check: relies on the format_filenames map populated by the
    batched DB query in list_books — no DB hits, no stat() calls.
    """
    if not state.on_device_filenames:
        return set()
    wanted = state.on_device_filenames
    return {b.id for b in books if any(name in wanted for name in b.format_filenames.values())}
