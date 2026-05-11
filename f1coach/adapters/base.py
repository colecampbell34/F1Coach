from __future__ import annotations

from typing import Protocol

from f1coach.models import TelemetryMessage


class UnsupportedPacket(ValueError):
    """Raised when a packet is valid enough to identify but not supported."""


class TelemetryAdapter(Protocol):
    name: str

    def decode(self, packet: bytes) -> TelemetryMessage | None:
        """Decode a raw UDP packet into a normalized telemetry message."""
