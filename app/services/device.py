"""Device-state polling and on-device helpers.

Lives in services/ so the route module only owns the HTTP handler. The poller
is scheduled from main.py's lifespan.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.config import Settings
from app.services.mtp_helper import DetectResult, Device
from app.state import DeviceState

log = logging.getLogger(__name__)


# Host's USB sysfs root. Module-level so tests can monkeypatch it.
_SYSFS_USB_DEVICES = Path("/sys/bus/usb/devices")


def _read_sysfs_str(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def _detect_kindle_via_sysfs(usb_ids: list[str]) -> Device | None:
    """Return a ``Device`` descriptor for the first USB device on the host bus
    matching one of the configured ``VID:PID`` pairs, or ``None``. Pure sysfs
    read — never opens an MTP session.

    Why sysfs and not ``mtp_helper.detect()``: the jailbroken MTP-only firmware
    on the Kindle Paperwhite Signature Edition treats an MTP open/close as a
    signal that the host is done with the device and exits USB mode, dropping
    the device off the bus. ``detect_managed_devices()`` opens MTP under the
    hood, so polling presence that way triggers a perpetual disconnect/
    reconnect cycle (see commit history around the device service). Sysfs
    presence is cheap, side-effect-free, and safe to call every tick.

    Empty ``usb_ids`` returns ``None`` — without a filter we cannot distinguish
    the Kindle from any other USB device. Operators must set
    ``CALIBRE_WEB_CLI_MTP_USB_IDS`` for device detection to work.
    """
    if not usb_ids:
        return None
    targets: set[tuple[str, str]] = set()
    for item in usb_ids:
        item = item.strip().lower()
        if ":" not in item:
            continue
        vid, pid = item.split(":", 1)
        targets.add((vid.zfill(4), pid.zfill(4)))
    if not targets:
        return None
    try:
        for entry in _SYSFS_USB_DEVICES.iterdir():
            vid_file = entry / "idVendor"
            pid_file = entry / "idProduct"
            if not (vid_file.is_file() and pid_file.is_file()):
                continue
            vid = vid_file.read_text().strip().lower()
            pid = pid_file.read_text().strip().lower()
            if (vid, pid) not in targets:
                continue
            manuf = _read_sysfs_str(entry / "manufacturer")
            prod = _read_sysfs_str(entry / "product")
            name = f"{manuf} {prod}".strip() or f"{vid}:{pid}"
            return Device(name=name, vid=vid, pid=pid)
    except OSError as exc:
        log.debug("sysfs USB scan failed: %s", exc)
    return None


async def _poll_tick(settings: Settings, state: DeviceState) -> None:
    """One iteration of the device-state poll. Extracted from the loop so tests
    can drive it directly without orchestrating ``asyncio.sleep``.

    Presence-only contract: this function NEVER opens MTP. Empirically, even
    a single ``mtp_helper.list_files()`` from inside the container is enough
    to drop the jailbroken Kindle MTP firmware off the USB bus for minutes,
    sometimes indefinitely (the previous "list once per session" attempt at
    f83b48f confirmed this). The poller's job here is reduced to:

    * synthesize ``state.detect`` from sysfs (truth-of-presence)
    * preserve ``state.on_device_filenames`` across ticks while the device is
      present — that cache is populated **only** by the optimistic add/discard
      in :mod:`app.handlers` after a user-initiated send/remove.

    ``CancelledError`` is re-raised so shutdown still works. Any other
    exception is caught and recorded so one bad tick can't kill the loop.
    """
    try:
        detected = _detect_kindle_via_sysfs(settings.mtp_usb_ids)
        if detected is None:
            if state.detect is not None:
                log.debug("device left USB bus; clearing detect state")
            state.detect = None
            state.on_device_filenames = set()
            state.last_detect_error = None
            return
        # Device on bus — synthesize the connected state from sysfs. No MTP, ever.
        state.detect = DetectResult(connected=True, device=detected)
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
    """Watch for device presence by polling sysfs.

    The poller never opens MTP — see :func:`_poll_tick` and
    :func:`_detect_kindle_via_sysfs` for why. The on-device filename cache is
    maintained by :mod:`app.handlers` (optimistic update after send/remove).
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
