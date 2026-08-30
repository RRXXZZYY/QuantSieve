from __future__ import annotations

from typing import Protocol

from .models import MonitorEvent


class MonitorPlugin(Protocol):
    """Public extension point invoked after new visible events are stored."""

    name: str

    async def on_events(self, events: list[MonitorEvent]) -> None:
        """Process newly stored events without changing core monitor behavior."""
