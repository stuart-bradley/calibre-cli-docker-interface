from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.services import device, mtp_helper
from app.state import DeviceState

# ---------------------------------------------------------------------------
# _kindle_on_bus — sysfs USB presence check
# ---------------------------------------------------------------------------


def _make_usb_entry(root: Path, name: str, vid: str, pid: str) -> None:
    """Write a fake sysfs USB-device directory like /sys/bus/usb/devices/1-3/."""
    d = root / name
    d.mkdir(parents=True)
    (d / "idVendor").write_text(vid + "\n")
    (d / "idProduct").write_text(pid + "\n")


def test_kindle_on_bus_returns_true_when_id_matches(tmp_path, monkeypatch):
    _make_usb_entry(tmp_path, "1-3", "1949", "9981")
    _make_usb_entry(tmp_path, "1-4", "f400", "f400")  # decoy
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path)

    assert device._kindle_on_bus(["1949:9981"]) is True


def test_kindle_on_bus_returns_false_when_id_missing(tmp_path, monkeypatch):
    _make_usb_entry(tmp_path, "1-4", "f400", "f400")
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path)

    assert device._kindle_on_bus(["1949:9981"]) is False


def test_kindle_on_bus_empty_ids_falls_back_to_true(tmp_path, monkeypatch):
    """No filter configured — caller falls back to the previous always-poll
    behaviour. The MTP layer does the final filtering."""
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path)

    assert device._kindle_on_bus([]) is True


def test_kindle_on_bus_ignores_malformed_ids(tmp_path, monkeypatch):
    _make_usb_entry(tmp_path, "1-3", "1949", "9981")
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path)

    # malformed entries are silently dropped; the well-formed one still matches
    assert device._kindle_on_bus(["not-a-pair", "1949:9981"]) is True


def test_kindle_on_bus_case_insensitive(tmp_path, monkeypatch):
    _make_usb_entry(tmp_path, "1-3", "1949", "9981")
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path)

    assert device._kindle_on_bus(["1949:9981"]) is True
    assert device._kindle_on_bus(["1949:9981".upper()]) is True


def test_kindle_on_bus_sysfs_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(device, "_SYSFS_USB_DEVICES", tmp_path / "does-not-exist")

    assert device._kindle_on_bus(["1949:9981"]) is False


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


async def test_poll_clears_state_when_device_not_on_bus(monkeypatch, tmp_path):
    """When the Kindle drops off the USB bus, the poller must clear detect
    state without ever calling mtp_helper.detect()."""
    monkeypatch.setattr(device, "_kindle_on_bus", lambda ids: False)
    detect_calls: list[int] = []

    async def fake_detect(**kw):
        detect_calls.append(1)
        return mtp_helper.DetectResult(connected=False, device=None)

    monkeypatch.setattr(mtp_helper, "detect", fake_detect)

    settings = _settings_with_ids(monkeypatch, tmp_path)
    state = _make_state(
        detect=mtp_helper.DetectResult(
            connected=True, device=mtp_helper.Device(name="Kindle", vid="1949", pid="9981")
        ),
        on_device_filenames={"old-book.epub"},
    )

    await device._poll_tick(settings, state)

    assert detect_calls == []  # MTP never opened
    assert state.detect is None
    assert state.on_device_filenames == set()
    assert state.last_detect_error is None
    assert state.has_polled is True


async def test_poll_opens_mtp_on_transition_to_connected(monkeypatch, tmp_path):
    """On the disconnect→connect transition the poller does open MTP — once —
    to confirm capability and populate the filename cache."""
    monkeypatch.setattr(device, "_kindle_on_bus", lambda ids: True)
    detect_calls: list[int] = []
    list_calls: list[int] = []

    async def fake_detect(**kw):
        detect_calls.append(1)
        return mtp_helper.DetectResult(
            connected=True, device=mtp_helper.Device(name="Kindle", vid="1949", pid="9981")
        )

    async def fake_list(**kw):
        list_calls.append(1)
        return [mtp_helper.FileEntry(path="documents/foo.epub", size=10)]

    monkeypatch.setattr(mtp_helper, "detect", fake_detect)
    monkeypatch.setattr(mtp_helper, "list_files", fake_list)

    settings = _settings_with_ids(monkeypatch, tmp_path)
    state = _make_state()  # detect=None — fresh

    await device._poll_tick(settings, state)

    assert detect_calls == [1]
    assert list_calls == [1]
    assert state.detect is not None and state.detect.connected is True
    assert state.on_device_filenames == {"foo.epub"}
    assert state.last_detect_error is None


async def test_poll_skips_mtp_in_steady_state(monkeypatch, tmp_path):
    """Regression for the Kindle-disconnect bug: when the device is already
    known-connected AND still on the bus, the poller must NOT re-open MTP.
    Each open/close caused the Kindle MTP firmware to drop the device back
    to reading mode."""
    monkeypatch.setattr(device, "_kindle_on_bus", lambda ids: True)
    detect_calls: list[int] = []
    list_calls: list[int] = []

    async def fake_detect(**kw):
        detect_calls.append(1)
        return mtp_helper.DetectResult(connected=False, device=None)

    async def fake_list(**kw):
        list_calls.append(1)
        return []

    monkeypatch.setattr(mtp_helper, "detect", fake_detect)
    monkeypatch.setattr(mtp_helper, "list_files", fake_list)

    settings = _settings_with_ids(monkeypatch, tmp_path)
    state = _make_state(
        detect=mtp_helper.DetectResult(
            connected=True, device=mtp_helper.Device(name="Kindle", vid="1949", pid="9981")
        ),
        on_device_filenames={"cached.epub"},
    )

    await device._poll_tick(settings, state)

    assert detect_calls == []
    assert list_calls == []
    # State must remain untouched.
    assert state.detect is not None and state.detect.connected is True
    assert state.on_device_filenames == {"cached.epub"}


async def test_poll_retries_mtp_after_previous_error(monkeypatch, tmp_path):
    """If a prior tick errored (state.detect is None even though device is on
    the bus), the poller should retry the MTP open on the next tick."""
    monkeypatch.setattr(device, "_kindle_on_bus", lambda ids: True)
    detect_calls: list[int] = []

    async def fake_detect(**kw):
        detect_calls.append(1)
        return mtp_helper.DetectResult(
            connected=True, device=mtp_helper.Device(name="K", vid="1949", pid="9981")
        )

    async def fake_list(**kw):
        return []

    monkeypatch.setattr(mtp_helper, "detect", fake_detect)
    monkeypatch.setattr(mtp_helper, "list_files", fake_list)

    settings = _settings_with_ids(monkeypatch, tmp_path)
    state = _make_state(detect=None, last_detect_error="prior boom")

    await device._poll_tick(settings, state)

    assert detect_calls == [1]
    assert state.detect is not None and state.detect.connected is True
    assert state.last_detect_error is None


async def test_poll_records_mtp_error_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(device, "_kindle_on_bus", lambda ids: True)

    async def fake_detect(**kw):
        raise mtp_helper.MTPHelperError("libmtp not loadable")

    monkeypatch.setattr(mtp_helper, "detect", fake_detect)

    settings = _settings_with_ids(monkeypatch, tmp_path)
    state = _make_state()

    await device._poll_tick(settings, state)

    assert state.detect is None
    assert state.on_device_filenames == set()
    assert state.last_detect_error == "libmtp not loadable"
    assert state.has_polled is True
