from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.config import Settings, get_settings
from tests.fakes.mtp import FakeMtpBackend

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
SOURCE_LIBRARY = FIXTURE_DIR / "library_minimal"
SOURCE_DB = FIXTURE_DIR / "metadata_minimal.db"


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """A writable copy of the fixture library — db, book files and cover stubs."""
    lib = tmp_path / "library"
    shutil.copytree(SOURCE_LIBRARY, lib)
    shutil.copy2(SOURCE_DB, lib / "metadata.db")
    return lib


@pytest.fixture
def settings(library: Path, tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setenv("LIBRARY_PATH", str(library))
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "data"))
    monkeypatch.setenv("CALIBRE_WEB_CLI_PASSWORD", "")
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def fake_mtp_backend(monkeypatch):
    """An in-memory MTP backend installed for the test.

    A single device is pre-populated with the canonical ``documents/`` and
    ``system/thumbnails/`` folders so handler-stack tests can exercise real
    sends/removes/thumbnail uploads without per-test setup. Tests that need
    extra state (preexisting files on the device, multiple devices, failure
    injection) reach into this fixture directly.
    """
    backend = FakeMtpBackend()
    device = backend.add_device()
    device.ensure_documents()
    device.ensure_system_thumbnails()

    from app.services import mtp_helper

    monkeypatch.setattr(mtp_helper, "_backend", backend)
    return backend


@pytest.fixture
def app(settings: Settings, fake_mtp_backend):
    from app.main import create_app

    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c
