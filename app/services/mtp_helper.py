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


class _LibMTPFolder(Structure):
    pass


_LibMTPFolder._fields_ = [
    ("folder_id", c_uint32),
    ("parent_id", c_uint32),
    ("storage_id", c_uint32),
    ("name", c_char_p),
    ("sibling", POINTER(_LibMTPFolder)),
    ("child", POINTER(_LibMTPFolder)),
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

        libmtp.LIBMTP_Open_Raw_Device.argtypes = [POINTER(_LibMTPRawDevice)]
        libmtp.LIBMTP_Open_Raw_Device.restype = c_void_p

        libmtp.LIBMTP_Release_Device.argtypes = [c_void_p]
        libmtp.LIBMTP_Release_Device.restype = None

        libmtp.LIBMTP_Get_Manufacturername.argtypes = [c_void_p]
        libmtp.LIBMTP_Get_Manufacturername.restype = c_char_p

        libmtp.LIBMTP_Get_Modelname.argtypes = [c_void_p]
        libmtp.LIBMTP_Get_Modelname.restype = c_char_p

        libmtp.LIBMTP_Get_Folder_List.argtypes = [c_void_p]
        libmtp.LIBMTP_Get_Folder_List.restype = POINTER(_LibMTPFolder)

        libmtp.LIBMTP_destroy_folder_t.argtypes = [POINTER(_LibMTPFolder)]
        libmtp.LIBMTP_destroy_folder_t.restype = None

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

    Open semantics: ``LIBMTP_Open_Raw_Device`` (the cached open) is used
    because ``LIBMTP_Get_Folder_List`` returns an empty list against the
    uncached handle on this firmware. The cache is what backs the folder
    enumeration; without it folder lookup silently returns no results
    (verified on-device: cached → 7 top-level folders, uncached → 0).
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
        dev = _libmtp.LIBMTP_Open_Raw_Device(byref(raw_ptr[idx]))
        if not dev:
            raise MTPHelperError(f"LIBMTP_Open_Raw_Device returned NULL for {vid}:{pid}")
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


def _find_documents_folder_id(dev: c_void_p) -> int | None:
    """Walk the top-level folders and return the id of ``documents``.

    The id varies between devices (and after a factory reset), so we resolve
    it on every call rather than caching.
    """
    assert _libmtp is not None
    folder_ptr = _libmtp.LIBMTP_Get_Folder_List(dev)
    if not folder_ptr:
        return None
    try:
        node_ptr = folder_ptr
        while node_ptr:
            node = node_ptr.contents
            name = (node.name or b"").decode(errors="replace")
            if name.lower() == _DOCUMENTS_FOLDER_NAME:
                return int(node.folder_id)
            node_ptr = node.sibling
        return None
    finally:
        _libmtp.LIBMTP_destroy_folder_t(folder_ptr)


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
    assert _libmtp is not None
    with _open_device() as (dev, _vid, _pid, _name):
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
    assert _libmtp is not None
    if not os.path.isfile(local_path):
        raise MTPHelperError(f"local file does not exist: {local_path!r}")
    with _open_device() as (dev, _vid, _pid, _name):
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
    assert _libmtp is not None
    with _open_device() as (dev, _vid, _pid, _name):
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
