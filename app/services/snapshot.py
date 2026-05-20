from __future__ import annotations

import logging
import re
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_SNAPSHOT_RE = re.compile(r"^metadata-(\d{4})-(\d{2})-(\d{2})\.db$")


def _today_in_tz(tz_name: str | None) -> date:
    if not tz_name:
        return datetime.now(UTC).date()
    return datetime.now(ZoneInfo(tz_name)).date()


def _snapshot_dir(data_path: Path) -> Path:
    return data_path / "snapshots"


def _parse_date_from_name(name: str) -> date | None:
    match = _SNAPSHOT_RE.match(name)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _prune(snapshots_dir: Path, retention_days: int, today: date) -> list[Path]:
    cutoff = today - timedelta(days=max(retention_days - 1, 0))
    removed: list[Path] = []
    for entry in sorted(snapshots_dir.iterdir()):
        d = _parse_date_from_name(entry.name)
        if d is None:
            continue
        if d < cutoff:
            entry.unlink()
            removed.append(entry)
    return removed


def snapshot_if_needed(
    library_path: Path,
    data_path: Path,
    retention_days: int,
    *,
    today: date | None = None,
    tz: str | None = None,
) -> Path | None:
    today = today or _today_in_tz(tz)
    snapshots_dir = _snapshot_dir(data_path)
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    target = snapshots_dir / f"metadata-{today.isoformat()}.db"
    if target.exists():
        return None

    src = library_path / "metadata.db"
    shutil.copy2(src, target)

    pruned = _prune(snapshots_dir, retention_days, today)
    if pruned:
        log.info("snapshot %s; pruned %d old", target.name, len(pruned))
    else:
        log.info("snapshot %s", target.name)
    return target
