"""
Pipeline event bus for coordinating OHLC -> D-Limit -> Spirit data flow.

Replaces cron-based batch processing with event-driven NOTIFY/LISTEN,
ensuring Spirit never evaluates with stale upstream data.

Architecture supports Lite/Pro split:
  - PgEventBus: Production (PG LISTEN/NOTIFY)
  - Future InMemoryEventBus: Lite (threading.Event, no PG)
  - DataReadinessGate(event_bus=None): always returns ready (Lite/replay)
"""

from spirit.pipeline.event_bus import PipelineEvent, PipelineEventBus
from spirit.pipeline.readiness_gate import DataReadinessGate
from spirit.pipeline.event_logger import record_pipeline_event
from spirit.pipeline.daemon_health import record_heartbeat

__all__ = [
    'PipelineEvent',
    'PipelineEventBus',
    'DataReadinessGate',
    'record_pipeline_event',
    'record_heartbeat',
]
