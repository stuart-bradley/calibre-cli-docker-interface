"""MTP helper for the connected e-reader.

In-process libmtp client. Drives ``libmtp.so.9`` directly via ctypes; no
``calibre-debug`` subprocess and no ``mtp-tools``. Both prior approaches were
insufficient on this device: Calibre's MTP wrapper silently returned success
for ``put_file`` without actually transferring the book bytes, and
``mtp-sendfile`` ignores both the remote filename argument and any parent
folder — every file lands at storage root with the local basename.

Threading: each verb runs its blocking libmtp calls on a worker thread via
``asyncio.to_thread``. A module-level ``asyncio.Lock`` serialises calls so
two operations never share the USB bus, which the jailbroken Kindle
Paperwhite Signature Edition firmware does not tolerate.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
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
# libmtp ctypes layout
# ---------------------------------------------------------------------------

# Values from libmtp 1.1.20 ``LIBMTP_filetype_t`` (src/libmtp.h.in). FOLDER is
# first so it's 0; UNKNOWN is last in the enum at 44. We send books with
# UNKNOWN so libmtp infers the type from the file extension — the Kindle
# accepts EPUB/AZW3/MOBI/PDF through that path.
_FILETYPE_FOLDER = 0
_FILETYPE_UNKNOWN = 44

# The Kindle Paperwhite Signature Edition (jailbroken MTP-only firmware)
# exposes exactly one storage, with id 0x00010001. We don't enumerate
# storages dynamically; if a future device exposes multiple, revisit.
_DEFAULT_STORAGE_ID = 0x00010001
_DOCUMENTS_FOLDER_NAME = "documents"
_SYSTEM_FOLDER_NAME = "system"
_THUMBNAILS_FOLDER_NAME = "thumbnails"

# Sentinel ``parent_id`` for ``LIBMTP_Get_Files_And_Folders`` meaning "root
# of storage". libmtp accepts this value where the PTP wire-protocol uses
# 0xFFFFFFFF; passing 0 instead returns a full recursive listing on this
# firmware (~50 entries) rather than the 9 top-level entries.
_MTP_PARENT_ROOT = 0xFFFFFFFF


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


# Lazy-init so ``import mtp_helper`` works in environments without
# libmtp.so.9 (dev machines, CI). Tests monkeypatch the ``_*_blocking``
# functions and never hit ``_ensure_init``.
_libmtp: CDLL | None = None
_libc: CDLL | None = None
_init_lock = threading.Lock()


def _ensure_init() -> None:
    global _libmtp, _libc
    if _libmtp is not None:
        return
    with _init_lock:
        if _libmtp is not None:
            return
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
        _libmtp = libmtp
        _libc = libc


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
def _open_device() -> Iterator[tuple[c_void_p, str, str, str]]:
    """Open the first MTP device matching the USB ID filter.

    Yields ``(handle, vid, pid, friendly_name)``. The handle is released and
    the raw-device list freed on exit. Raises ``MTPHelperError`` if no device
    matches the filter, libmtp errors out, or the open returns NULL.

    Open semantics: ``LIBMTP_Open_Raw_Device_Uncached`` is used because
    ``LIBMTP_Get_Files_And_Folders`` (used for both folder lookup and file
    listing) refuses to run on a cached handle ("tried to use
    LIBMTP_Get_Files_And_Folders on a cached device!"). With the uncached
    open libmtp issues fresh GetObjectHandles requests per folder rather
    than walking an in-memory tree, which is exactly what we want.
    """
    _ensure_init()
    assert _libmtp is not None and _libc is not None

    raw_ptr = POINTER(_LibMTPRawDevice)()
    count = c_int(0)
    err = _libmtp.LIBMTP_Detect_Raw_Devices(byref(raw_ptr), byref(count))
    if err != 0:
        raise MTPHelperError(f"LIBMTP_Detect_Raw_Devices error {err}")
    if count.value == 0 or not raw_ptr:
        raise MTPHelperError("no MTP devices found")
    try:
        wanted = _parse_usb_id_filter()
        chosen: tuple[int, str, str] | None = None
        for i in range(count.value):
            entry = raw_ptr[i].device_entry
            vid = format(entry.vendor_id & 0xFFFF, "04x")
            pid = format(entry.product_id & 0xFFFF, "04x")
            if wanted and (vid, pid) not in wanted:
                continue
            chosen = (i, vid, pid)
            break
        if chosen is None:
            raise MTPHelperError(f"no MTP device matched USB ID filter {sorted(wanted)!r}")
        idx, vid, pid = chosen
        dev = _libmtp.LIBMTP_Open_Raw_Device_Uncached(byref(raw_ptr[idx]))
        if not dev:
            raise MTPHelperError(f"LIBMTP_Open_Raw_Device_Uncached returned NULL for {vid}:{pid}")
        try:
            manuf = _libmtp.LIBMTP_Get_Manufacturername(dev) or b""
            model = _libmtp.LIBMTP_Get_Modelname(dev) or b""
            name = (
                manuf.decode(errors="replace") + " " + model.decode(errors="replace")
            ).strip() or f"{vid}:{pid}"
            yield dev, vid, pid, name
        finally:
            _libmtp.LIBMTP_Release_Device(dev)
    finally:
        _libc.free(raw_ptr)


def _find_folder_id(dev: c_void_p, parent_id: int, name: str) -> int | None:
    """Return the id of the first folder named ``name`` under ``parent_id``.

    Case-insensitive match. The MTP folder ids vary between devices (and
    after a factory reset), so callers resolve on every operation rather
    than caching.
    """
    assert _libmtp is not None
    head = _libmtp.LIBMTP_Get_Files_And_Folders(
        dev, _DEFAULT_STORAGE_ID, c_uint32(parent_id)
    )
    try:
        node_ptr = head
        while node_ptr:
            node = node_ptr.contents
            if node.filetype == _FILETYPE_FOLDER:
                fname = (node.filename or b"").decode(errors="replace")
                if fname.lower() == name.lower():
                    return int(node.item_id)
            node_ptr = node.next
        return None
    finally:
        if head:
            _libmtp.LIBMTP_destroy_file_t(head)


def _find_documents_folder_id(dev: c_void_p) -> int | None:
    return _find_folder_id(dev, _MTP_PARENT_ROOT, _DOCUMENTS_FOLDER_NAME)


def _find_thumbnails_folder_id(dev: c_void_p) -> int | None:
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
            return DetectResult(connected=True, device=Device(name=name, vid=vid, pid=pid))
    except MTPHelperError:
        return DetectResult(connected=False, device=None)


def _list_blocking() -> list[FileEntry]:
    with _open_device() as (dev, _vid, _pid, _name):
        # _open_device ran _ensure_init, so _libmtp is set.
        assert _libmtp is not None
        folder_id = _find_documents_folder_id(dev)
        if folder_id is None:
            return []
        head = _libmtp.LIBMTP_Get_Files_And_Folders(dev, _DEFAULT_STORAGE_ID, c_uint32(folder_id))
        try:
            out: list[FileEntry] = []
            node_ptr = head
            while node_ptr:
                node = node_ptr.contents
                if node.filetype != _FILETYPE_FOLDER:
                    name = (node.filename or b"").decode(errors="replace")
                    out.append(FileEntry(path=f"documents/{name}", size=int(node.filesize)))
                node_ptr = node.next
            return out
        finally:
            if head:
                _libmtp.LIBMTP_destroy_file_t(head)


def _send_blocking(local_path: str, dest_name: str) -> str:
    if not os.path.isfile(local_path):
        raise MTPHelperError(f"local file does not exist: {local_path!r}")
    with _open_device() as (dev, _vid, _pid, _name):
        assert _libmtp is not None
        folder_id = _find_documents_folder_id(dev)
        if folder_id is None:
            raise MTPHelperError("device has no 'documents' folder")
        size = os.path.getsize(local_path)
        f = _LibMTPFile()
        f.item_id = 0
        f.parent_id = folder_id
        f.storage_id = _DEFAULT_STORAGE_ID
        f.filename = dest_name.encode()
        f.filesize = size
        f.modificationdate = 0
        f.filetype = _FILETYPE_UNKNOWN
        f.next = POINTER(_LibMTPFile)()
        ret = _libmtp.LIBMTP_Send_File_From_File(dev, local_path.encode(), byref(f), None, None)
        if ret != 0:
            _libmtp.LIBMTP_Clear_Errorstack(dev)
            raise MTPHelperError(f"LIBMTP_Send_File_From_File returned {ret}")
        if f.item_id == 0:
            raise MTPHelperError("send returned success but no item_id was assigned")
        return f"documents/{dest_name}"


def _remove_blocking(dest_name: str) -> None:
    with _open_device() as (dev, _vid, _pid, _name):
        assert _libmtp is not None
        folder_id = _find_documents_folder_id(dev)
        if folder_id is None:
            raise MTPHelperError("device has no 'documents' folder")
        head = _libmtp.LIBMTP_Get_Files_And_Folders(dev, _DEFAULT_STORAGE_ID, c_uint32(folder_id))
        target_id: int | None = None
        try:
            node_ptr = head
            while node_ptr:
                node = node_ptr.contents
                if node.filetype != _FILETYPE_FOLDER:
                    name = (node.filename or b"").decode(errors="replace")
                    if name == dest_name:
                        target_id = int(node.item_id)
                        break
                node_ptr = node.next
        finally:
            if head:
                _libmtp.LIBMTP_destroy_file_t(head)
        if target_id is None:
            raise MTPHelperError(f"documents/{dest_name} not found on device")
        ret = _libmtp.LIBMTP_Delete_Object(dev, c_uint32(target_id))
        if ret != 0:
            _libmtp.LIBMTP_Clear_Errorstack(dev)
            raise MTPHelperError(f"LIBMTP_Delete_Object returned {ret} for {dest_name!r}")


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
    with _open_device() as (dev, _vid, _pid, _name):
        assert _libmtp is not None
        thumb_folder_id = _find_thumbnails_folder_id(dev)
        if thumb_folder_id is None:
            raise MTPHelperError("device has no 'system/thumbnails' folder")
        partial_name = f"{dest_name}.tmp.partial"
        head = _libmtp.LIBMTP_Get_Files_And_Folders(
            dev, _DEFAULT_STORAGE_ID, c_uint32(thumb_folder_id)
        )
        try:
            node_ptr = head
            while node_ptr:
                node = node_ptr.contents
                if node.filetype != _FILETYPE_FOLDER:
                    name = (node.filename or b"").decode(errors="replace")
                    if name in (dest_name, partial_name):
                        ret = _libmtp.LIBMTP_Delete_Object(dev, c_uint32(int(node.item_id)))
                        if ret != 0:
                            _libmtp.LIBMTP_Clear_Errorstack(dev)
                node_ptr = node.next
        finally:
            if head:
                _libmtp.LIBMTP_destroy_file_t(head)
        size = os.path.getsize(local_path)
        f = _LibMTPFile()
        f.item_id = 0
        f.parent_id = thumb_folder_id
        f.storage_id = _DEFAULT_STORAGE_ID
        f.filename = dest_name.encode()
        f.filesize = size
        f.modificationdate = 0
        f.filetype = _FILETYPE_UNKNOWN
        f.next = POINTER(_LibMTPFile)()
        ret = _libmtp.LIBMTP_Send_File_From_File(dev, local_path.encode(), byref(f), None, None)
        if ret != 0:
            _libmtp.LIBMTP_Clear_Errorstack(dev)
            raise MTPHelperError(f"LIBMTP_Send_File_From_File returned {ret}")
        if f.item_id == 0:
            raise MTPHelperError("thumbnail send returned success but no item_id was assigned")
        return f"system/thumbnails/{dest_name}"


def _remove_thumbnail_blocking(dest_name: str, *, ignore_missing: bool) -> bool:
    """Delete a thumbnail and its ``.tmp.partial`` sibling, if either exists.

    Returns ``True`` if at least one object was deleted. With
    ``ignore_missing=False`` raises if neither file is found.
    """
    with _open_device() as (dev, _vid, _pid, _name):
        assert _libmtp is not None
        thumb_folder_id = _find_thumbnails_folder_id(dev)
        if thumb_folder_id is None:
            if ignore_missing:
                return False
            raise MTPHelperError("device has no 'system/thumbnails' folder")
        partial_name = f"{dest_name}.tmp.partial"
        head = _libmtp.LIBMTP_Get_Files_And_Folders(
            dev, _DEFAULT_STORAGE_ID, c_uint32(thumb_folder_id)
        )
        targets: list[tuple[int, str]] = []
        try:
            node_ptr = head
            while node_ptr:
                node = node_ptr.contents
                if node.filetype != _FILETYPE_FOLDER:
                    name = (node.filename or b"").decode(errors="replace")
                    if name in (dest_name, partial_name):
                        targets.append((int(node.item_id), name))
                node_ptr = node.next
        finally:
            if head:
                _libmtp.LIBMTP_destroy_file_t(head)
        if not targets:
            if ignore_missing:
                return False
            raise MTPHelperError(f"system/thumbnails/{dest_name} not found on device")
        deleted_any = False
        for iid, _name in targets:
            ret = _libmtp.LIBMTP_Delete_Object(dev, c_uint32(iid))
            if ret == 0:
                deleted_any = True
            else:
                _libmtp.LIBMTP_Clear_Errorstack(dev)
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
