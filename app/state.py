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
    on_device_filenames: set[str] = field(default_factory=set)

    def is_connected(self) -> bool:
        return bool(self.detect and self.detect.connected)
