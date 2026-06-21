from pathlib import Path

import pytest

from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _clear_env(monkeypatch):
    for key in [
        "LIBRARY_PATH",
        "DATA_PATH",
        "PUID",
        "PGID",
        "TZ",
        "CALIBRE_WEB_CLI_PORT",
        "CALIBRE_WEB_CLI_PASSWORD",
        "CALIBRE_WEB_CLI_METADATA_SOURCES",
        "CALIBRE_WEB_CLI_DEVICE_FORMAT_ORDER",
        "CALIBRE_WEB_CLI_PAGE_SIZE",
        "CALIBRE_WEB_CLI_MTP_USB_IDS",
        "CALIBRE_WEB_CLI_SNAPSHOT_RETENTION_DAYS",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir("/tmp")


def test_defaults_with_only_required_set(_clear_env, monkeypatch):
    monkeypatch.setenv("LIBRARY_PATH", "/tmp/library")

    s = Settings()

    assert s.library_path == Path("/tmp/library")
    assert s.data_path == Path("./data")
    assert s.puid == 1000
    assert s.pgid == 1000
    assert s.tz == "Europe/London"
    assert s.port == 8084
    assert s.password is None
    assert s.metadata_sources == ["Amazon", "Google"]
    assert s.device_format_order == ["AZW3", "MOBI", "PDF", "EPUB"]
    assert s.page_size == 48
    assert s.mtp_usb_ids == []
    assert s.snapshot_retention_days == 14


def test_library_path_defaults_to_container_mount(_clear_env):
    """When LIBRARY_PATH is unset, fall back to /books (the in-container mount).

    The host-side bind source is configured via LIBRARY_HOST_PATH at the
    compose layer. Defaulting here means env_file users can't accidentally
    leak the host path into the container.
    """
    assert Settings().library_path == Path("/books")


def test_password_set(_clear_env, monkeypatch):
    monkeypatch.setenv("LIBRARY_PATH", "/tmp/library")
    monkeypatch.setenv("CALIBRE_WEB_CLI_PASSWORD", "hunter2")

    assert Settings().password == "hunter2"


def test_empty_mtp_usb_ids_becomes_empty_list(_clear_env, monkeypatch):
    monkeypatch.setenv("LIBRARY_PATH", "/tmp/library")
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "")

    assert Settings().mtp_usb_ids == []


def test_mtp_usb_ids_parses_comma_list(_clear_env, monkeypatch):
    monkeypatch.setenv("LIBRARY_PATH", "/tmp/library")
    monkeypatch.setenv("CALIBRE_WEB_CLI_MTP_USB_IDS", "1949:9981,abcd:1234")

    assert Settings().mtp_usb_ids == ["1949:9981", "abcd:1234"]


def test_csv_parsing_strips_whitespace(_clear_env, monkeypatch):
    monkeypatch.setenv("LIBRARY_PATH", "/tmp/library")
    monkeypatch.setenv("CALIBRE_WEB_CLI_METADATA_SOURCES", " Amazon , Google , OpenLibrary ")

    assert Settings().metadata_sources == ["Amazon", "Google", "OpenLibrary"]


def test_device_format_order_override(_clear_env, monkeypatch):
    monkeypatch.setenv("LIBRARY_PATH", "/tmp/library")
    monkeypatch.setenv("CALIBRE_WEB_CLI_DEVICE_FORMAT_ORDER", "AZW3,EPUB")

    assert Settings().device_format_order == ["AZW3", "EPUB"]


def test_int_fields_parse(_clear_env, monkeypatch):
    monkeypatch.setenv("LIBRARY_PATH", "/tmp/library")
    monkeypatch.setenv("PUID", "1026")
    monkeypatch.setenv("PGID", "100")
    monkeypatch.setenv("CALIBRE_WEB_CLI_PORT", "9090")
    monkeypatch.setenv("CALIBRE_WEB_CLI_PAGE_SIZE", "24")
    monkeypatch.setenv("CALIBRE_WEB_CLI_SNAPSHOT_RETENTION_DAYS", "7")

    s = Settings()
    assert (s.puid, s.pgid, s.port, s.page_size, s.snapshot_retention_days) == (
        1026,
        100,
        9090,
        24,
        7,
    )


def test_get_settings_cached(_clear_env, monkeypatch):
    monkeypatch.setenv("LIBRARY_PATH", "/tmp/library")

    first = get_settings()
    second = get_settings()

    assert first is second


def test_get_settings_cache_clear_picks_up_changes(_clear_env, monkeypatch):
    monkeypatch.setenv("LIBRARY_PATH", "/tmp/library")
    first = get_settings()

    get_settings.cache_clear()
    monkeypatch.setenv("CALIBRE_WEB_CLI_PORT", "9999")
    second = get_settings()

    assert first.port == 8084
    assert second.port == 9999
