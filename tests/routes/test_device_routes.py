from __future__ import annotations

import time

from app.services import mtp_helper
from app.state import DeviceState


def test_device_status_disconnected_by_default(client):
    resp = client.get("/device/status")

    assert resp.status_code == 200
    assert "No device" in resp.text


def test_device_status_connected_renders_name(client):
    client.app.state.device_state.detect = mtp_helper.DetectResult(
        connected=True,
        device=mtp_helper.Device(name="Kindle PW3", vid="1949", pid="9981"),
    )
    resp = client.get("/device/status")

    assert "Device connected: Kindle PW3" in resp.text


def test_device_status_sets_body_attribute_when_connected(client):
    client.app.state.device_state.detect = mtp_helper.DetectResult(
        connected=True,
        device=mtp_helper.Device(name="Kindle", vid="x", pid="y"),
    )
    resp = client.get("/device/status")

    assert 'data-connected="true"' in resp.text
    # The inline script that flips body.dataset.deviceConnected lives in this fragment.
    assert "deviceConnected" in resp.text


def test_device_status_disconnected_clears_body_attribute(client):
    resp = client.get("/device/status")

    assert 'data-connected="false"' in resp.text
    # When disconnected the script should remove the body attribute, not set it.
    assert "delete document.body.dataset.deviceConnected" in resp.text


def test_library_page_renders_with_device_only_class(client):
    """Send/Remove buttons should be wrapped in .cwc-device-only so CSS can hide
    them when no device is connected."""
    resp = client.get("/")

    assert resp.status_code == 200
    assert 'class="cwc-device-only"' in resp.text
    assert "Send to device" in resp.text  # still in DOM, just CSS-hidden


def test_library_body_attribute_when_device_connected(client):
    state: DeviceState = client.app.state.device_state
    state.detect = mtp_helper.DetectResult(
        connected=True,
        device=mtp_helper.Device(name="Kindle", vid="x", pid="y"),
    )

    resp = client.get("/")
    assert 'data-device-connected="true"' in resp.text


def test_library_body_attribute_absent_when_disconnected(client):
    resp = client.get("/")
    assert "data-device-connected" not in resp.text


def test_on_device_badge_appears_for_matching_filename(client):
    # Book #1 fixture has file "Children of Time.epub" on disk.
    state: DeviceState = client.app.state.device_state
    state.detect = mtp_helper.DetectResult(
        connected=True,
        device=mtp_helper.Device(name="Kindle", vid="x", pid="y"),
    )
    state.on_device_filenames = {"Children of Time.epub"}

    resp = client.get("/")

    assert "on device" in resp.text


def test_on_device_badge_absent_when_no_match(client):
    state: DeviceState = client.app.state.device_state
    state.detect = mtp_helper.DetectResult(
        connected=True,
        device=mtp_helper.Device(name="Kindle", vid="x", pid="y"),
    )
    state.on_device_filenames = {"NotInLibrary.epub"}

    resp = client.get("/")

    assert "on device" not in resp.text


def test_batch_send_skips_books_with_no_compatible_format(client, monkeypatch):
    """US-7: books without a compatible format are skipped and reported."""
    state: DeviceState = client.app.state.device_state
    state.detect = mtp_helper.DetectResult(
        connected=True,
        device=mtp_helper.Device(name="Kindle", vid="x", pid="y"),
    )

    # Override device format order to ONLY accept TXT — no fixture book has it.
    client.app.state.settings.device_format_order.clear()
    client.app.state.settings.device_format_order.append("TXT")

    resp = client.post("/batch/send", data={"book_id": ["1", "2"]})
    assert resp.status_code == 200

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        send_jobs = [j for j in client.app.state.worker.list_jobs(100) if j.kind == "send"]
        if send_jobs and send_jobs[0].state in ("done", "failed"):
            job = send_jobs[0]
            assert all(p.state == "skipped" for p in job.progress)
            assert "skipped 2" in (job.summary or "")
            return
        time.sleep(0.02)
    raise AssertionError("send job did not complete in time")
