from __future__ import annotations

import base64
import binascii
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

EXEMPT_PREFIXES = ("/health", "/static/")
REALM = "calibre-cli-docker-interface"
_DUMMY = "x" * 32


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, password: str | None) -> None:
        super().__init__(app)
        self._password = (password or "").strip()
        self._enabled = bool(self._password)

    async def dispatch(self, request: Request, call_next):
        if not self._enabled:
            return await call_next(request)

        if any(request.url.path.startswith(p) for p in EXEMPT_PREFIXES):
            return await call_next(request)

        submitted = _extract_password(request.headers.get("authorization", ""))
        expected = self._password
        match = secrets.compare_digest(
            (submitted or _DUMMY).encode("utf-8"),
            expected.encode("utf-8"),
        )

        if not submitted or not match:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": f'Basic realm="{REALM}"'},
            )

        return await call_next(request)


def _extract_password(header: str) -> str | None:
    if not header or not header.lower().startswith("basic "):
        return None
    encoded = header[6:].strip()
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    _user, password = decoded.split(":", 1)
    return password
