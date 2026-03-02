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
) -> bool:
    """UPSERT a heartbeat row for this daemon.

    Args:
        daemon_id: Unique daemon identifier (e.g. 'ohlc', 'dlimit60-XBTUSD')
        status: 'ok', 'error', or 'starting'
        metadata: Optional JSON-serializable dict (zones_count, last_candle, etc.)

    Returns:
        True if write succeeded, False otherwise.
    """
    try:
        from spirit.utils.db_connection import execute_query
        execute_query(
            """
            INSERT INTO daemon_heartbeats (daemon_id, last_heartbeat, status, metadata)
            VALUES (%s, NOW(), %s, %s)
            ON CONFLICT (daemon_id) DO UPDATE SET
                last_heartbeat = NOW(),
                status = EXCLUDED.status,
                metadata = EXCLUDED.metadata
            """,
            (
                daemon_id,
                status,
                json.dumps(metadata) if metadata else None,
            ),
            fetch='none',
        )
        return True
    except Exception as e:
        logger.warning(f"[HEARTBEAT] Failed to write heartbeat for {daemon_id}: {e}")
        return False
