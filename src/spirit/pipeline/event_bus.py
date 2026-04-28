"""
PipelineEvent dataclass and PipelineEventBus protocol.

Defines the contract for pipeline stage coordination.
Implementations: PgEventBus (Pro), future InMemoryEventBus (Lite).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

# PG NOTIFY payloads are capped at 8000 bytes (NOTIFY_PAYLOAD_MAX_LENGTH).
# Leave a 1000-byte safety margin: above this, drop `row` and log a warning
# so we never publish a malformed-truncated payload. Client falls back to a
# direct PG fetch when `row` is absent (#493 stage 3).
_NOTIFY_PAYLOAD_SAFETY_BYTES = 7000

_size_logger = logging.getLogger("pipeline_event_bus")


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
    row: Optional[Dict[str, Any]] = None  # canonical stage-row payload (#493)

    def to_json(self) -> str:
        data = asdict(self)
        payload = json.dumps(data, default=str)

        if len(payload.encode("utf-8")) > _NOTIFY_PAYLOAD_SAFETY_BYTES and data.get("row"):
            _size_logger.warning(
                "[PAYLOAD-SIZE] %s %s candle=%s exceeded %d-byte safety threshold "
                "(%d bytes) — dropping `row`; client will fall back to fetch",
                self.stage, self.pair, self.candle_dt,
                _NOTIFY_PAYLOAD_SAFETY_BYTES, len(payload.encode("utf-8")),
            )
            data["row"] = None
            if isinstance(data.get("metadata"), dict):
                data["metadata"] = {**data["metadata"], "row": None}
            payload = json.dumps(data, default=str)

        return payload

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
