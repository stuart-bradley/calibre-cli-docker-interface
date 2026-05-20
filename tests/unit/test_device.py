from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.services import device, mtp_helper
from app.state import DeviceState

# ---------------------------------------------------------------------------
# _detect_kindle_via_sysfs — pure sysfs USB presence + identity check
# ---------------------------------------------------------------------------


def _make_usb_entry(
    root: Path,
    name: str,
    vid: str,
    pid: str,
    *,
    manufacturer: str | None = None,
    product: str | None = None,
) -> None:
    """Write a fake sysfs USB-device directory like /sys/bus/usb/devices/1-3/."""
    d = root / name
    d.mkdir(parents=True)
    (d / "idVendor").write_text(vid + "\n")
    (d / "idProduct").write_text(pid + "\n")
    if manufacturer is not None:
        (d / "manufacturer").write_text(manufacturer + "\n")
    if product is not None:
        (d / "product").write_text(product + "\n")


def test_detect_returns_device_when_id_matches_with_strings(tmp_path, monkeypatch):
    _make_usb_entry(tmp_path, "1-3", "1949", "9981", manufacturer="Amazon", product="Amazon Kindle")
    _make_usb_entry(tmp_path, "1-4", "f400", "f400")  # decoy
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path)

    dev = device._detect_kindle_via_sysfs(["1949:9981"])
    assert dev == mtp_helper.Device(name="Amazon Amazon Kindle", vid="1949", pid="9981")


def test_detect_falls_back_to_vidpid_name_when_strings_missing(tmp_path, monkeypatch):
    _make_usb_entry(tmp_path, "1-3", "1949", "9981")
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path)

    dev = device._detect_kindle_via_sysfs(["1949:9981"])
    assert dev == mtp_helper.Device(name="1949:9981", vid="1949", pid="9981")


def test_detect_returns_none_when_id_missing(tmp_path, monkeypatch):
    _make_usb_entry(tmp_path, "1-4", "f400", "f400")
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path)

    assert device._detect_kindle_via_sysfs(["1949:9981"]) is None


def test_detect_empty_ids_returns_none(tmp_path, monkeypatch):
    """Without a VID:PID filter we cannot identify the Kindle. The empty case
    is reported as 'no device' rather than falling back to MTP probing — the
    operator must set CALIBRE_WEB_CLI_MTP_USB_IDS for detection to work."""
    _make_usb_entry(tmp_path, "1-3", "1949", "9981")
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path)

    assert device._detect_kindle_via_sysfs([]) is None


def test_detect_ignores_malformed_ids(tmp_path, monkeypatch):
    _make_usb_entry(tmp_path, "1-3", "1949", "9981")
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path)

    dev = device._detect_kindle_via_sysfs(["not-a-pair", "1949:9981"])
    assert dev is not None
    assert (dev.vid, dev.pid) == ("1949", "9981")


def test_detect_case_insensitive(tmp_path, monkeypatch):
    _make_usb_entry(tmp_path, "1-3", "1949", "9981")
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path)

    assert device._detect_kindle_via_sysfs(["1949:9981"]) is not None
    assert device._detect_kindle_via_sysfs(["1949:9981".upper()]) is not None


def test_detect_returns_none_when_sysfs_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path / "does-not-exist")

    assert device._detect_kindle_via_sysfs(["1949:9981"]) is None


def test_detect_picks_first_matching_when_multiple_present(tmp_path, monkeypatch):
    _make_usb_entry(tmp_path, "1-2", "f400", "f400")  # decoy
    _make_usb_entry(tmp_path, "1-3", "1949", "9981", manufacturer="Amazon", product="Kindle")
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path)

    dev = device._detect_kindle_via_sysfs(["1949:9981"])
    assert dev is not None
    assert (dev.vid, dev.pid) == ("1949", "9981")


# ---------------------------------------------------------------------------
# _poll_tick — the polling state machine
# ---------------------------------------------------------------------------


def _make_state(**kw) -> DeviceState:
    return DeviceState(**kw)


def _settings_with_ids(monkeypatch, tmp_path, *, ids: str = "1949:9981") -> Settings:
    monkeypatch.setenv("LIBRARY_PATH", str(tmp_path))
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "data"))
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", ids)
    return Settings()


def _patch_sysfs_returns(monkeypatch, dev: mtp_helper.Device | None) -> None:
    """Make _detect_kindle_via_sysfs return a fixed value, bypassing real sysfs."""
    monkeypatch.setattr(device, "_detect_kindle_via_sysfs", lambda ids: dev)


_FAKE_KINDLE = mtp_helper.Device(name="Amazon Amazon Kindle", vid="1949", pid="9981")


async def test_poll_clears_state_when_device_not_on_bus(monkeypatch, tmp_path):
    """When the Kindle drops off the USB bus, the poller must clear detect
    state and the file cache, and reset files_fetched so the next reconnect
    will re-list."""
    _patch_sysfs_returns(monkeypatch, None)
    list_calls: list[int] = []

    async def fake_list(**kw):
        list_calls.append(1)
        return []

    monkeypatch.setattr(mtp_helper, "list_files", fake_list)

    settings = _settings_with_ids(monkeypatch, tmp_path)
    state = _make_state(
        detect=mtp_helper.DetectResult(connected=True, device=_FAKE_KINDLE),
        on_device_filenames={"old-book.epub"},
        files_fetched=True,
    )

    await device._poll_tick(settings, state)

    assert list_calls == []  # MTP never opened
    assert state.detect is None
    assert state.on_device_filenames == set()
    assert state.files_fetched is False
    assert state.last_detect_error is None
    assert state.has_polled is True


async def test_poll_first_tick_with_device_present_lists_files_once(monkeypatch, tmp_path):
    """First poll tick after device appears on the bus: synthesize DetectResult
    from sysfs, then open MTP exactly once to populate the on-device cache."""
    _patch_sysfs_returns(monkeypatch, _FAKE_KINDLE)
    list_calls: list[int] = []

    async def fake_list(**kw):
        list_calls.append(1)
        return [
            mtp_helper.FileEntry(path="documents/foo.epub", size=10),
            mtp_helper.FileEntry(path="documents/bar.mobi", size=20),
        ]

    monkeypatch.setattr(mtp_helper, "list_files", fake_list)

    settings = _settings_with_ids(monkeypatch, tmp_path)
    state = _make_state()  # fresh: detect=None, files_fetched=False

    await device._poll_tick(settings, state)

    assert list_calls == [1]
    assert state.detect is not None
    assert state.detect.connected is True
    assert state.detect.device == _FAKE_KINDLE
    assert state.on_device_filenames == {"foo.epub", "bar.mobi"}
    assert state.files_fetched is True
    assert state.last_detect_error is None


async def test_poll_skips_mtp_in_steady_state(monkeypatch, tmp_path):
    """Regression for the Kindle-disconnect bug: when the device is on the bus
    AND files_fetched is True, the poller must NOT re-open MTP. Each MTP open
    causes the jailbroken Kindle firmware to drop the device."""
    _patch_sysfs_returns(monkeypatch, _FAKE_KINDLE)
    list_calls: list[int] = []

    async def fake_list(**kw):
        list_calls.append(1)
        return []

    monkeypatch.setattr(mtp_helper, "list_files", fake_list)

    settings = _settings_with_ids(monkeypatch, tmp_path)
    state = _make_state(
        detect=mtp_helper.DetectResult(connected=True, device=_FAKE_KINDLE),
        on_device_filenames={"cached.epub"},
        files_fetched=True,
    )

    await device._poll_tick(settings, state)

    assert list_calls == []
    # state.detect is refreshed from sysfs each tick, but the cache is untouched.
    assert state.detect is not None and state.detect.connected is True
    assert state.on_device_filenames == {"cached.epub"}
    assert state.files_fetched is True


async def test_poll_list_files_error_sets_files_fetched_to_avoid_retry_loop(monkeypatch, tmp_path):
    """If list_files fails, files_fetched must still be True so the next tick
    doesn't retry — each retry would re-trigger the firmware disconnect. The
    user can replug to retry."""
    _patch_sysfs_returns(monkeypatch, _FAKE_KINDLE)
    list_calls: list[int] = []

    async def fake_list(**kw):
        list_calls.append(1)
        raise mtp_helper.MTPHelperError("libmtp boom")

    monkeypatch.setattr(mtp_helper, "list_files", fake_list)

    settings = _settings_with_ids(monkeypatch, tmp_path)
    state = _make_state()

    await device._poll_tick(settings, state)

    assert list_calls == [1]
    assert state.detect is not None and state.detect.connected is True
    assert state.on_device_filenames == set()
    assert state.files_fetched is True
    assert state.last_detect_error == "libmtp boom"

    # Second tick must NOT retry list_files.
    await device._poll_tick(settings, state)
    assert list_calls == [1]


async def test_poll_relists_after_device_leaves_and_returns(monkeypatch, tmp_path):
    """Full session cycle: connect → cache populated → unplug → replug → cache
    re-populated."""

    # Tick 1: device on bus, fresh state, list_files runs.
    _patch_sysfs_returns(monkeypatch, _FAKE_KINDLE)
    list_calls: list[int] = []

    async def fake_list(**kw):
        list_calls.append(1)
        return [mtp_helper.FileEntry(path=f"documents/book{len(list_calls)}.epub", size=1)]

    monkeypatch.setattr(mtp_helper, "list_files", fake_list)

    settings = _settings_with_ids(monkeypatch, tmp_path)
    state = _make_state()

    await device._poll_tick(settings, state)
    assert state.files_fetched is True
    assert state.on_device_filenames == {"book1.epub"}

    # Tick 2: still on bus — no MTP.
    await device._poll_tick(settings, state)
    assert list_calls == [1]

    # Tick 3: device leaves bus — state cleared.
    _patch_sysfs_returns(monkeypatch, None)
    await device._poll_tick(settings, state)
    assert state.detect is None
    assert state.files_fetched is False
    assert state.on_device_filenames == set()

    # Tick 4: device returns — list_files runs again with fresh listing.
    _patch_sysfs_returns(monkeypatch, _FAKE_KINDLE)
    await device._poll_tick(settings, state)
    assert list_calls == [1, 1]
    assert state.on_device_filenames == {"book2.epub"}


async def test_poll_with_empty_ids_treats_device_as_absent(monkeypatch, tmp_path):
    """Empty CALIBRE_WEB_CLI_MTP_USB_IDS: detection always returns None, no MTP
    calls. Real sysfs would still see the device but we can't identify it."""
    # Don't patch _detect_kindle_via_sysfs — let it run with the real impl.
    # Use an empty sysfs dir so even if it tries to scan it gets nothing.
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path)
    list_calls: list[int] = []

    async def fake_list(**kw):
        list_calls.append(1)
        return []

    monkeypatch.setattr(mtp_helper, "list_files", fake_list)

    settings = _settings_with_ids(monkeypatch, tmp_path, ids="")
    assert settings.mtp_usb_ids == []
    state = _make_state()

    await device._poll_tick(settings, state)

    assert list_calls == []
    assert state.detect is None
    assert state.files_fetched is False
    assert state.has_polled is True


async def test_poll_unexpected_exception_does_not_kill_loop(monkeypatch, tmp_path):
    """An unexpected exception from list_files (something other than
    MTPHelperError) must be caught so poll_device_loop keeps ticking."""
    _patch_sysfs_returns(monkeypatch, _FAKE_KINDLE)

    async def fake_list(**kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(mtp_helper, "list_files", fake_list)

    settings = _settings_with_ids(monkeypatch, tmp_path)
    state = _make_state()

    # Must not raise.
    await device._poll_tick(settings, state)

    assert state.detect is None
    assert state.on_device_filenames == set()
    assert state.files_fetched is False
    assert state.last_detect_error is not None
    assert "RuntimeError" in state.last_detect_error
    assert state.has_polled is True


# ---------------------------------------------------------------------------
# books_on_device — pure in-memory filename matching
# ---------------------------------------------------------------------------


class _FakeBook:
    def __init__(self, book_id: int, format_filenames: dict[str, str]):
        self.id = book_id
        self.format_filenames = format_filenames


def test_books_on_device_returns_empty_when_cache_empty():
    state = _make_state()
    books = [_FakeBook(1, {"EPUB": "foo.epub"})]
    assert device.books_on_device(state, books) == set()


def test_books_on_device_matches_any_format():
    state = _make_state(on_device_filenames={"foo.epub", "baz.mobi"})
    books = [
        _FakeBook(1, {"EPUB": "foo.epub"}),
        _FakeBook(2, {"AZW3": "bar.azw3"}),
        _FakeBook(3, {"MOBI": "baz.mobi", "EPUB": "baz.epub"}),
    ]
    assert device.books_on_device(state, books) == {1, 3}
