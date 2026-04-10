"""
Pair Registry — single source of truth for active trading pairs.

Reads from the `pair_registry` table in PostgreSQL. All consumers
(Spirit, pipeline daemons, cron scripts) should use get_active_pairs()
instead of hardcoded lists or config values.

Multi-instance support (#225): When `instance` is passed, only pairs with
matching instance OR NULL instance (shared) are returned.

Upstream Dependencies: None (reads directly from pair_registry table)
Outputs: List of active pair strings, e.g. ['ATOMUSD', 'ETHUSD', ...]
"""

import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Thread-safe cache — keyed by instance name for multi-instance isolation
_cache_lock = threading.Lock()
_cached_pairs: Dict[Optional[str], List[str]] = {}
_cache_timestamps: Dict[Optional[str], float] = {}

# Cache for instance column existence check (one-time per process)
_instance_column_checked: Optional[bool] = None


def _has_instance_column() -> bool:
    """Check if pair_registry has the 'instance' column (migration 023)."""
    global _instance_column_checked
    if _instance_column_checked is not None:
        return _instance_column_checked
    try:
        from spirit.utils.db_connection import execute_query
        row = execute_query(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'pair_registry' AND column_name = 'instance'",
            fetch='one',
        )
        _instance_column_checked = row is not None
    except Exception:
        _instance_column_checked = False
    return _instance_column_checked


def get_active_pairs(cache_ttl: int = 300, instance: Optional[str] = None) -> List[str]:
    """
    Return active trading pairs from pair_registry table.

    Args:
        cache_ttl: Cache lifetime in seconds (default 5 minutes).
        instance: Spirit instance name (e.g. 'prod', 'davy'). When set,
                  returns pairs where instance IS NULL (shared) OR matches.
                  When None, returns all active pairs (backward compat for crons).

    Returns:
        Sorted list of active pair strings (e.g. ['ATOMUSD', 'ETHUSD', ...]).

    Raises:
        RuntimeError: If the registry returns no active pairs.
        Exception: DB connection errors propagate (fail loudly).
    """
    now = time.monotonic()

    with _cache_lock:
        cached = _cached_pairs.get(instance)
        ts = _cache_timestamps.get(instance, 0.0)
        if cached and (now - ts) < cache_ttl:
            return list(cached)

    # Query outside the lock to avoid holding it during I/O
    from spirit.utils.db_connection import execute_query

    if instance and _has_instance_column():
        rows = execute_query(
            "SELECT pair FROM pair_registry "
            "WHERE active = true AND (instance IS NULL OR instance = %s) "
            "ORDER BY pair",
            (instance,),
        )
    else:
        rows = execute_query(
            "SELECT pair FROM pair_registry WHERE active = true ORDER BY pair"
        )

    pairs = [row['pair'] for row in rows] if rows else []

    if not pairs:
        raise RuntimeError(
            "pair_registry returned no active pairs. "
            "Check the pair_registry table has rows with active=true."
        )

    with _cache_lock:
        _cached_pairs[instance] = pairs
        _cache_timestamps[instance] = time.monotonic()

    logger.debug(f"[PAIR_REGISTRY] Loaded {len(pairs)} active pairs (instance={instance}): {pairs}")
    return list(pairs)


def invalidate_cache() -> None:
    """Clear the cached pairs, forcing a fresh DB query on next call."""
    with _cache_lock:
        _cached_pairs.clear()
        _cache_timestamps.clear()
