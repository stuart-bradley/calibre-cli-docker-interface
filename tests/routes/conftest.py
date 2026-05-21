from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.config import Settings, get_settings

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
def app(settings: Settings, monkeypatch):
    # Stub MTP helper so route tests don't try to spawn calibre-debug.
    from app.services import mtp_helper

    async def fake_detect(**kw):
        return mtp_helper.DetectResult(connected=False, device=None)

    async def fake_list_files(**kw):
        return []

    async def fake_send(*a, **kw):
        return "documents/stub"

    async def fake_remove(*a, **kw):
        return None

    async def fake_send_thumbnail(*a, **kw):
        return "system/thumbnails/stub"

    async def fake_remove_thumbnail(*a, **kw):
        return False

    monkeypatch.setattr(mtp_helper, "detect", fake_detect)
    monkeypatch.setattr(mtp_helper, "list_files", fake_list_files)
    monkeypatch.setattr(mtp_helper, "send", fake_send)
    monkeypatch.setattr(mtp_helper, "remove", fake_remove)
    monkeypatch.setattr(mtp_helper, "send_thumbnail", fake_send_thumbnail)
    monkeypatch.setattr(mtp_helper, "remove_thumbnail", fake_remove_thumbnail)

    from app.main import create_app

    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c
