"""In-memory MTP backend used by tests.

Implements the ``_Backend`` protocol from ``app.services.mtp_helper`` against
an explicit tree of folders + files. No USB, no libmtp, no kernel.

Tests build up a device with ``backend.add_device()`` then seed the tree via
``device.add_folder()`` / ``device.add_file()``. Operations the production
code runs (send / delete / list_folder) mutate the same tree, so assertions
inspect ``device.nodes`` afterwards.

Failure injection: set ``backend.fail_next_send`` or ``backend.fail_next_delete``
to a string before triggering the operation; the next call raises
``MTPHelperError`` with that message and the flag clears.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.services.mtp_helper import (
    _DEFAULT_STORAGE_ID,
    _DOCUMENTS_FOLDER_NAME,
    _MTP_PARENT_ROOT,
    _SYSTEM_FOLDER_NAME,
    _THUMBNAILS_FOLDER_NAME,
    MTPHelperError,
    _Entry,
    _RawDevice,
)


@dataclass
class FakeNode:
    item_id: int
    parent_id: int
    filename: str
    filesize: int
    is_folder: bool


class FakeDevice:
    def __init__(
        self,
        *,
        vid: int,
        pid: int,
        manufacturer: str,
        model: str,
        storage_id: int = _DEFAULT_STORAGE_ID,
    ) -> None:
        self.vid = vid
        self.pid = pid
        self.manufacturer = manufacturer
        self.model = model
        self.storage_id = storage_id
        self.nodes: dict[int, FakeNode] = {}
        # Item ids start above storage_id and the root sentinel so collisions
        # with libmtp constants are impossible.
        self._next_id = 0x100
        self.opened_count = 0
        self.closed_count = 0

    def add_folder(self, parent_id: int, name: str) -> int:
        return self._add(parent_id, name, 0, is_folder=True)

    def add_file(self, parent_id: int, name: str, *, size: int = 0) -> int:
        return self._add(parent_id, name, size, is_folder=False)

    def ensure_documents(self) -> int:
        """Create (or return) the ``documents/`` folder under root."""
        existing = self.find_by_name(_MTP_PARENT_ROOT, _DOCUMENTS_FOLDER_NAME)
        if existing is not None:
            return existing
        return self.add_folder(_MTP_PARENT_ROOT, _DOCUMENTS_FOLDER_NAME)

    def ensure_system_thumbnails(self) -> int:
        """Create (or return) the ``system/thumbnails/`` folder."""
        sys_id = self.find_by_name(_MTP_PARENT_ROOT, _SYSTEM_FOLDER_NAME)
        if sys_id is None:
            sys_id = self.add_folder(_MTP_PARENT_ROOT, _SYSTEM_FOLDER_NAME)
        thumb_id = self.find_by_name(sys_id, _THUMBNAILS_FOLDER_NAME)
        if thumb_id is None:
            thumb_id = self.add_folder(sys_id, _THUMBNAILS_FOLDER_NAME)
        return thumb_id

    def find_by_name(self, parent_id: int, name: str) -> int | None:
        for n in self.nodes.values():
            if n.parent_id == parent_id and n.filename == name:
                return n.item_id
        return None

    def list_children(self, parent_id: int) -> list[FakeNode]:
        return [n for n in self.nodes.values() if n.parent_id == parent_id]

    def filenames_in(self, parent_id: int) -> set[str]:
        return {n.filename for n in self.list_children(parent_id)}

    def _add(self, parent_id: int, name: str, size: int, *, is_folder: bool) -> int:
        item_id = self._next_id
        self._next_id += 1
        self.nodes[item_id] = FakeNode(
            item_id=item_id,
            parent_id=parent_id,
            filename=name,
            filesize=size,
            is_folder=is_folder,
        )
        return item_id


class FakeMtpBackend:
    def __init__(self) -> None:
        self.devices: list[FakeDevice] = []
        self.fail_next_send: str | None = None
        self.fail_next_delete: str | None = None
        self.fail_open: str | None = None
        self.sent_files: list[tuple[str, str]] = []
        self.deleted_ids: list[int] = []

    def add_device(
        self,
        *,
        vid: int = 0x1949,
        pid: int = 0x9981,
        manufacturer: str = "Amazon",
        model: str = "Kindle",
        storage_id: int = _DEFAULT_STORAGE_ID,
    ) -> FakeDevice:
        d = FakeDevice(
            vid=vid,
            pid=pid,
            manufacturer=manufacturer,
            model=model,
            storage_id=storage_id,
        )
        self.devices.append(d)
        return d

    # _Backend protocol --------------------------------------------------

    def detect(self) -> list[_RawDevice]:
        return [
            _RawDevice(vendor_id=d.vid, product_id=d.pid, handle=d)
            for d in self.devices
        ]

    def open(self, raw: _RawDevice) -> object:
        if self.fail_open is not None:
            raise MTPHelperError(self.fail_open)
        dev = raw.handle
        assert isinstance(dev, FakeDevice)
        dev.opened_count += 1
        return dev

    def close(self, dev: object) -> None:
        assert isinstance(dev, FakeDevice)
        dev.closed_count += 1

    def manufacturer(self, dev: object) -> str:
        assert isinstance(dev, FakeDevice)
        return dev.manufacturer

    def model(self, dev: object) -> str:
        assert isinstance(dev, FakeDevice)
        return dev.model

    def list_folder(
        self, dev: object, storage_id: int, parent_id: int
    ) -> list[_Entry]:
        assert isinstance(dev, FakeDevice)
        return [
            _Entry(
                item_id=n.item_id,
                parent_id=n.parent_id,
                filename=n.filename,
                filesize=n.filesize,
                is_folder=n.is_folder,
            )
            for n in dev.list_children(parent_id)
        ]

    def send_file(
        self,
        dev: object,
        local_path: str,
        storage_id: int,
        parent_id: int,
        dest_name: str,
    ) -> int:
        assert isinstance(dev, FakeDevice)
        if self.fail_next_send is not None:
            msg = self.fail_next_send
            self.fail_next_send = None
            raise MTPHelperError(msg)
        size = os.path.getsize(local_path)
        item_id = dev.add_file(parent_id, dest_name, size=size)
        self.sent_files.append((local_path, dest_name))
        return item_id

    def delete(self, dev: object, item_id: int) -> None:
        assert isinstance(dev, FakeDevice)
        if self.fail_next_delete is not None:
            msg = self.fail_next_delete
            self.fail_next_delete = None
            raise MTPHelperError(msg)
        if item_id not in dev.nodes:
            raise MTPHelperError(f"unknown item_id {item_id}")
        del dev.nodes[item_id]
        self.deleted_ids.append(item_id)
