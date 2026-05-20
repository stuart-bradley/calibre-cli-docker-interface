from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from app.services import snapshot


@pytest.fixture
def library(tmp_path: Path) -> Path:
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / "metadata.db").write_bytes(b"calibre-db-bytes")
    return lib


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _make_snapshot(data_path: Path, d: date, contents: bytes = b"old") -> Path:
    snaps = data_path / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    path = snaps / f"metadata-{d.isoformat()}.db"
    path.write_bytes(contents)
    return path


def test_creates_snapshot_when_absent(library, data_dir):
    today = date(2026, 5, 20)

    result = snapshot.snapshot_if_needed(library, data_dir, 14, today=today)

    assert result is not None
    assert result.name == "metadata-2026-05-20.db"
    assert result.read_bytes() == b"calibre-db-bytes"


def test_idempotent_when_today_exists(library, data_dir):
    today = date(2026, 5, 20)
    existing = _make_snapshot(data_dir, today, b"existing")
    mtime_before = existing.stat().st_mtime

    result = snapshot.snapshot_if_needed(library, data_dir, 14, today=today)

    assert result is None
    assert existing.read_bytes() == b"existing"
    assert existing.stat().st_mtime == mtime_before


def test_prunes_old_snapshots_when_writing_new(library, data_dir):
    today = date(2026, 5, 20)
    tomorrow = today + timedelta(days=1)

    keep = _make_snapshot(data_dir, today)
    keep_edge = _make_snapshot(data_dir, today - timedelta(days=12))   # within 14 of tomorrow
    drop_old = _make_snapshot(data_dir, today - timedelta(days=14))    # outside 14 of tomorrow
    drop_older = _make_snapshot(data_dir, today - timedelta(days=30))

    snapshot.snapshot_if_needed(library, data_dir, 14, today=tomorrow)

    # tomorrow's exists; today and 12-days-ago survive; older deleted.
    snaps = sorted((data_dir / "snapshots").iterdir())
    names = [p.name for p in snaps]
    assert f"metadata-{tomorrow.isoformat()}.db" in names
    assert keep.name in names
    assert keep_edge.name in names
    assert drop_old.name not in names
    assert drop_older.name not in names


def test_creates_snapshots_dir_when_missing(library, data_dir):
    assert not data_dir.exists()

    snapshot.snapshot_if_needed(library, data_dir, 14, today=date(2026, 5, 20))

    assert (data_dir / "snapshots").is_dir()


def test_retention_one_keeps_only_today(library, data_dir):
    today = date(2026, 5, 20)
    _make_snapshot(data_dir, today - timedelta(days=1))
    _make_snapshot(data_dir, today - timedelta(days=2))
    tomorrow = today + timedelta(days=1)

    snapshot.snapshot_if_needed(library, data_dir, 1, today=tomorrow)

    names = sorted(p.name for p in (data_dir / "snapshots").iterdir())
    assert names == [f"metadata-{tomorrow.isoformat()}.db"]


def test_unrelated_files_in_snapshot_dir_are_left_alone(library, data_dir):
    today = date(2026, 5, 20)
    snaps = data_dir / "snapshots"
    snaps.mkdir(parents=True)
    stray = snaps / "notes.txt"
    stray.write_text("don't touch me")
    _make_snapshot(data_dir, today - timedelta(days=30))

    snapshot.snapshot_if_needed(library, data_dir, 14, today=today + timedelta(days=1))

    assert stray.exists()
