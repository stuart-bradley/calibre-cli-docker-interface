from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services import mtp_helper
from tests.fakes.mtp import FakeMtpBackend

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(monkeypatch):
    """A FakeMtpBackend installed into mtp_helper for the duration of the test.
    No device is added by default; tests opt in via ``backend.add_device(...)``
    so the empty-bus / no-match paths are also reachable."""
    fake = FakeMtpBackend()
    monkeypatch.setattr(mtp_helper, "_backend", fake)
    return fake


@pytest.fixture
def local_book(tmp_path: Path) -> Path:
    """A tiny on-disk file standing in for a book or thumbnail. ``send_file``
    on both the real and fake backends reads its size via ``os.path.getsize``,
    so the file has to actually exist on the host."""
    p = tmp_path / "book.azw3"
    p.write_bytes(b"AZW3-payload")
    return p


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------


async def test_detect_returns_friendly_name_from_backend(backend, monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    backend.add_device(manufacturer="Amazon", model="Kindle")

    result = await mtp_helper.detect()

    assert result.connected is True
    assert result.device is not None
    assert result.device.name == "Amazon Kindle"
    assert (result.device.vid, result.device.pid) == ("1949", "9981")


async def test_detect_returns_disconnected_when_no_devices(backend, monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")

    result = await mtp_helper.detect()

    assert result.connected is False
    assert result.device is None


async def test_detect_filters_by_usb_id(backend, monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "1949:9981")
    backend.add_device(vid=0x0BDA, pid=0x9210, manufacturer="Realtek", model="Decoy")
    backend.add_device(vid=0x1949, pid=0x9981, manufacturer="Amazon", model="Kindle")

    result = await mtp_helper.detect()

    assert result.connected is True
    assert result.device is not None
    assert result.device.vid == "1949"


async def test_detect_returns_disconnected_when_no_filter_match(backend, monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "1949:9981")
    backend.add_device(vid=0x0BDA, pid=0x9210)

    result = await mtp_helper.detect()

    assert result.connected is False


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


async def test_list_files_returns_documents_contents(backend, monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    dev = backend.add_device()
    docs = dev.ensure_documents()
    dev.add_file(docs, "alpha.azw3", size=111)
    dev.add_file(docs, "beta.mobi", size=222)

    files = await mtp_helper.list_files()

    paths = sorted(f.path for f in files)
    assert paths == ["documents/alpha.azw3", "documents/beta.mobi"]
    by_path = {f.path: f.size for f in files}
    assert by_path["documents/alpha.azw3"] == 111
    assert by_path["documents/beta.mobi"] == 222


async def test_list_files_excludes_folders(backend, monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    dev = backend.add_device()
    docs = dev.ensure_documents()
    dev.add_file(docs, "real.azw3", size=10)
    dev.add_folder(docs, "subdir")

    files = await mtp_helper.list_files()

    assert [f.path for f in files] == ["documents/real.azw3"]


async def test_list_files_returns_empty_when_no_documents_folder(backend, monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    backend.add_device()  # device present but root is empty

    files = await mtp_helper.list_files()

    assert files == []


async def test_list_files_raises_when_no_device(backend, monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")

    with pytest.raises(mtp_helper.MTPHelperError, match="no MTP devices found"):
        await mtp_helper.list_files()


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


async def test_send_writes_into_documents_folder(backend, monkeypatch, local_book):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    dev = backend.add_device()
    docs = dev.ensure_documents()

    dest = await mtp_helper.send(local_book, "alpha.azw3")

    assert dest == "documents/alpha.azw3"
    assert dev.find_by_name(docs, "alpha.azw3") is not None
    item_id = dev.find_by_name(docs, "alpha.azw3")
    assert dev.nodes[item_id].filesize == len(b"AZW3-payload")
    assert backend.sent_files == [(str(local_book), "alpha.azw3")]


async def test_send_raises_when_no_documents_folder(backend, monkeypatch, local_book):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    backend.add_device()

    with pytest.raises(mtp_helper.MTPHelperError, match="documents"):
        await mtp_helper.send(local_book, "alpha.azw3")


async def test_send_raises_when_local_file_missing(backend, monkeypatch, tmp_path):
    """Missing-file check happens before any device access, so this works
    even with no device on the bus — the cold-start regression test."""
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    missing = tmp_path / "definitely-not-here.azw3"

    with pytest.raises(mtp_helper.MTPHelperError, match="local file does not exist"):
        await mtp_helper.send(missing, "alpha.azw3")


async def test_send_propagates_backend_failure(backend, monkeypatch, local_book):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    dev = backend.add_device()
    dev.ensure_documents()
    backend.fail_next_send = "device storage full"

    with pytest.raises(mtp_helper.MTPHelperError, match="device storage full"):
        await mtp_helper.send(local_book, "alpha.azw3")


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


async def test_remove_deletes_by_name(backend, monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    dev = backend.add_device()
    docs = dev.ensure_documents()
    iid = dev.add_file(docs, "bar.mobi", size=42)

    await mtp_helper.remove("bar.mobi")

    assert iid not in dev.nodes
    assert dev.find_by_name(docs, "bar.mobi") is None


async def test_remove_raises_when_missing(backend, monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    dev = backend.add_device()
    dev.ensure_documents()

    with pytest.raises(mtp_helper.MTPHelperError, match="not found"):
        await mtp_helper.remove("bar.mobi")


async def test_remove_raises_when_no_documents_folder(backend, monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    backend.add_device()

    with pytest.raises(mtp_helper.MTPHelperError, match="documents"):
        await mtp_helper.remove("bar.mobi")


# ---------------------------------------------------------------------------
# send_thumbnail
# ---------------------------------------------------------------------------


async def test_send_thumbnail_writes_into_system_thumbnails(
    backend, monkeypatch, local_book
):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    dev = backend.add_device()
    thumbs = dev.ensure_system_thumbnails()

    dest = await mtp_helper.send_thumbnail(local_book, "thumbnail_X_EBOK_portrait.jpg")

    assert dest == "system/thumbnails/thumbnail_X_EBOK_portrait.jpg"
    assert dev.find_by_name(thumbs, "thumbnail_X_EBOK_portrait.jpg") is not None


async def test_send_thumbnail_cleans_up_partial_sentinel(
    backend, monkeypatch, local_book
):
    """The firmware leaves a 0-byte ``.tmp.partial`` when its own cover
    extractor fails. Until it's removed, the device ignores subsequent
    thumbnail uploads for that UUID. Our send path must wipe it first."""
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    dev = backend.add_device()
    thumbs = dev.ensure_system_thumbnails()
    sentinel_id = dev.add_file(
        thumbs, "thumbnail_X_EBOK_portrait.jpg.tmp.partial", size=0
    )

    await mtp_helper.send_thumbnail(local_book, "thumbnail_X_EBOK_portrait.jpg")

    assert sentinel_id not in dev.nodes
    assert dev.find_by_name(thumbs, "thumbnail_X_EBOK_portrait.jpg.tmp.partial") is None
    assert dev.find_by_name(thumbs, "thumbnail_X_EBOK_portrait.jpg") is not None


async def test_send_thumbnail_overwrites_existing(backend, monkeypatch, local_book):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    dev = backend.add_device()
    thumbs = dev.ensure_system_thumbnails()
    old_id = dev.add_file(thumbs, "thumbnail_X_EBOK_portrait.jpg", size=999)

    await mtp_helper.send_thumbnail(local_book, "thumbnail_X_EBOK_portrait.jpg")

    assert old_id not in dev.nodes
    new_id = dev.find_by_name(thumbs, "thumbnail_X_EBOK_portrait.jpg")
    assert new_id is not None
    assert dev.nodes[new_id].filesize == len(b"AZW3-payload")


async def test_send_thumbnail_raises_when_thumbnails_folder_missing(
    backend, monkeypatch, local_book
):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    backend.add_device()

    with pytest.raises(mtp_helper.MTPHelperError, match="thumbnails"):
        await mtp_helper.send_thumbnail(local_book, "thumbnail_X.jpg")


# ---------------------------------------------------------------------------
# remove_thumbnail
# ---------------------------------------------------------------------------


async def test_remove_thumbnail_removes_canonical_and_partial(backend, monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    dev = backend.add_device()
    thumbs = dev.ensure_system_thumbnails()
    canonical_id = dev.add_file(thumbs, "thumbnail_X_EBOK_portrait.jpg", size=1)
    partial_id = dev.add_file(
        thumbs, "thumbnail_X_EBOK_portrait.jpg.tmp.partial", size=0
    )

    deleted = await mtp_helper.remove_thumbnail("thumbnail_X_EBOK_portrait.jpg")

    assert deleted is True
    assert canonical_id not in dev.nodes
    assert partial_id not in dev.nodes


async def test_remove_thumbnail_returns_false_when_missing_with_ignore_missing(
    backend, monkeypatch
):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    dev = backend.add_device()
    dev.ensure_system_thumbnails()

    deleted = await mtp_helper.remove_thumbnail("thumbnail_X_EBOK_portrait.jpg")

    assert deleted is False


async def test_remove_thumbnail_raises_when_required(backend, monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    dev = backend.add_device()
    dev.ensure_system_thumbnails()

    with pytest.raises(mtp_helper.MTPHelperError, match="not found"):
        await mtp_helper.remove_thumbnail(
            "thumbnail_X_EBOK_portrait.jpg", ignore_missing=False
        )


async def test_remove_thumbnail_returns_false_when_folder_missing(backend, monkeypatch):
    """Folder absent + ignore_missing=True is a no-op, not an error."""
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    backend.add_device()  # no system/thumbnails created

    deleted = await mtp_helper.remove_thumbnail("thumbnail_X.jpg")

    assert deleted is False


# ---------------------------------------------------------------------------
# Lock + session lifecycle
# ---------------------------------------------------------------------------


async def test_lock_serialises_concurrent_operations(backend, monkeypatch):
    """Two concurrent verbs must not run their blocking work concurrently —
    the libmtp session and the USB bus are single-tenant."""
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    backend.add_device()

    active = 0
    max_active = 0
    original_list_folder = backend.list_folder

    def hooked_list_folder(dev, storage_id, parent_id):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        import time

        time.sleep(0.02)
        active -= 1
        return original_list_folder(dev, storage_id, parent_id)

    monkeypatch.setattr(backend, "list_folder", hooked_list_folder)

    await asyncio.gather(mtp_helper.list_files(), mtp_helper.list_files())

    assert max_active == 1


async def test_open_close_pair_per_operation(backend, monkeypatch, local_book):
    """Each blocking verb opens, runs, releases the device. No leaks
    between verbs."""
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")
    dev = backend.add_device()
    dev.ensure_documents()

    await mtp_helper.list_files()
    await mtp_helper.send(local_book, "alpha.azw3")
    await mtp_helper.remove("alpha.azw3")

    assert dev.opened_count == dev.closed_count == 3


# ---------------------------------------------------------------------------
# Pure-Python helpers (no backend needed)
# ---------------------------------------------------------------------------


def test_parse_usb_id_filter_empty(monkeypatch):
    monkeypatch.delenv("CALIBRE_WEB_CLI_MTP_USB_IDS", raising=False)
    assert mtp_helper._parse_usb_id_filter() == set()


def test_parse_usb_id_filter_single(monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "1949:9981")
    assert mtp_helper._parse_usb_id_filter() == {("1949", "9981")}


def test_parse_usb_id_filter_multi_pads_short_ids(monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "0BDA:9210, 949:81 ")
    assert mtp_helper._parse_usb_id_filter() == {
        ("0bda", "9210"),
        ("0949", "0081"),
    }


def test_parse_usb_id_filter_skips_malformed(monkeypatch):
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "noidhere, 1949:9981, alsobad")
    assert mtp_helper._parse_usb_id_filter() == {("1949", "9981")}
