"""
Pair Registry — single source of truth for active trading pairs.

Reads from the `pair_registry` table in PostgreSQL. All consumers
(Spirit, pipeline daemons, cron scripts) should use get_active_pairs()
instead of hardcoded lists or config values.

Upstream Dependencies: None (reads directly from pair_registry table)
Outputs: List of active pair strings, e.g. ['ATOMUSD', 'ETHUSD', ...]
"""

import logging
import threading
import time
from typing import List

logger = logging.getLogger(__name__)

# Thread-safe cache
_cache_lock = threading.Lock()
_cached_pairs: List[str] = []
_cache_timestamp: float = 0.0


def get_active_pairs(cache_ttl: int = 300) -> List[str]:
    """
    Return active trading pairs from pair_registry table.

    Args:
        cache_ttl: Cache lifetime in seconds (default 5 minutes).

    Returns:
        Sorted list of active pair strings (e.g. ['ATOMUSD', 'ETHUSD', ...]).

    Raises:
        RuntimeError: If the registry returns no active pairs.
        Exception: DB connection errors propagate (fail loudly).
    """
    global _cached_pairs, _cache_timestamp

    now = time.monotonic()

    with _cache_lock:
        if _cached_pairs and (now - _cache_timestamp) < cache_ttl:
            return list(_cached_pairs)

    # Query outside the lock to avoid holding it during I/O
    from spirit.utils.db_connection import execute_query

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
        _cached_pairs = pairs
        _cache_timestamp = time.monotonic()

    logger.debug(f"[PAIR_REGISTRY] Loaded {len(pairs)} active pairs: {pairs}")
    return list(pairs)


def invalidate_cache() -> None:
    """Clear the cached pairs, forcing a fresh DB query on next call."""
    global _cached_pairs, _cache_timestamp

    with _cache_lock:
        _cached_pairs = []
        _cache_timestamp = 0.0
