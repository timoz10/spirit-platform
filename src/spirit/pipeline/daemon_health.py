"""
Daemon heartbeat writer: UPSERTs a row per daemon into daemon_heartbeats.

Every pipeline daemon (OHLC, D-Limit 60m, D-Limit 15m) calls
record_heartbeat() periodically. A separate cron checker
(scripts/check_daemon_health.py) queries the table and creates
GitHub Issues for stale daemons.

Non-fatal by design: if the DB write fails the daemon keeps running.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from spirit.logger import get_logger

logger = get_logger("daemon_health")


def record_heartbeat(
    daemon_id: str,
    status: str = 'ok',
    metadata: Optional[Dict[str, Any]] = None,
    run_id: str = 'live',
    instance: str = 'prod',
) -> bool:
    """UPSERT a heartbeat row for this daemon.

    Args:
        daemon_id: Unique daemon identifier (e.g. 'ohlc', 'dlimit60-XBTUSD')
        status: 'ok', 'error', or 'starting'
        metadata: Optional JSON-serializable dict (zones_count, last_candle, etc.)
        run_id: Only 'live' writes to the table — test/validate/replay are skipped
                to prevent dev runs from overwriting production heartbeats.
        instance: Spirit instance name (e.g. 'prod', 'davy'). Part of the
                  composite PK so different instances can't overwrite each other.

    Returns:
        True if write succeeded (or skipped for non-live), False otherwise.
    """
    if run_id != 'live':
        return True  # silently skip — dev/test must not touch prod heartbeats

    try:
        from spirit.utils.data_provider import get_data_provider
        get_data_provider().write_heartbeat(
            daemon_id, status=status, metadata=metadata, run_id=run_id,
        )
        return True
    except Exception as e:
        logger.warning(f"[HEARTBEAT] Failed to write heartbeat for {daemon_id}: {e}")
        return False
