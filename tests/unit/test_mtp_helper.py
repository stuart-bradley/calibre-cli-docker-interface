from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services import mtp_helper

# ---------------------------------------------------------------------------
# Async API — monkeypatches the per-verb blocking functions. The libmtp
# ctypes layer itself is not unit-tested; correctness there is verified
# on the device via the deploy + manual UI send.
# ---------------------------------------------------------------------------


# --- detect ----------------------------------------------------------------


async def test_detect_connected(monkeypatch):
    def fake_detect_blocking() -> mtp_helper.DetectResult:
        return mtp_helper.DetectResult(
            connected=True,
            device=mtp_helper.Device(name="Amazon Kindle", vid="1949", pid="9981"),
        )

    monkeypatch.setattr(mtp_helper, "_detect_blocking", fake_detect_blocking)

    result = await mtp_helper.detect()

    assert result.connected is True
    assert result.device == mtp_helper.Device(name="Amazon Kindle", vid="1949", pid="9981")


async def test_detect_disconnected(monkeypatch):
    def fake_detect_blocking() -> mtp_helper.DetectResult:
        return mtp_helper.DetectResult(connected=False, device=None)

    monkeypatch.setattr(mtp_helper, "_detect_blocking", fake_detect_blocking)

    result = await mtp_helper.detect()

    assert result.connected is False
    assert result.device is None


# --- list ------------------------------------------------------------------


async def test_list_files_ok(monkeypatch):
    def fake_list_blocking() -> list[mtp_helper.FileEntry]:
        return [
            mtp_helper.FileEntry(path="documents/A.epub", size=111),
            mtp_helper.FileEntry(path="documents/B.epub", size=222),
        ]

    monkeypatch.setattr(mtp_helper, "_list_blocking", fake_list_blocking)

    files = await mtp_helper.list_files()

    assert [f.path for f in files] == ["documents/A.epub", "documents/B.epub"]
    assert files[0].size == 111


async def test_list_files_propagates_error(monkeypatch):
    def fake_list_blocking() -> list[mtp_helper.FileEntry]:
        raise mtp_helper.MTPHelperError("no MTP devices found")

    monkeypatch.setattr(mtp_helper, "_list_blocking", fake_list_blocking)

    with pytest.raises(mtp_helper.MTPHelperError, match="no MTP devices found"):
        await mtp_helper.list_files()


# --- send ------------------------------------------------------------------


async def test_send_ok(monkeypatch):
    seen: list[tuple[str, str]] = []

    def fake_send_blocking(local: str, dest: str) -> str:
        seen.append((local, dest))
        return f"documents/{dest}"

    monkeypatch.setattr(mtp_helper, "_send_blocking", fake_send_blocking)

    dest = await mtp_helper.send(Path("/tmp/local.epub"), "X.epub")

    assert dest == "documents/X.epub"
    assert seen == [("/tmp/local.epub", "X.epub")]


async def test_send_propagates_error(monkeypatch):
    def fake_send_blocking(local: str, dest: str) -> str:
        raise mtp_helper.MTPHelperError("device has no 'documents' folder")

    monkeypatch.setattr(mtp_helper, "_send_blocking", fake_send_blocking)

    with pytest.raises(mtp_helper.MTPHelperError, match="documents"):
        await mtp_helper.send(Path("/tmp/local.epub"), "X.epub")


# --- remove ----------------------------------------------------------------


async def test_remove_ok(monkeypatch):
    seen: list[str] = []

    def fake_remove_blocking(dest: str) -> None:
        seen.append(dest)

    monkeypatch.setattr(mtp_helper, "_remove_blocking", fake_remove_blocking)

    await mtp_helper.remove("X.epub")

    assert seen == ["X.epub"]


async def test_remove_propagates_error(monkeypatch):
    def fake_remove_blocking(dest: str) -> None:
        raise mtp_helper.MTPHelperError("documents/X.epub not found on device")

    monkeypatch.setattr(mtp_helper, "_remove_blocking", fake_remove_blocking)

    with pytest.raises(mtp_helper.MTPHelperError, match="not found"):
        await mtp_helper.remove("X.epub")


# --- send_thumbnail / remove_thumbnail -----------------------------------------


async def test_send_thumbnail_ok(monkeypatch):
    seen: list[tuple[str, str]] = []

    def fake_blocking(local: str, dest: str) -> str:
        seen.append((local, dest))
        return f"system/thumbnails/{dest}"

    monkeypatch.setattr(mtp_helper, "_send_thumbnail_blocking", fake_blocking)

    dest = await mtp_helper.send_thumbnail(
        Path("/tmp/t.jpg"), "thumbnail_u_EBOK_portrait.jpg"
    )

    assert dest == "system/thumbnails/thumbnail_u_EBOK_portrait.jpg"
    assert seen == [("/tmp/t.jpg", "thumbnail_u_EBOK_portrait.jpg")]


async def test_send_thumbnail_propagates_error(monkeypatch):
    def fake_blocking(local: str, dest: str) -> str:
        raise mtp_helper.MTPHelperError("device has no 'system/thumbnails' folder")

    monkeypatch.setattr(mtp_helper, "_send_thumbnail_blocking", fake_blocking)

    with pytest.raises(mtp_helper.MTPHelperError, match="thumbnails"):
        await mtp_helper.send_thumbnail(Path("/tmp/t.jpg"), "thumbnail_x.jpg")


async def test_remove_thumbnail_default_ignores_missing(monkeypatch):
    seen: list[tuple[str, bool]] = []

    def fake_blocking(dest: str, *, ignore_missing: bool) -> bool:
        seen.append((dest, ignore_missing))
        return False

    monkeypatch.setattr(mtp_helper, "_remove_thumbnail_blocking", fake_blocking)

    deleted = await mtp_helper.remove_thumbnail("thumbnail_x.jpg")

    assert deleted is False
    assert seen == [("thumbnail_x.jpg", True)]


async def test_remove_thumbnail_can_raise_when_required(monkeypatch):
    def fake_blocking(dest: str, *, ignore_missing: bool) -> bool:
        raise mtp_helper.MTPHelperError("not found")

    monkeypatch.setattr(mtp_helper, "_remove_thumbnail_blocking", fake_blocking)

    with pytest.raises(mtp_helper.MTPHelperError, match="not found"):
        await mtp_helper.remove_thumbnail("thumbnail_x.jpg", ignore_missing=False)


# --- lock guard ------------------------------------------------------------


async def test_non_overlap_lock_serialises_calls(monkeypatch):
    """Two concurrent verbs must not run their blocking work concurrently —
    the libmtp session and the USB bus are single-tenant."""

    active = 0
    max_active = 0

    def fake_detect_blocking() -> mtp_helper.DetectResult:
        # Run inside ``asyncio.to_thread`` — this is a real worker thread.
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        # Hold the worker briefly so a second call (if not serialised) would
        # overlap.
        import time

        time.sleep(0.05)
        active -= 1
        return mtp_helper.DetectResult(connected=False, device=None)

    monkeypatch.setattr(mtp_helper, "_detect_blocking", fake_detect_blocking)

    await asyncio.gather(mtp_helper.detect(), mtp_helper.detect())

    assert max_active == 1


# ---------------------------------------------------------------------------
# Cold-start regression: the blocking verbs must not depend on a prior
# call having initialised the libmtp ctypes module global. The FastAPI
# worker hit this when a fresh process's first MTP operation was send()
# rather than detect(), and AssertionError fired before _open_device
# (which runs _ensure_init) was ever reached.
# ---------------------------------------------------------------------------


def test_send_blocking_does_not_assert_on_cold_libmtp(monkeypatch, tmp_path):
    """With ``_libmtp`` un-initialised, an early error path must raise
    MTPHelperError (the intended failure), not AssertionError (cold-start
    bug). Exercises the real ``_send_blocking`` via a missing-file path
    that returns before any libmtp call, so we don't need a fake device.
    """
    monkeypatch.setattr(mtp_helper, "_libmtp", None)
    missing = tmp_path / "definitely-not-here.epub"

    with pytest.raises(mtp_helper.MTPHelperError, match="local file does not exist"):
        mtp_helper._send_blocking(str(missing), "x.epub")


# ---------------------------------------------------------------------------
# Pure-Python helpers (no libmtp needed)
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
