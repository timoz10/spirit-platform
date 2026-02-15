import pandas as pd
import sqlite3
import concurrent.futures
from system_config import DB_PATH as DEFAULT_DB_PATH


class TimeoutException(Exception):
    pass


# SpiritContext override: when set, get_spirit_temp_ti() reads from the
# in-memory context instead of SQLite. Set by spirit_main.py when
# SPIRIT_V2_CONTEXT=1.
_spirit_context = None


def set_spirit_context(context):
    """Register a SpiritContext as the data source for get_spirit_temp_ti()."""
    global _spirit_context
    _spirit_context = context


def _read_spirit_temp_ti(db_path):
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        df = pd.read_sql_query("SELECT * FROM spirit_temp_ti ORDER BY datetime ASC", conn)
        conn.close()
        return df
    except Exception as e:
        raise RuntimeError(f"Error reading from spirit_temp_ti: {e}")


def get_spirit_temp_ti(timeout_seconds=30, db_path=DEFAULT_DB_PATH):
    """
    Fetch all rows from spirit_temp_ti as a DataFrame.

    When SPIRIT_V2_CONTEXT is active, reads from in-memory SpiritContext
    instead of SQLite. Falls back to SQLite if no context is registered.
    """
    # V2 path: read from in-memory context
    if _spirit_context is not None:
        return _spirit_context.get_feature_df()

    # V1 path: read from SQLite with timeout
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_read_spirit_temp_ti, db_path)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            raise TimeoutException(f"Timed out after {timeout_seconds}s while reading spirit_temp_ti from database.")
