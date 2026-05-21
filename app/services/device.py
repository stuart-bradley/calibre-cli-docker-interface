"""Device-state polling and on-device helpers.

Lives in services/ so the route module only owns the HTTP handler. The poller
is scheduled from main.py's lifespan.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from app.config import Settings
from app.services import mtp_helper
from app.services.mtp_helper import DetectResult, Device
from app.state import DeviceState

log = logging.getLogger(__name__)


# Host's USB sysfs root. Module-level so tests can monkeypatch it.
_SYSFS_USB_DEVICES = Path("/sys/bus/usb/devices")

# Backoff for the per-connection on-device file sync. First attempt fires
# immediately on detection; entries here are the wait before attempts 2 and
# 3 respectively. After _SYNC_MAX_ATTEMPTS failed attempts we give up for
# the session (terminal state — replug to reset).
_SYNC_BACKOFF_SECONDS: tuple[float, ...] = (30.0, 300.0)
_SYNC_MAX_ATTEMPTS = 3


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


async def _sync_filenames(state: DeviceState) -> None:
    """Populate ``state.on_device_filenames`` from a single MTP listing.

    Spawned by :func:`_poll_tick` as a background task on each device-connect
    event. Merges results into the existing set rather than overwriting so
    optimistic entries the handlers added between the sync start and finish
    aren't dropped. On failure, schedules a retry via the backoff table; on
    the third consecutive failure, marks the session terminal (replug to
    reset).
    """
    try:
        entries = await mtp_helper.list_files()
        names = {Path(e.path).name for e in entries}
        state.on_device_filenames |= names
        state.files_synced = True
        state.sync_attempts = 0
        log.info("synced %d on-device filenames from MTP", len(names))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        state.sync_attempts += 1
        if state.sync_attempts >= _SYNC_MAX_ATTEMPTS:
            state.files_synced = True
            log.warning(
                "on-device filename sync failed %d times; giving up for this session: %s",
                state.sync_attempts,
                exc,
            )
        else:
            delay = _SYNC_BACKOFF_SECONDS[state.sync_attempts - 1]
            state.next_sync_at = time.monotonic() + delay
            log.warning(
                "on-device filename sync attempt %d failed; retrying in %.0fs: %s",
                state.sync_attempts,
                delay,
                exc,
            )
    finally:
        state.sync_in_progress = False


def _reset_sync_state(state: DeviceState) -> None:
    state.on_device_filenames = set()
    state.files_synced = False
    state.sync_attempts = 0
    state.next_sync_at = 0.0


async def _poll_tick(settings: Settings, state: DeviceState) -> None:
    """One iteration of the device-state poll. Extracted from the loop so tests
    can drive it directly without orchestrating ``asyncio.sleep``.

    Two responsibilities:

    * synthesize ``state.detect`` from sysfs every tick — cheap and
      side-effect-free; safe even on the jailbroken Kindle firmware that
      treats MTP opens as a "host is done" signal.
    * trigger a one-shot MTP file listing on each device-connect event, via
      :func:`_sync_filenames`. Listing is gated on ``files_synced`` (terminal
      for the session), ``sync_in_progress`` (no overlap), and
      ``next_sync_at`` (backoff). The sync runs as a fire-and-forget task so
      the tick stays fast; the connection indicator updates within one tick
      regardless of how long the listing takes.

    ``CancelledError`` is re-raised so shutdown still works. Any other
    exception is caught and recorded so one bad tick can't kill the loop.
    """
    try:
        detected = _detect_kindle_via_sysfs(settings.mtp_usb_ids)
        if detected is None:
            if state.detect is not None:
                log.debug("device left USB bus; clearing detect state")
            state.detect = None
            _reset_sync_state(state)
            state.last_detect_error = None
            return
        state.detect = DetectResult(connected=True, device=detected)
        if (
            not state.files_synced
            and not state.sync_in_progress
            and time.monotonic() >= state.next_sync_at
        ):
            state.sync_in_progress = True
            asyncio.create_task(_sync_filenames(state))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("device poller tick failed; continuing")
        state.detect = None
        _reset_sync_state(state)
        state.last_detect_error = f"{type(exc).__name__}: {exc}"
    finally:
        state.has_polled = True


async def poll_device_loop(settings: Settings, state: DeviceState, interval: float = 5.0) -> None:
    """Watch for device presence by polling sysfs.

    Every tick: sysfs presence check (cheap) plus, on the connect transition,
    a one-shot MTP file listing in the background to seed the on-device
    filename cache. See :func:`_poll_tick` and :func:`_sync_filenames` for
    the contract.
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
