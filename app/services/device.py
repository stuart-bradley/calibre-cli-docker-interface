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


# Host's USB sysfs root. Module-level so tests can monkeypatch it.
_SYSFS_USB_DEVICES = Path("/sys/bus/usb/devices")


def _kindle_on_bus(usb_ids: list[str]) -> bool:
    """Return True if any configured ``VID:PID`` pair is currently enumerated
    on the host's USB bus. Reads sysfs directly — does not open an MTP session.

    Why: some Kindle MTP firmwares (notably the jailbroken MTP-only build on
    the Paperwhite Signature Edition) treat an MTP open/close as a signal that
    the host is done and immediately exit USB mode, dropping the device off
    the bus. Polling presence via ``mtp_helper.detect()`` was the trigger,
    because ``detect_managed_devices()`` opens MTP under the hood. A pure
    sysfs check confirms presence cheaply and is safe to call every tick.

    If ``usb_ids`` is empty, returns True so the caller falls back to the
    previous always-poll behaviour (sysfs filtering is opt-in via
    ``CALIBRE_WEB_CLI_MTP_USB_IDS``).
    """
    if not usb_ids:
        return True
    targets: set[tuple[str, str]] = set()
    for item in usb_ids:
        item = item.strip().lower()
        if ":" not in item:
            continue
        vid, pid = item.split(":", 1)
        targets.add((vid.zfill(4), pid.zfill(4)))
    if not targets:
        return True
    try:
        for entry in _SYSFS_USB_DEVICES.iterdir():
            vid_file = entry / "idVendor"
            pid_file = entry / "idProduct"
            if not (vid_file.is_file() and pid_file.is_file()):
                continue
            vid = vid_file.read_text().strip().lower()
            pid = pid_file.read_text().strip().lower()
            if (vid, pid) in targets:
                return True
    except OSError as exc:
        log.debug("sysfs USB scan failed: %s", exc)
        return False
    return False


async def _poll_tick(settings: Settings, state: DeviceState) -> None:
    """One iteration of the device-state poll. Extracted from the loop so tests
    can drive it directly without orchestrating ``asyncio.sleep``.

    Any unexpected exception is caught and recorded so the surrounding loop
    keeps ticking — a poller that silently dies leaves the UI showing stale
    state forever. ``CancelledError`` is re-raised so shutdown still works.
    """
    try:
        on_bus = _kindle_on_bus(settings.mtp_usb_ids)
        if not on_bus:
            if state.detect is not None:
                log.debug("device left USB bus; clearing detect state")
            state.detect = None
            state.on_device_filenames = set()
            state.last_detect_error = None
        elif state.detect is None or not state.detect.connected:
            # Transition (or recovery from a prior error): open MTP once to
            # confirm MTP capability and populate the on-device filename
            # cache used to render "on-device" badges.
            try:
                detect = await mtp_helper.detect()
            except mtp_helper.MTPHelperError as exc:
                log.debug("mtp detect failed: %s", exc)
                state.detect = None
                state.on_device_filenames = set()
                state.last_detect_error = str(exc)
            else:
                state.detect = detect
                state.last_detect_error = None
                if detect.connected:
                    try:
                        files = await mtp_helper.list_files()
                        state.on_device_filenames = {Path(f.path).name for f in files}
                    except mtp_helper.MTPHelperError as exc:
                        log.debug("mtp list failed: %s", exc)
                        state.on_device_filenames = set()
                else:
                    state.on_device_filenames = set()
        # else: on_bus AND state.detect.connected — steady state, skip MTP.
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("device poller tick failed; continuing")
        state.detect = None
        state.on_device_filenames = set()
        state.last_detect_error = f"{type(exc).__name__}: {exc}"
    finally:
        state.has_polled = True


async def poll_device_loop(settings: Settings, state: DeviceState, interval: float = 5.0) -> None:
    """Watch for device presence and refresh on-device state on transitions.

    Steady-state behaviour avoids repeatedly opening MTP sessions: a sysfs
    USB-presence check runs every tick, and ``mtp_helper.detect()`` /
    ``list_files()`` only run on the disconnect→connect transition (or when
    the helper previously errored). See :func:`_kindle_on_bus` for why.
    """
    while True:
        await _poll_tick(settings, state)
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
