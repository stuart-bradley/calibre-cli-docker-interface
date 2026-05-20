from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.services import auth


def _basic(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _client(password: str | None) -> TestClient:
    app = FastAPI()
    app.add_middleware(auth.BasicAuthMiddleware, password=password)

    @app.get("/protected")
    def protected():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/static/foo.css")
    def static_file():
        return {"css": True}

    return TestClient(app)


def test_open_when_password_unset():
    client = _client(None)

    assert client.get("/protected").status_code == 200


def test_open_when_password_empty_string():
    client = _client("")

    assert client.get("/protected").status_code == 200


def test_401_without_credentials_when_password_set():
    client = _client("secret")

    resp = client.get("/protected")

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Basic realm=")


def test_401_with_wrong_password():
    client = _client("secret")

    resp = client.get("/protected", headers={"Authorization": _basic("anyone", "nope")})

    assert resp.status_code == 401


def test_200_with_correct_password():
    client = _client("secret")

    resp = client.get("/protected", headers={"Authorization": _basic("anyone", "secret")})

    assert resp.status_code == 200


def test_health_exempt_even_when_password_set():
    client = _client("secret")

    assert client.get("/health").status_code == 200


def test_static_exempt_even_when_password_set():
    client = _client("secret")

    assert client.get("/static/foo.css").status_code == 200


def test_uses_compare_digest(monkeypatch):
    called = []
    real = auth.secrets.compare_digest

    def wrapper(a, b):
        called.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.secrets, "compare_digest", wrapper)
    client = _client("secret")

    client.get("/protected", headers={"Authorization": _basic("u", "secret")})

    assert called, "compare_digest was not invoked"


def test_malformed_authorization_header_returns_401():
    client = _client("secret")

    no_colon = "Basic " + base64.b64encode(b"no-colon").decode()
    for bad in ["Bearer abc", "Basic not-base64!!", no_colon]:
        resp = client.get("/protected", headers={"Authorization": bad})
        assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/health", "/health/ready", "/static/foo.css", "/static/x/y.png"])
def test_exempt_paths_with_password_set(path):
    app = FastAPI()
    app.add_middleware(auth.BasicAuthMiddleware, password="secret")

    @app.get(path)
    def handler():
        return {"ok": True}

    resp = TestClient(app).get(path)

    assert resp.status_code == 200
