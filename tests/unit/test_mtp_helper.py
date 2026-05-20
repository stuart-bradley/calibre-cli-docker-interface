from __future__ import annotations

import asyncio
import json
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
