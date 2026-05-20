from __future__ import annotations


def test_health_returns_200_when_all_ok(client, monkeypatch):
    # MTP check uses ctypes.CDLL("libmtp.so.9") which may not be installed in the
    # CI environment; stub it.
    import ctypes

    monkeypatch.setattr(ctypes, "CDLL", lambda name: None)

    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["db"] == "ok"
    assert body["mtp"] == "ok"
    assert body["books"] == "writable"


def test_health_503_when_libmtp_missing(client, monkeypatch):
    import ctypes

    def fail(name):
        raise OSError("not found")

    monkeypatch.setattr(ctypes, "CDLL", fail)

    resp = client.get("/health")

    assert resp.status_code == 503
    assert "missing libmtp" in resp.json()["mtp"]


def test_health_503_when_poller_records_error(client, monkeypatch):
    """NAS bug: previously /health returned mtp:ok even when MTP_DEVICE init
    failed silently. With the deepened check, a recorded poller error surfaces
    as mtp:error and the response is 503."""
    import ctypes

    monkeypatch.setattr(ctypes, "CDLL", lambda name: None)

    client.app.state.device_state.last_detect_error = (
        "AttributeError: 'MTP_DEVICE' object has no attribute 'prefs'"
    )

    resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert "error" in body["mtp"]
    assert "prefs" in body["mtp"]


def test_health_ok_when_poller_clear_after_error(client, monkeypatch):
    """If a previous error was cleared (poller recovered), /health goes back to ok."""
    import ctypes

    monkeypatch.setattr(ctypes, "CDLL", lambda name: None)

    state = client.app.state.device_state
    state.last_detect_error = "old error"
    state.last_detect_error = None  # poller recovered

    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["mtp"] == "ok"


def test_health_env_returns_uid_gid(client):
    resp = client.get("/health/env")
    body = resp.json()

    assert isinstance(body["uid"], int)
    assert isinstance(body["gid"], int)
