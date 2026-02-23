"""
PipelineEvent dataclass and PipelineEventBus protocol.

Defines the contract for pipeline stage coordination.
Implementations: PgEventBus (Pro), future InMemoryEventBus (Lite).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable


@dataclass
class PipelineEvent:
    """A single pipeline stage completion event.

    Serialises to/from JSON for PG NOTIFY payloads.
    """
    stage: str                    # e.g. 'ohlc', 'dlimit_60m', 'dlimit_15m'
    pair: str                     # e.g. 'XBTUSD'
    interval_minutes: int         # e.g. 60
    candle_dt: str                # ISO-8601 string of the candle datetime
    rows_affected: int = 0
    duration_ms: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, payload: str) -> PipelineEvent:
        data = json.loads(payload)
        return cls(**data)

    @property
    def channel(self) -> str:
        """PG NOTIFY channel name for this event's stage."""
        return f"pipeline_{self.stage}"


# Type alias for subscriber callbacks
EventCallback = Callable[[PipelineEvent], None]


@runtime_checkable
class PipelineEventBus(Protocol):
    """Abstract interface for pipeline event coordination.

    Implementations must be thread-safe.
    """

    def publish(self, event: PipelineEvent) -> None:
        """Publish an event to the bus (fires NOTIFY in PG implementation)."""
        ...

    def subscribe(self, channel: str, callback: EventCallback) -> None:
        """Register a callback for events on the given channel.

        Args:
            channel: Channel name (e.g. 'pipeline_ohlc')
            callback: Called with PipelineEvent when event arrives
        """
        ...

    def wait_for(
        self,
        channel: str,
        pair: str,
        candle_dt: str,
        timeout: float = 60.0,
    ) -> Optional[PipelineEvent]:
        """Block until a matching event arrives or timeout.

        Args:
            channel: Channel to listen on
            pair: Expected pair
            candle_dt: Expected candle datetime (ISO string)
            timeout: Max seconds to wait

        Returns:
            PipelineEvent if received, None on timeout
        """
        ...

    def start(self) -> None:
        """Start the event bus (background listener thread)."""
        ...

    def stop(self) -> None:
        """Stop the event bus and clean up connections."""
        ...
