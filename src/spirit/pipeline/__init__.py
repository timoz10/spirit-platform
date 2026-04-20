"""
Pipeline event bus for coordinating OHLC -> D-Limit -> Spirit data flow.

Replaces cron-based batch processing with event-driven NOTIFY/LISTEN,
ensuring Spirit never evaluates with stale upstream data.

Implementations:
  - PgEventBus: gateway + offline calibrators (direct PG LISTEN/NOTIFY)
  - WsEventBus: api-driven Spirit clients (WebSocket to gateway /v1/events)
  - PipelineFreshnessCache: push-updated non-blocking readiness check
    (replaces DataReadinessGate's blocking wait_for_* pattern)
"""

from spirit.pipeline.event_bus import PipelineEvent, PipelineEventBus
from spirit.pipeline.readiness_gate import DataReadinessGate
from spirit.pipeline.event_logger import record_pipeline_event
from spirit.pipeline.daemon_health import record_heartbeat
from spirit.pipeline.freshness_cache import PipelineFreshnessCache

# WsEventBus lazy-imported below — it depends on `websockets`, which is only
# installed on api-mode consumers (canary, prod). Ingestion hosts (DEV daemons)
# don't need it, and an eager import here used to crash their pipeline imports
# when websockets was absent — silently disabling event emission. See incident
# 2026-04-19/20.
__all__ = [
    'PipelineEvent',
    'PipelineEventBus',
    'DataReadinessGate',
    'PipelineFreshnessCache',
    'WsEventBus',
    'derive_ws_url',
    'record_pipeline_event',
    'record_heartbeat',
]


def __getattr__(name):
    if name in ('WsEventBus', 'derive_ws_url'):
        from spirit.pipeline.ws_event_bus import WsEventBus, derive_ws_url
        globals()['WsEventBus'] = WsEventBus
        globals()['derive_ws_url'] = derive_ws_url
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
