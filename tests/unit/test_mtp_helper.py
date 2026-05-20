from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from app.services import mtp_helper


@pytest.fixture
def fake_helper(monkeypatch):
    """Stub asyncio.create_subprocess_exec with a queued list of responses."""

    queue: list[tuple[int, str, str]] = []   # (returncode, stdout, stderr)
    call_log: list[list[str]] = []

    class _FakeProc:
        def __init__(self, returncode: int, stdout: str, stderr: str):
            self.returncode = returncode
            self._stdout = stdout.encode()
            self._stderr = stderr.encode()

        async def communicate(self) -> tuple[bytes, bytes]:
            return self._stdout, self._stderr

    async def fake_exec(*cmd, stdout=None, stderr=None):
        call_log.append(list(cmd))
        if not queue:
            return _FakeProc(0, "{}", "")
        rc, out, err = queue.pop(0)
        return _FakeProc(rc, out, err)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return queue, call_log


def _enqueue(queue, payload, *, returncode: int = 0, stderr: str = "") -> None:
    queue.append((returncode, json.dumps(payload), stderr))


# --- detect -------------------------------------------------------------------


async def test_detect_connected(fake_helper):
    queue, log = fake_helper
    _enqueue(queue, {
        "connected": True,
        "device": {"name": "Kindle PW3", "vid": "1949", "pid": "9981"},
    })

    result = await mtp_helper.detect()

    assert result.connected is True
    assert result.device == mtp_helper.Device(name="Kindle PW3", vid="1949", pid="9981")
    assert log[0][:3] == ["calibre-debug", "-e", str(mtp_helper._DEFAULT_HELPER_PATH)]
    assert log[0][3] == "detect"


async def test_detect_disconnected(fake_helper):
    queue, _log = fake_helper
    _enqueue(queue, {"connected": False, "device": None})

    result = await mtp_helper.detect()

    assert result.connected is False
    assert result.device is None


# --- list ---------------------------------------------------------------------


async def test_list_files_ok(fake_helper):
    queue, _log = fake_helper
    _enqueue(queue, {"ok": True, "files": [
        {"path": "documents/A.epub", "size": 111},
        {"path": "documents/B.epub", "size": 222},
    ]})

    files = await mtp_helper.list_files()

    assert [f.path for f in files] == ["documents/A.epub", "documents/B.epub"]
    assert files[0].size == 111


async def test_list_files_error(fake_helper):
    queue, _log = fake_helper
    _enqueue(queue, {"ok": False, "error": "no device"})

    with pytest.raises(mtp_helper.MTPHelperError, match="no device"):
        await mtp_helper.list_files()


# --- send ---------------------------------------------------------------------


async def test_send_ok(fake_helper):
    queue, log = fake_helper
    _enqueue(queue, {"ok": True, "dest": "documents/X.epub"})

    dest = await mtp_helper.send(Path("/tmp/local.epub"), "X.epub")

    assert dest == "documents/X.epub"
    assert log[0][-3:] == ["send", "/tmp/local.epub", "X.epub"]


async def test_send_error(fake_helper):
    queue, _log = fake_helper
    _enqueue(queue, {"ok": False, "error": "device full"})

    with pytest.raises(mtp_helper.MTPHelperError, match="device full"):
        await mtp_helper.send(Path("/tmp/local.epub"), "X.epub")


# --- remove -------------------------------------------------------------------


async def test_remove_ok(fake_helper):
    queue, log = fake_helper
    _enqueue(queue, {"ok": True})

    await mtp_helper.remove("X.epub")

    assert log[0][-2:] == ["remove", "X.epub"]


async def test_remove_error(fake_helper):
    queue, _log = fake_helper
    _enqueue(queue, {"ok": False, "error": "not found"})

    with pytest.raises(mtp_helper.MTPHelperError, match="not found"):
        await mtp_helper.remove("X.epub")


# --- failure modes ------------------------------------------------------------


async def test_catastrophic_failure_raises(fake_helper):
    queue, _log = fake_helper
    queue.append((2, "", "ImportError: libmtp not loadable"))

    with pytest.raises(mtp_helper.MTPHelperError, match="exit 2"):
        await mtp_helper.detect()


async def test_non_json_output_raises(fake_helper):
    queue, _log = fake_helper
    queue.append((0, "not valid json", ""))

    with pytest.raises(mtp_helper.MTPHelperError, match="non-JSON"):
        await mtp_helper.detect()


# --- lock guard ---------------------------------------------------------------


# --- _build_driver (NAS bug fix: GUI-side attributes) -----------------------


def _install_fake_calibre(monkeypatch):
    """Inject minimal fake calibre.* modules into sys.modules so _build_driver
    can import them. Returns (MTP_DEVICE_class, instance_capture)."""
    instances: list[object] = []

    class _FakeMTP:
        def __init__(self, parent):
            self.parent = parent
            self.startup_called = False
            instances.append(self)

        def startup(self):
            self.startup_called = True

    class _FakeJSONConfig:
        def __init__(self, name):
            self.name = name

    driver_mod = types.ModuleType("calibre.devices.mtp.driver")
    driver_mod.MTP_DEVICE = _FakeMTP
    config_mod = types.ModuleType("calibre.utils.config")
    config_mod.JSONConfig = _FakeJSONConfig

    monkeypatch.setitem(sys.modules, "calibre", types.ModuleType("calibre"))
    monkeypatch.setitem(sys.modules, "calibre.devices", types.ModuleType("calibre.devices"))
    monkeypatch.setitem(sys.modules, "calibre.devices.mtp", types.ModuleType("calibre.devices.mtp"))
    monkeypatch.setitem(sys.modules, "calibre.devices.mtp.driver", driver_mod)
    monkeypatch.setitem(sys.modules, "calibre.utils", types.ModuleType("calibre.utils"))
    monkeypatch.setitem(sys.modules, "calibre.utils.config", config_mod)
    return _FakeMTP, instances


def test_build_driver_calls_startup_and_sets_gui_attributes(monkeypatch):
    """NAS bug: detect_managed_devices() internally opens the device, which
    raises AttributeError on `prefs` unless the GUI-side init has run.
    Verify _build_driver sets all three attributes BEFORE returning."""
    _FakeMTP, instances = _install_fake_calibre(monkeypatch)

    drv = mtp_helper._build_driver()

    assert isinstance(drv, _FakeMTP)
    assert drv.startup_called is True
    # The three attributes the upstream GUI device-manager init sets:
    assert hasattr(drv, "prefs")
    assert drv.prefs.name == "mtp_devices"  # JSONConfig("mtp_devices")
    assert callable(drv.report_progress)
    # report_progress should be a safe no-op
    drv.report_progress("anything", percent=50)
    assert drv.current_friendly_name is None


def test_cli_detect_uses_build_driver(monkeypatch, capsys):
    """Smoke-test: _cli_detect goes through _build_driver, so a fake driver
    that records detect_managed_devices is called and returns no devices."""
    _FakeMTP, instances = _install_fake_calibre(monkeypatch)

    # Extend the fake to satisfy _cli_detect's needs.
    detect_calls: list[object] = []

    def detect_managed_devices(self, scanner_devices):
        detect_calls.append(scanner_devices)
        return []

    _FakeMTP.detect_managed_devices = detect_managed_devices

    # Provide a minimal scanner module.
    scanner_mod = types.ModuleType("calibre.devices.scanner")

    class _FakeScanner:
        def __init__(self):
            self.devices = []

        def scan(self):
            pass

    scanner_mod.DeviceScanner = _FakeScanner
    monkeypatch.setitem(sys.modules, "calibre.devices.scanner", scanner_mod)

    mtp_helper._cli_detect()

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload == {"connected": False, "device": None}
    # The fake driver instance was created with .startup() and .prefs set,
    # then detect_managed_devices() was called on it.
    assert len(instances) == 1
    assert instances[0].startup_called is True
    assert detect_calls == [[]]


async def test_non_overlap_lock_serialises_calls(monkeypatch):
    """Two concurrent detect() calls should not overlap subprocesses."""

    active = 0
    max_active = 0
    log: list[str] = []

    class _SlowProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1
            return b'{"connected": false, "device": null}', b""

    async def fake_exec(*_cmd, **_kw):
        log.append("spawned")
        return _SlowProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await asyncio.gather(mtp_helper.detect(), mtp_helper.detect())

    assert max_active == 1
    assert log.count("spawned") == 2
