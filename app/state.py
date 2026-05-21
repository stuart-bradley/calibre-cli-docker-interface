"""Shared mutable app state — current device snapshot and on-device file cache.

The 5-second device-status poller writes here; the listing view reads from here
to render on-device badges.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.mtp_helper import DetectResult


@dataclass
class DeviceState:
    detect: DetectResult | None = None
    # Seeded by a one-shot MTP listing the poller spawns on each
    # device-connect event, then maintained by the optimistic add/discard
    # in app.handlers after each user-initiated send/remove. The sync
    # task merges into this set rather than overwriting, so optimistic
    # entries added mid-flight aren't lost.
    on_device_filenames: set[str] = field(default_factory=set)
    # Last error from the device poller. None = the most recent tick succeeded
    # (whether or not a device was plugged in). /health reads this to
    # distinguish "MTP stack working but no device" from "MTP stack broken".
    last_detect_error: str | None = None
    # True once the poller has finished at least one tick. /health stays
    # pessimistic until then so a slow first init doesn't get a false-OK.
    has_polled: bool = False
    # Sync-state for the per-connection MTP file listing. Reset every time
    # the device leaves the USB bus. files_synced is the terminal flag for
    # the session (set on success OR after max attempts give up); the
    # backoff schedule lives in services.device.
    files_synced: bool = False
    sync_attempts: int = 0
    next_sync_at: float = 0.0
    sync_in_progress: bool = False

    def is_connected(self) -> bool:
        return bool(self.detect and self.detect.connected)
