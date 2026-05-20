from __future__ import annotations

import os

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.services import db

router = APIRouter()


@router.get("/health")
def health(settings: Settings = Depends(get_settings)):
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
        checks["mtp"] = "ok"
    except OSError:
        checks["mtp"] = "missing libmtp.so.9"
        ok = False

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
