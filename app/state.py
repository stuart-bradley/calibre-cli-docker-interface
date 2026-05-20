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
    # Populated only by the optimistic add/discard in app.handlers after a
    # user-initiated send/remove. The poller never lists files (see
    # services.device for the firmware-bricks-on-MTP-open rationale), so
    # badges for books already on the device pre-app are not shown.
    on_device_filenames: set[str] = field(default_factory=set)
    # Last error from the device poller. None = the most recent tick succeeded
    # (whether or not a device was plugged in). /health reads this to
    # distinguish "MTP stack working but no device" from "MTP stack broken".
    last_detect_error: str | None = None
    # True once the poller has finished at least one tick. /health stays
    # pessimistic until then so a slow first init doesn't get a false-OK.
    has_polled: bool = False

    def is_connected(self) -> bool:
        return bool(self.detect and self.detect.connected)
