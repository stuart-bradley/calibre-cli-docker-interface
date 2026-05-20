"""MTP helper for the connected e-reader.

Two modes:

* **CLI mode** — invoked as `calibre-debug -e mtp_helper.py <verb> [args]`. Runs
  the verb against Calibre's bundled ``calibre.devices.mtp`` and prints a
  single JSON object to stdout. Per-operation errors are reported in the JSON
  (``{"ok": false, "error": "..."}``). Non-zero exit is reserved for
  catastrophic failure (e.g. libmtp not loadable).

* **Caller mode** — importable from the FastAPI app. Provides async wrappers
  that shell out to ``calibre-debug -e`` and parse the JSON response. A module-
  level ``asyncio.Lock`` serialises calls so the 5-second status poller does
  not overlap itself when libmtp first-call init takes several seconds.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Caller (async API used by the FastAPI app)
# ---------------------------------------------------------------------------

_DEFAULT_HELPER_PATH = Path(__file__).resolve()
_lock = asyncio.Lock()


class MTPHelperError(RuntimeError):
    pass


@dataclass(frozen=True)
class Device:
    name: str
    vid: str
    pid: str


@dataclass(frozen=True)
class DetectResult:
    connected: bool
    device: Device | None


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int


async def _invoke(verb: str, *args: str, helper_path: Path | None = None) -> dict:
    helper = str(helper_path or _DEFAULT_HELPER_PATH)
    cmd = ["calibre-debug", "-e", helper, verb, *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = (stderr.decode() or stdout.decode() or "").strip()
        raise MTPHelperError(f"calibre-debug exit {proc.returncode}: {err}")
    # calibre-debug prints diagnostics around our JSON line: setup warnings
    # before, teardown chatter ("Device 0 (VID=... PID=...) is a Amazon
    # Kindle ...") after. Our _print tags its JSON with _JSON_MARKER so we
    # can extract it unambiguously. If nothing matches, surface the full
    # stdout (truncated) plus stderr so the caller sees what Calibre said.
    text = stdout.decode()
    for line in text.splitlines():
        if line.startswith(_JSON_MARKER):
            try:
                return json.loads(line[len(_JSON_MARKER) :])
            except json.JSONDecodeError as exc:
                raise MTPHelperError(f"helper marker line was not valid JSON: {line!r}") from exc
    err = stderr.decode().strip()
    stdout_excerpt = text.strip()[-500:]
    raise MTPHelperError(
        f"helper did not emit JSON marker. stderr: {err!r}; stdout tail: {stdout_excerpt!r}"
    )


async def detect(*, helper_path: Path | None = None) -> DetectResult:
    async with _lock:
        data = await _invoke("detect", helper_path=helper_path)
    dev_dict = data.get("device") or None
    device = Device(**dev_dict) if dev_dict else None
    return DetectResult(connected=bool(data.get("connected")), device=device)


async def list_files(*, helper_path: Path | None = None) -> list[FileEntry]:
    async with _lock:
        data = await _invoke("list", helper_path=helper_path)
    if not data.get("ok"):
        raise MTPHelperError(data.get("error", "list failed"))
    return [FileEntry(path=f["path"], size=f["size"]) for f in data.get("files", [])]


async def send(local_path: Path, dest_name: str, *, helper_path: Path | None = None) -> str:
    async with _lock:
        data = await _invoke("send", str(local_path), dest_name, helper_path=helper_path)
    if not data.get("ok"):
        raise MTPHelperError(data.get("error", "send failed"))
    return data["dest"]


async def remove(dest_name: str, *, helper_path: Path | None = None) -> None:
    async with _lock:
        data = await _invoke("remove", dest_name, helper_path=helper_path)
    if not data.get("ok"):
        raise MTPHelperError(data.get("error", "remove failed"))


# ---------------------------------------------------------------------------
# CLI mode (runs under `calibre-debug -e`)
# ---------------------------------------------------------------------------


_JSON_MARKER = "@@CWC_JSON@@"


def _print(payload: dict) -> None:
    # Tag the JSON line with a sentinel so the caller can pick it out of
    # Calibre's chatter — Calibre's scanner prints lines like
    # "Device 0 (VID=... and PID=...) is a ..." during teardown, after our
    # output, and other warnings can show up before it.
    sys.stdout.write(_JSON_MARKER + json.dumps(payload) + "\n")
    sys.stdout.flush()


def _parse_usb_id_filter() -> set[tuple[str, str]]:
    raw = os.environ.get("CALIBRE_WEB_CLI_MTP_USB_IDS", "").strip()
    if not raw:
        return set()
    out: set[tuple[str, str]] = set()
    for item in raw.split(","):
        item = item.strip().lower()
        if ":" not in item:
            continue
        vid, pid = item.split(":", 1)
        out.add((vid, pid))
    return out


def _build_driver():
    """Construct an MTP_DEVICE with the GUI-side attributes the headless
    constructor skips. Without ``report_progress`` and ``current_friendly_name``,
    ``detect_managed_devices()`` raises ``AttributeError`` during its internal
    ``open()`` probe and swallows it — returning ``None`` and making the UI
    report "no device" even when libmtp sees the device.

    Note: ``prefs`` is **not** set here. On calibre 9.8 it is a read-only
    property auto-populated by ``startup()`` (assigning to it raises
    "property 'prefs' has no setter"). Calling ``startup()`` is sufficient.
    """
    from calibre.devices.mtp.driver import MTP_DEVICE  # type: ignore

    drv = MTP_DEVICE(None)
    drv.startup()
    drv.report_progress = lambda *a, **k: None
    drv.current_friendly_name = None
    return drv


def _scan_and_detect(driver):
    """Return the list of MTP devices currently attached.

    Calibre's ``MTP_DEVICE.detect_managed_devices()`` returns a **single**
    ``MTPDevice`` (a namedtuple) or ``None``, not a list of devices — that's
    how the ``MANAGES_DEVICE_PRESENCE`` plugin contract works. The previous
    ``... or []`` collapsed the None case correctly but left the single
    namedtuple naked: iterating it then yielded the tuple's *field values*
    (busnum, devnum, vendor_id, ...) instead of devices, and the caller's
    ``getattr(dev, "vendor_id", 0)`` returned 0 for every integer/string item
    so the device was silently rejected. Wrap into a single-element list.
    """
    from calibre.devices.scanner import DeviceScanner  # type: ignore

    scanner = DeviceScanner()
    scanner.scan()
    found = driver.detect_managed_devices(scanner.devices)
    return [found] if found is not None else []


def _cli_detect() -> None:
    driver = _build_driver()
    connected_devs = _scan_and_detect(driver)

    id_filter = _parse_usb_id_filter()
    selected = None
    for dev in connected_devs:
        vid = format(getattr(dev, "vendor_id", 0) or 0, "04x")
        pid = format(getattr(dev, "product_id", 0) or 0, "04x")
        if id_filter and (vid, pid) not in id_filter:
            continue
        selected = (dev, vid, pid)
        break

    if selected is None:
        _print({"connected": False, "device": None})
        return

    dev, vid, pid = selected
    name = getattr(dev, "manufacturer", "") + " " + getattr(dev, "product", "")
    _print({"connected": True, "device": {"name": name.strip(), "vid": vid, "pid": pid}})


def _cli_list() -> None:
    """List files in the Kindle's ``/documents`` folder.

    Walks the cached MTP filesystem tree via ``FilesystemCache.storage(...)``
    and ``find_path``. Deliberately avoids ``MTP_DEVICE.list()``: on Calibre
    9.8 that path raises ``UnboundLocalError: cannot access local variable
    'q' where it is not associated with a value`` for this firmware (a
    Calibre internal bug). The filesystem cache is populated during
    ``driver.open``, so we just traverse it ourselves.
    """
    driver = _build_driver()
    devs = _scan_and_detect(driver)
    if not devs:
        _print({"ok": False, "error": "no device"})
        return
    driver.open(devs[0], "library")
    storage = driver.filesystem_cache.storage(driver._main_id)
    documents = storage.find_path(("documents",)) if storage is not None else None
    files: list[dict] = []
    if documents is not None:
        for f in documents.files:
            files.append({"path": "/".join(f.full_path), "size": getattr(f, "size", 0)})
    _print({"ok": True, "files": files})


def _cli_send(local: str, dest: str) -> None:
    """Upload ``local`` to the Kindle's ``/documents`` folder, named ``dest``.

    Calibre 9.8 ``MTP_DEVICE`` has no ``root(name)`` method — the previous
    code (``driver.put_file(driver.root("documents"), ...)``) failed every
    time with ``AttributeError: 'MTP_DEVICE' object has no attribute 'root'``.
    The correct way (mirroring ``MTP_DEVICE.upload_books`` internally) is:

    1. ``storage = driver.filesystem_cache.storage(driver._main_id)`` — the
       device's main storage root, populated lazily during ``driver.open``.
    2. ``parent = storage.find_path(("documents",))`` — case-insensitive folder
       lookup against the cached MTP filesystem tree.
    3. ``driver.put_file(parent, dest, stream, size)`` — the actual transfer.

    Bypasses ``upload_books`` deliberately: that requires a list of
    ``Metadata`` objects (it ``zip``\\s them with files+names) and applies
    Calibre's ``save_template`` substitution to derive the destination path.
    We already know the destination filename and folder; no templating
    needed.
    """
    driver = _build_driver()
    devs = _scan_and_detect(driver)
    if not devs:
        _print({"ok": False, "error": "no device"})
        return
    driver.open(devs[0], "library")
    storage = driver.filesystem_cache.storage(driver._main_id)
    parent = storage.find_path(("documents",))
    if parent is None:
        _print({"ok": False, "error": "device has no 'documents' folder"})
        return
    size = os.path.getsize(local)
    with open(local, "rb") as fh:
        driver.put_file(parent, dest, fh, size)
    _print({"ok": True, "dest": f"documents/{dest}"})


def _cli_remove(dest: str) -> None:
    """Remove ``documents/<dest>`` from the Kindle.

    Calibre 9.8 ``MTP_DEVICE`` has no ``delete_file`` — the previous code's
    ``driver.delete_file(f"documents/{dest}")`` would have raised
    ``AttributeError``. The supported path is to resolve the file via the
    filesystem cache and call ``recursive_delete`` on it (this is what
    ``MTP_DEVICE.delete_books`` does internally for each path).
    """
    driver = _build_driver()
    devs = _scan_and_detect(driver)
    if not devs:
        _print({"ok": False, "error": "no device"})
        return
    driver.open(devs[0], "library")
    storage = driver.filesystem_cache.storage(driver._main_id)
    target = storage.find_path(("documents", dest)) if storage is not None else None
    if target is None:
        _print({"ok": False, "error": f"documents/{dest} not found on device"})
        return
    driver.recursive_delete(target)
    _print({"ok": True})


def _main(argv: list[str]) -> int:
    if not argv:
        _print({"ok": False, "error": "missing verb"})
        return 0
    verb, *rest = argv
    try:
        if verb == "detect":
            _cli_detect()
        elif verb == "list":
            _cli_list()
        elif verb == "send":
            if len(rest) != 2:
                _print({"ok": False, "error": "send requires <local> <dest>"})
                return 0
            _cli_send(rest[0], rest[1])
        elif verb == "remove":
            if len(rest) != 1:
                _print({"ok": False, "error": "remove requires <dest>"})
                return 0
            _cli_remove(rest[0])
        else:
            _print({"ok": False, "error": f"unknown verb {verb!r}"})
    except Exception as exc:
        # Catastrophic failures (libmtp not loadable, calibre import error)
        # exit non-zero so the caller can distinguish them from per-op errors.
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
