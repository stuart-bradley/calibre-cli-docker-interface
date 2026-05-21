"""MTP helper for the connected e-reader.

In-process libmtp client routed through a small ``_Backend`` protocol. The
production backend (``_CtypesBackend``) drives ``libmtp.so.9`` directly via
ctypes; tests inject an in-memory fake. Both ``calibre-debug`` and
``mtp-tools`` were tried first and proved insufficient on this device:
Calibre's MTP wrapper silently returned success for ``put_file`` without
actually transferring the book bytes, and ``mtp-sendfile`` ignores both the
remote filename argument and any parent folder — every file lands at storage
root with the local basename.

Threading: each verb runs its blocking backend calls on a worker thread via
``asyncio.to_thread``. A module-level ``asyncio.Lock`` serialises calls so
two operations never share the USB bus, which the jailbroken Kindle
Paperwhite Signature Edition firmware does not tolerate.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from ctypes import (
    CDLL,
    POINTER,
    Structure,
    byref,
    c_char_p,
    c_int,
    c_long,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The Kindle Paperwhite Signature Edition (jailbroken MTP-only firmware)
# exposes exactly one storage, with id 0x00010001. We don't enumerate
# storages dynamically; if a future device exposes multiple, revisit.
_DEFAULT_STORAGE_ID = 0x00010001
_DOCUMENTS_FOLDER_NAME = "documents"
_SYSTEM_FOLDER_NAME = "system"
_THUMBNAILS_FOLDER_NAME = "thumbnails"

# Sentinel ``parent_id`` for ``list_folder`` meaning "root of storage".
# libmtp accepts this value where the PTP wire-protocol uses 0xFFFFFFFF;
# passing 0 instead returns a full recursive listing on this firmware
# (~50 entries) rather than the 9 top-level entries.
_MTP_PARENT_ROOT = 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawDevice:
    """Identity of an MTP device returned by ``Backend.detect``. ``handle``
    is opaque to ``mtp_helper`` — the backend stores any state it needs to
    later ``open`` this device (e.g. the ctypes backend keeps the raw-device
    pointer plus its parent array)."""

    vendor_id: int
    product_id: int
    handle: object


@dataclass(frozen=True)
class _Entry:
    """One node returned by ``Backend.list_folder``."""

    item_id: int
    parent_id: int
    filename: str
    filesize: int
    is_folder: bool


class _Backend(Protocol):
    def detect(self) -> list[_RawDevice]: ...
    def open(self, raw: _RawDevice) -> object: ...
    def close(self, dev: object) -> None: ...
    def manufacturer(self, dev: object) -> str: ...
    def model(self, dev: object) -> str: ...
    def list_folder(
        self, dev: object, storage_id: int, parent_id: int
    ) -> list[_Entry]: ...
    def send_file(
        self,
        dev: object,
        local_path: str,
        storage_id: int,
        parent_id: int,
        dest_name: str,
    ) -> int: ...
    def delete(self, dev: object, item_id: int) -> None: ...


# Module-level slot for the active backend. Lazily constructed to
# ``_CtypesBackend()`` on first ``_get_backend()`` call. Tests override
# via ``monkeypatch.setattr(mtp_helper, "_backend", fake)``.
_backend: _Backend | None = None
_backend_lock = threading.Lock()


def _get_backend() -> _Backend:
    global _backend
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is None:
            _backend = _CtypesBackend()
    return _backend


# ---------------------------------------------------------------------------
# Ctypes backend — drives libmtp.so.9 directly
# ---------------------------------------------------------------------------


# Values from libmtp 1.1.20 ``LIBMTP_filetype_t`` (src/libmtp.h.in). FOLDER is
# first so it's 0; UNKNOWN is last in the enum at 44. We send books with
# UNKNOWN so libmtp infers the type from the file extension — the Kindle
# accepts EPUB/AZW3/MOBI/PDF through that path.
_FILETYPE_FOLDER = 0
_FILETYPE_UNKNOWN = 44


class _LibMTPFile(Structure):
    pass


_LibMTPFile._fields_ = [
    ("item_id", c_uint32),
    ("parent_id", c_uint32),
    ("storage_id", c_uint32),
    ("filename", c_char_p),
    ("filesize", c_uint64),
    ("modificationdate", c_long),
    ("filetype", c_int),
    ("next", POINTER(_LibMTPFile)),
]


class _LibMTPDeviceEntry(Structure):
    _fields_ = [
        ("vendor", c_char_p),
        ("vendor_id", c_uint16),
        ("product", c_char_p),
        ("product_id", c_uint16),
        ("device_flags", c_uint32),
    ]


class _LibMTPRawDevice(Structure):
    _fields_ = [
        ("device_entry", _LibMTPDeviceEntry),
        ("bus_location", c_uint32),
        ("devnum", c_uint8),
    ]


class _CtypesBackend:
    """Real backend. Loads libmtp.so.9 on construction. Holds the raw-device
    array between ``detect`` and ``open`` so the pointer the ``Open`` call
    needs stays alive."""

    def __init__(self) -> None:
        libmtp = CDLL("libmtp.so.9")
        libc = CDLL("libc.so.6")

        libmtp.LIBMTP_Init.argtypes = []
        libmtp.LIBMTP_Init.restype = None

        libmtp.LIBMTP_Detect_Raw_Devices.argtypes = [
            POINTER(POINTER(_LibMTPRawDevice)),
            POINTER(c_int),
        ]
        libmtp.LIBMTP_Detect_Raw_Devices.restype = c_int

        libmtp.LIBMTP_Open_Raw_Device_Uncached.argtypes = [POINTER(_LibMTPRawDevice)]
        libmtp.LIBMTP_Open_Raw_Device_Uncached.restype = c_void_p

        libmtp.LIBMTP_Release_Device.argtypes = [c_void_p]
        libmtp.LIBMTP_Release_Device.restype = None

        libmtp.LIBMTP_Get_Manufacturername.argtypes = [c_void_p]
        libmtp.LIBMTP_Get_Manufacturername.restype = c_char_p

        libmtp.LIBMTP_Get_Modelname.argtypes = [c_void_p]
        libmtp.LIBMTP_Get_Modelname.restype = c_char_p

        libmtp.LIBMTP_Get_Files_And_Folders.argtypes = [c_void_p, c_uint32, c_uint32]
        libmtp.LIBMTP_Get_Files_And_Folders.restype = POINTER(_LibMTPFile)

        libmtp.LIBMTP_destroy_file_t.argtypes = [POINTER(_LibMTPFile)]
        libmtp.LIBMTP_destroy_file_t.restype = None

        libmtp.LIBMTP_Send_File_From_File.argtypes = [
            c_void_p,
            c_char_p,
            POINTER(_LibMTPFile),
            c_void_p,
            c_void_p,
        ]
        libmtp.LIBMTP_Send_File_From_File.restype = c_int

        libmtp.LIBMTP_Delete_Object.argtypes = [c_void_p, c_uint32]
        libmtp.LIBMTP_Delete_Object.restype = c_int

        libmtp.LIBMTP_Clear_Errorstack.argtypes = [c_void_p]
        libmtp.LIBMTP_Clear_Errorstack.restype = None

        libc.free.argtypes = [c_void_p]
        libc.free.restype = None

        libmtp.LIBMTP_Init()

        self._libmtp = libmtp
        self._libc = libc

    def detect(self) -> list[_RawDevice]:
        raw_ptr = POINTER(_LibMTPRawDevice)()
        count = c_int(0)
        err = self._libmtp.LIBMTP_Detect_Raw_Devices(byref(raw_ptr), byref(count))
        if err != 0:
            raise MTPHelperError(f"LIBMTP_Detect_Raw_Devices error {err}")
        if count.value == 0 or not raw_ptr:
            return []
        out: list[_RawDevice] = []
        for i in range(count.value):
            entry = raw_ptr[i].device_entry
            # ``handle`` keeps a reference both to the array and the index so
            # ``open`` can pass ``byref(raw_ptr[idx])`` to libmtp. We can't
            # ``libc.free(raw_ptr)`` until all RawDevices from this batch are
            # closed; tying lifetime to the RawDevice instances accomplishes
            # this via Python refcount.
            out.append(
                _RawDevice(
                    vendor_id=int(entry.vendor_id),
                    product_id=int(entry.product_id),
                    handle=(raw_ptr, i),
                )
            )
        return out

    def open(self, raw: _RawDevice) -> object:
        # ``LIBMTP_Open_Raw_Device_Uncached`` is used because
        # ``LIBMTP_Get_Files_And_Folders`` (used for both folder lookup and
        # file listing) refuses to run on a cached handle ("tried to use
        # LIBMTP_Get_Files_And_Folders on a cached device!"). With the
        # uncached open libmtp issues fresh GetObjectHandles requests per
        # folder rather than walking an in-memory tree, which is exactly
        # what we want.
        raw_ptr, idx = raw.handle  # type: ignore[misc]
        dev = self._libmtp.LIBMTP_Open_Raw_Device_Uncached(byref(raw_ptr[idx]))
        if not dev:
            raise MTPHelperError(
                "LIBMTP_Open_Raw_Device_Uncached returned NULL for "
                f"{raw.vendor_id:04x}:{raw.product_id:04x}"
            )
        return dev

    def close(self, dev: object) -> None:
        self._libmtp.LIBMTP_Release_Device(dev)

    def manufacturer(self, dev: object) -> str:
        s = self._libmtp.LIBMTP_Get_Manufacturername(dev) or b""
        return s.decode(errors="replace")

    def model(self, dev: object) -> str:
        s = self._libmtp.LIBMTP_Get_Modelname(dev) or b""
        return s.decode(errors="replace")

    def list_folder(
        self, dev: object, storage_id: int, parent_id: int
    ) -> list[_Entry]:
        head = self._libmtp.LIBMTP_Get_Files_And_Folders(
            dev, c_uint32(storage_id), c_uint32(parent_id)
        )
        try:
            out: list[_Entry] = []
            node_ptr = head
            while node_ptr:
                node = node_ptr.contents
                fname = (node.filename or b"").decode(errors="replace")
                out.append(
                    _Entry(
                        item_id=int(node.item_id),
                        parent_id=int(node.parent_id),
                        filename=fname,
                        filesize=int(node.filesize),
                        is_folder=(node.filetype == _FILETYPE_FOLDER),
                    )
                )
                node_ptr = node.next
            return out
        finally:
            if head:
                self._libmtp.LIBMTP_destroy_file_t(head)

    def send_file(
        self,
        dev: object,
        local_path: str,
        storage_id: int,
        parent_id: int,
        dest_name: str,
    ) -> int:
        size = os.path.getsize(local_path)
        f = _LibMTPFile()
        f.item_id = 0
        f.parent_id = parent_id
        f.storage_id = storage_id
        f.filename = dest_name.encode()
        f.filesize = size
        f.modificationdate = 0
        f.filetype = _FILETYPE_UNKNOWN
        f.next = POINTER(_LibMTPFile)()
        ret = self._libmtp.LIBMTP_Send_File_From_File(
            dev, local_path.encode(), byref(f), None, None
        )
        if ret != 0:
            self._libmtp.LIBMTP_Clear_Errorstack(dev)
            raise MTPHelperError(f"LIBMTP_Send_File_From_File returned {ret}")
        if f.item_id == 0:
            raise MTPHelperError("send returned success but no item_id was assigned")
        return int(f.item_id)

    def delete(self, dev: object, item_id: int) -> None:
        ret = self._libmtp.LIBMTP_Delete_Object(dev, c_uint32(item_id))
        if ret != 0:
            self._libmtp.LIBMTP_Clear_Errorstack(dev)
            raise MTPHelperError(f"LIBMTP_Delete_Object returned {ret}")


# ---------------------------------------------------------------------------
# Device session
# ---------------------------------------------------------------------------


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
        out.add((vid.zfill(4), pid.zfill(4)))
    return out


@contextmanager
def _open_device() -> Iterator[tuple[object, str, str, str]]:
    """Open the first MTP device matching the USB ID filter.

    Yields ``(handle, vid, pid, friendly_name)``. The handle is released on
    exit. Raises ``MTPHelperError`` if no device matches the filter, the
    backend errors out, or the open returns NULL.
    """
    backend = _get_backend()
    raws = backend.detect()
    if not raws:
        raise MTPHelperError("no MTP devices found")
    wanted = _parse_usb_id_filter()
    chosen: _RawDevice | None = None
    chosen_vid = ""
    chosen_pid = ""
    for r in raws:
        vid = format(r.vendor_id & 0xFFFF, "04x")
        pid = format(r.product_id & 0xFFFF, "04x")
        if wanted and (vid, pid) not in wanted:
            continue
        chosen = r
        chosen_vid = vid
        chosen_pid = pid
        break
    if chosen is None:
        raise MTPHelperError(
            f"no MTP device matched USB ID filter {sorted(wanted)!r}"
        )
    dev = backend.open(chosen)
    try:
        manuf = backend.manufacturer(dev) or ""
        model = backend.model(dev) or ""
        name = (manuf + " " + model).strip() or f"{chosen_vid}:{chosen_pid}"
        yield dev, chosen_vid, chosen_pid, name
    finally:
        backend.close(dev)


def _find_folder_id(dev: object, parent_id: int, name: str) -> int | None:
    """Return the id of the first folder named ``name`` under ``parent_id``.

    Case-insensitive match. The MTP folder ids vary between devices (and
    after a factory reset), so callers resolve on every operation rather
    than caching.
    """
    backend = _get_backend()
    for e in backend.list_folder(dev, _DEFAULT_STORAGE_ID, parent_id):
        if e.is_folder and e.filename.lower() == name.lower():
            return e.item_id
    return None


def _find_documents_folder_id(dev: object) -> int | None:
    return _find_folder_id(dev, _MTP_PARENT_ROOT, _DOCUMENTS_FOLDER_NAME)


def _find_thumbnails_folder_id(dev: object) -> int | None:
    """Walk root → ``system`` → ``thumbnails``.

    This is the folder the Kindle firmware reads to render library tile
    covers (see kindle-cover-investigation memory). Calibre Desktop's
    KINDLE driver writes ``thumbnail_<UUID>_<CDE_TYPE>_portrait.jpg`` here
    after each book send; without it, sideloaded books show a blank tile
    on this jailbroken-firmware Paperwhite Signature Edition.
    """
    sys_id = _find_folder_id(dev, _MTP_PARENT_ROOT, _SYSTEM_FOLDER_NAME)
    if sys_id is None:
        return None
    return _find_folder_id(dev, sys_id, _THUMBNAILS_FOLDER_NAME)


# ---------------------------------------------------------------------------
# Blocking verb implementations (run on a worker thread)
# ---------------------------------------------------------------------------


def _detect_blocking() -> DetectResult:
    try:
        with _open_device() as (_dev, vid, pid, name):
            return DetectResult(
                connected=True, device=Device(name=name, vid=vid, pid=pid)
            )
    except MTPHelperError:
        return DetectResult(connected=False, device=None)


def _list_blocking() -> list[FileEntry]:
    backend = _get_backend()
    with _open_device() as (dev, _vid, _pid, _name):
        folder_id = _find_documents_folder_id(dev)
        if folder_id is None:
            return []
        return [
            FileEntry(path=f"documents/{e.filename}", size=e.filesize)
            for e in backend.list_folder(dev, _DEFAULT_STORAGE_ID, folder_id)
            if not e.is_folder
        ]


def _send_blocking(local_path: str, dest_name: str) -> str:
    if not os.path.isfile(local_path):
        raise MTPHelperError(f"local file does not exist: {local_path!r}")
    backend = _get_backend()
    with _open_device() as (dev, _vid, _pid, _name):
        folder_id = _find_documents_folder_id(dev)
        if folder_id is None:
            raise MTPHelperError("device has no 'documents' folder")
        backend.send_file(dev, local_path, _DEFAULT_STORAGE_ID, folder_id, dest_name)
        return f"documents/{dest_name}"


def _remove_blocking(dest_name: str) -> None:
    backend = _get_backend()
    with _open_device() as (dev, _vid, _pid, _name):
        folder_id = _find_documents_folder_id(dev)
        if folder_id is None:
            raise MTPHelperError("device has no 'documents' folder")
        target_id: int | None = None
        for e in backend.list_folder(dev, _DEFAULT_STORAGE_ID, folder_id):
            if not e.is_folder and e.filename == dest_name:
                target_id = e.item_id
                break
        if target_id is None:
            raise MTPHelperError(f"documents/{dest_name} not found on device")
        backend.delete(dev, target_id)


def _send_thumbnail_blocking(local_path: str, dest_name: str) -> str:
    """Upload a sidecar thumbnail JPEG to ``system/thumbnails/``.

    Before uploading the new file, any pre-existing entry with the same
    canonical name OR the firmware's ``.tmp.partial`` sentinel for the
    same UUID is deleted. The sentinel is a 0-byte file the firmware
    creates when it tries and fails to extract a thumbnail from a
    sideloaded MOBI — without removing it, the firmware ignores
    subsequent attempts to render a cover for that book.
    """
    if not os.path.isfile(local_path):
        raise MTPHelperError(f"local file does not exist: {local_path!r}")
    backend = _get_backend()
    with _open_device() as (dev, _vid, _pid, _name):
        thumb_folder_id = _find_thumbnails_folder_id(dev)
        if thumb_folder_id is None:
            raise MTPHelperError("device has no 'system/thumbnails' folder")
        partial_name = f"{dest_name}.tmp.partial"
        for e in backend.list_folder(dev, _DEFAULT_STORAGE_ID, thumb_folder_id):
            if e.is_folder:
                continue
            if e.filename in (dest_name, partial_name):
                # Best-effort cleanup; if the delete fails we still try the
                # upload below — overwriting the canonical file works on
                # this firmware.
                with suppress(MTPHelperError):
                    backend.delete(dev, e.item_id)
        backend.send_file(
            dev, local_path, _DEFAULT_STORAGE_ID, thumb_folder_id, dest_name
        )
        return f"system/thumbnails/{dest_name}"


def _remove_thumbnail_blocking(dest_name: str, *, ignore_missing: bool) -> bool:
    """Delete a thumbnail and its ``.tmp.partial`` sibling, if either exists.

    Returns ``True`` if at least one object was deleted. With
    ``ignore_missing=False`` raises if neither file is found.
    """
    backend = _get_backend()
    with _open_device() as (dev, _vid, _pid, _name):
        thumb_folder_id = _find_thumbnails_folder_id(dev)
        if thumb_folder_id is None:
            if ignore_missing:
                return False
            raise MTPHelperError("device has no 'system/thumbnails' folder")
        partial_name = f"{dest_name}.tmp.partial"
        targets: list[int] = []
        for e in backend.list_folder(dev, _DEFAULT_STORAGE_ID, thumb_folder_id):
            if not e.is_folder and e.filename in (dest_name, partial_name):
                targets.append(e.item_id)
        if not targets:
            if ignore_missing:
                return False
            raise MTPHelperError(f"system/thumbnails/{dest_name} not found on device")
        deleted_any = False
        for iid in targets:
            try:
                backend.delete(dev, iid)
                deleted_any = True
            except MTPHelperError:
                pass
        return deleted_any


# ---------------------------------------------------------------------------
# Async API
# ---------------------------------------------------------------------------

_lock = asyncio.Lock()


async def detect() -> DetectResult:
    async with _lock:
        return await asyncio.to_thread(_detect_blocking)


async def list_files() -> list[FileEntry]:
    async with _lock:
        return await asyncio.to_thread(_list_blocking)


async def send(local_path: Path, dest_name: str) -> str:
    async with _lock:
        return await asyncio.to_thread(_send_blocking, str(local_path), dest_name)


async def remove(dest_name: str) -> None:
    async with _lock:
        await asyncio.to_thread(_remove_blocking, dest_name)


async def send_thumbnail(local_path: Path, dest_name: str) -> str:
    """Upload ``local_path`` to ``system/thumbnails/<dest_name>``.

    ``dest_name`` is the canonical sidecar name — typically
    ``thumbnail_<UUID>_<CDE_TYPE>_portrait.jpg``. Any existing entry with
    the same name, or the firmware's ``.tmp.partial`` sentinel for that
    UUID, is deleted first.
    """
    async with _lock:
        return await asyncio.to_thread(_send_thumbnail_blocking, str(local_path), dest_name)


async def remove_thumbnail(dest_name: str, *, ignore_missing: bool = True) -> bool:
    """Delete ``system/thumbnails/<dest_name>`` and its ``.tmp.partial``.

    Returns ``True`` if anything was deleted. ``ignore_missing`` controls
    whether a missing target is silently ignored (default) or raised.
    """
    async with _lock:
        return await asyncio.to_thread(
            _remove_thumbnail_blocking, dest_name, ignore_missing=ignore_missing
        )
