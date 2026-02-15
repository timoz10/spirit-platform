import threading
import pandas as pd


class TimeoutException(Exception):
    pass


# Context storage: supports both single SpiritContext and ContextManager.
# Thread-local _active_pair ensures each thread routes to the right pair.
_spirit_context = None      # SpiritContext or ContextManager
_active_pair = threading.local()


def set_spirit_context(context):
    """Register a SpiritContext or ContextManager as the data source."""
    global _spirit_context
    _spirit_context = context


def set_active_pair(pair: str):
    """Set the active pair for this thread (used by get_spirit_temp_ti routing)."""
    _active_pair.pair = pair


def get_active_pair() -> str | None:
    """Get the active pair for this thread, or None."""
    return getattr(_active_pair, 'pair', None)


def get_spirit_temp_ti(timeout_seconds=30, db_path=None, pair: str | None = None):
    """
    Fetch all rows from spirit_temp_ti as a DataFrame.

    Routing logic:
      1. If ContextManager is registered: use explicit pair arg, else thread-local
         _active_pair, else first pair in the manager.
      2. If single SpiritContext: return its feature_df directly.
      3. Otherwise: raise RuntimeError.

    Args:
        timeout_seconds: Ignored (kept for backward compat)
        db_path: Ignored (kept for backward compat)
        pair: Explicit pair to fetch. If None, uses thread-local active pair.
    """
    if _spirit_context is None:
        raise RuntimeError(
            "SpiritContext not initialized. Call set_spirit_context() first "
            "(spirit_main.py does this automatically at startup)."
        )

    # Check if it's a ContextManager (has .get() and .all_pairs())
    if hasattr(_spirit_context, 'all_pairs'):
        p = pair or getattr(_active_pair, 'pair', None)
        if p is None:
            # Fallback to first pair
            pairs = _spirit_context.all_pairs()
            if pairs:
                p = pairs[0]
            else:
                return pd.DataFrame()
        try:
            return _spirit_context.get(p).get_feature_df()
        except KeyError:
            return pd.DataFrame()

    # Single SpiritContext path
    return _spirit_context.get_feature_df()
