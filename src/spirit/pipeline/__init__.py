"""
Pipeline event bus for coordinating OHLC -> D-Limit -> Spirit data flow.

Replaces cron-based batch processing with event-driven NOTIFY/LISTEN,
ensuring Spirit never evaluates with stale upstream data.

Implementations:
  - PgEventBus: gateway + offline calibrators (direct PG LISTEN/NOTIFY)
  - WsEventBus: api-driven Spirit clients (WebSocket to gateway /v1/events)
  - DataReadinessGate(event_bus=None): always returns ready (replay / CSV)
"""

from spirit.pipeline.event_bus import PipelineEvent, PipelineEventBus
from spirit.pipeline.readiness_gate import DataReadinessGate
from spirit.pipeline.event_logger import record_pipeline_event
from spirit.pipeline.daemon_health import record_heartbeat
from spirit.pipeline.ws_event_bus import WsEventBus, derive_ws_url

__all__ = [
    'PipelineEvent',
    'PipelineEventBus',
    'DataReadinessGate',
    'WsEventBus',
    'derive_ws_url',
    'record_pipeline_event',
    'record_heartbeat',
]
