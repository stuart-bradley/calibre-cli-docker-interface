from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.services import db
from app.state import DeviceState

router = APIRouter()


@router.get("/health")
def health(request: Request, settings: Settings = Depends(get_settings)):
    checks: dict[str, str] = {}
    ok = True

    try:
        with db.connect(settings.library_path) as conn:
            conn.execute("SELECT 1 FROM books LIMIT 1").fetchone()
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"error: {exc}"
        ok = False

    try:
        import ctypes
        ctypes.CDLL("libmtp.so.9")
    except OSError:
        checks["mtp"] = "missing libmtp.so.9"
        ok = False
    else:
        # libmtp loads, but that's not enough — the headless MTP_DEVICE init
        # or detect_managed_devices() can still fail (e.g. missing GUI-side
        # attributes on the driver). Surface the most recent poller error so a
        # broken init reports here instead of staying invisibly stuck at "no
        # device". A None error (or pre-poll state) is treated as ok — libmtp
        # loading is the floor.
        state: DeviceState | None = getattr(request.app.state, "device_state", None)
        if state is not None and state.last_detect_error:
            checks["mtp"] = f"error: {state.last_detect_error}"
            ok = False
        else:
            checks["mtp"] = "ok"

    try:
        probe = settings.library_path / ".cwc-write-probe"
        probe.touch()
        probe.unlink()
        checks["books"] = "writable"
    except OSError as exc:
        checks["books"] = f"not writable: {exc}"
        ok = False

    status_code = 200 if ok else 503
    return JSONResponse(checks, status_code=status_code)


@router.get("/health/env")
def health_env():
    """Diagnostic — confirms PUID/PGID resolution at runtime."""
    return {"uid": os.getuid(), "gid": os.getgid()}
