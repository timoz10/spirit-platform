"""
SpiritContext — Single source of truth for all Spirit runtime state.

Replaces SQLite temp tables with in-memory pandas DataFrame + PostgreSQL
persistence for crash recovery.

Components replaced:
  spirit_temp_ti  (SQLite)  → self.ohlc_df (pandas DataFrame, rolling window)
  spirit_temp_trade (SQLite) → PG spirit_state table
  PaperOrderExecutor.equity  → PG spirit_state + in-memory
  TradeStateManager.open_trade → PG spirit_state + in-memory

Always active — Spirit V2 is the only runtime path.

Author: Claude Code + Tim
Date: 2026-02-13
"""

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from spirit.logger import get_logger
from spirit.utils.feature_engineering import add_features

logger = get_logger("spirit_context")

# Maximum rows to keep in the OHLC DataFrame (per interval)
MAX_OHLC_ROWS = 720


class SpiritContext:
    """
    Single source of truth for per-pair Spirit runtime state.

    Manages:
      - OHLC data + engineered features (in-memory DataFrame)
      - Open trade state (in-memory + PG persistence)
      - Paper trading equity (in-memory + PG persistence)
      - Signal/decision history (in-memory, for web dashboard)
      - Health telemetry counters

    Each pair gets its own SpiritContext instance. PG state keys are
    pair-prefixed (e.g. 'open_trade:XBTUSD') for crash recovery.
    """

    def __init__(
        self,
        pair: str = 'XBTUSD',
        max_rows: int = MAX_OHLC_ROWS,
        persist_to_pg: bool = True,
    ):
        """
        Args:
            pair: Trading pair this context manages (e.g. 'XBTUSD')
            max_rows: Maximum OHLC rows per interval to retain
            persist_to_pg: Whether to persist state to PostgreSQL spirit_state table
        """
        self.pair = pair
        self.max_rows = max_rows
        self.persist_to_pg = persist_to_pg

        # Thread safety: multi-interval buffers call append_candle from separate threads
        self._lock = threading.Lock()

        # OHLC data + features (replaces spirit_temp_ti)
        self.ohlc_df: pd.DataFrame = pd.DataFrame()

        # Trade state (replaces spirit_temp_trade + TradeStateManager.open_trade)
        self.open_trade: Optional[Any] = None  # TradeRecord when a trade is open

        # Paper equity (replaces PaperOrderExecutor standalone tracking)
        self.equity: float = 0.0

        # Signal/decision history (for web dashboard)
        self.recent_signals: deque = deque(maxlen=100)
        self.recent_decisions: deque = deque(maxlen=100)

        # Health/telemetry
        self.health: Dict[str, Any] = {
            'candles_processed': 0,
            'trades_opened': 0,
            'trades_closed': 0,
            'errors': 0,
            'start_time': time.time(),
            'last_candle_time': None,
        }

        # Ensure PG table exists if persistence is enabled
        if self.persist_to_pg:
            self._ensure_pg_table()

    # =========================================================================
    # OHLC DATA MANAGEMENT (replaces spirit_temp_ti)
    # =========================================================================

    def warmup(self, records: list, interval: int = 60):
        """
        Bulk load OHLC records and run full feature engineering.

        Replaces: bulk_insert_spirit_temp_ti + engineer_and_update_temp_table(mode='full')

        Args:
            records: List of OHLCRecord objects or dicts
            interval: Primary interval for these records
        """
        rows = []
        for r in records:
            row = r.__dict__.copy() if hasattr(r, '__dict__') else dict(r)
            # Normalize datetime to string
            dt = row.get('datetime')
            if isinstance(dt, pd.Timestamp):
                dt = dt.tz_convert('UTC') if dt.tzinfo else dt.tz_localize('UTC')
                row['datetime'] = dt.strftime('%Y-%m-%dT%H:%M:%S')
            elif isinstance(dt, datetime):
                row['datetime'] = dt.strftime('%Y-%m-%dT%H:%M:%S')
            # Normalize interval
            try:
                row['interval'] = int(row.get('interval', interval))
            except (TypeError, ValueError):
                row['interval'] = interval
            rows.append(row)

        df = pd.DataFrame(rows)
        if df.empty:
            logger.warning("[CONTEXT] Warmup called with no records")
            return

        # Convert Decimal types to float (PG returns Decimal for NUMERIC columns)
        from decimal import Decimal
        for col in df.columns:
            if df[col].dtype == object and len(df) > 0:
                sample = df[col].dropna().iloc[0] if not df[col].dropna().empty else None
                if isinstance(sample, Decimal):
                    df[col] = df[col].apply(lambda x: float(x) if isinstance(x, Decimal) else x)

        # Sort chronologically and run feature engineering
        if 'datetime' in df.columns:
            df = df.sort_values('datetime').reset_index(drop=True)

        logger.info(f"[CONTEXT] Warming up with {len(df)} candles (interval={interval})")
        df = add_features(df)

        # Drop rows with essential NaN values (warmup period for indicators)
        essential = ['datetime', 'open', 'high', 'low', 'close', 'volume',
                     'rsi', 'atr', 'sma200', 'adx', 'macd', 'macd_signal']
        existing = [c for c in essential if c in df.columns]
        orig_len = len(df)
        df.dropna(subset=existing, inplace=True)
        dropped = orig_len - len(df)
        if dropped > 0:
            logger.debug(f"[CONTEXT] Dropped {dropped} NaN rows during warmup")

        # Trim to max_rows per interval
        if 'interval' in df.columns:
            dfs = []
            for itvl, grp in df.groupby('interval'):
                dfs.append(grp.tail(self.max_rows))
            df = pd.concat(dfs, ignore_index=True)
        else:
            df = df.tail(self.max_rows)

        self.ohlc_df = df.reset_index(drop=True)
        logger.info(f"[CONTEXT] Warmup complete: {len(self.ohlc_df)} rows")

    def append_candle(self, candle: dict, interval: int = 60):
        """
        Append a new candle and run incremental feature engineering.

        Replaces: insert_and_roll_spirit_temp_ti + engineer_and_update_temp_table(mode='incremental')

        Args:
            candle: Dict with OHLCV fields
            interval: Candle interval
        """
        # Convert Decimal types to float (PG returns Decimal for NUMERIC)
        from decimal import Decimal
        for k, v in candle.items():
            if isinstance(v, Decimal):
                candle[k] = float(v)

        # Normalize datetime
        dt = candle.get('datetime')
        if isinstance(dt, pd.Timestamp):
            dt = dt.tz_convert('UTC') if dt.tzinfo else dt.tz_localize('UTC')
            candle['datetime'] = dt.strftime('%Y-%m-%dT%H:%M:%S')
        elif isinstance(dt, datetime):
            candle['datetime'] = dt.strftime('%Y-%m-%dT%H:%M:%S')

        # Normalize interval
        try:
            candle['interval'] = int(candle.get('interval', interval))
        except (TypeError, ValueError):
            candle['interval'] = interval

        interval_val = candle['interval']

        # Lock protects self.ohlc_df from concurrent access by multi-interval threads
        with self._lock:
            # Get existing rows for this interval
            if not self.ohlc_df.empty and 'interval' in self.ohlc_df.columns:
                mask = (self.ohlc_df['interval'] == interval_val).values
                interval_df = self.ohlc_df[mask].copy()
                other_df = self.ohlc_df[~mask].copy()
            else:
                interval_df = self.ohlc_df.copy()
                other_df = pd.DataFrame()

            # Check if candle already exists (dedup)
            dt_str = candle.get('datetime')
            if not interval_df.empty and 'datetime' in interval_df.columns:
                exists = (interval_df['datetime'] == dt_str).any()
                if exists:
                    logger.debug(f"[CONTEXT] Candle {dt_str} already in DataFrame, skipping")
                    return

            # Append new candle
            new_row = pd.DataFrame([candle])
            interval_df = pd.concat([interval_df, new_row], ignore_index=True)

            if 'datetime' in interval_df.columns:
                interval_df = interval_df.sort_values('datetime').reset_index(drop=True)

            # Run feature engineering on the context window
            # Use last 300 rows (enough for all indicators) to keep incremental fast
            window_size = min(300, len(interval_df))
            if window_size >= 50:
                try:
                    # Engineer on the window, then take the latest row's features
                    window = interval_df.tail(window_size).copy()
                    window = add_features(window)
                    # Replace the window portion in interval_df
                    interval_df = pd.concat([
                        interval_df.head(len(interval_df) - window_size),
                        window,
                    ], ignore_index=True)
                except Exception as e:
                    logger.error(f"[CONTEXT] Incremental FE error: {e}")
            else:
                logger.debug(f"[CONTEXT] Not enough rows for FE ({window_size} < 50)")

            # Trim to max_rows
            interval_df = interval_df.tail(self.max_rows).reset_index(drop=True)

            # Recombine with other intervals
            if not other_df.empty:
                self.ohlc_df = pd.concat([other_df, interval_df], ignore_index=True)
            else:
                self.ohlc_df = interval_df

            # Update health
            self.health['candles_processed'] += 1
            self.health['last_candle_time'] = dt_str

    def get_feature_df(self, n: int = 300, interval: Optional[int] = None) -> pd.DataFrame:
        """
        Get the last n rows of feature-engineered OHLC data.

        Replaces: get_spirit_temp_ti() from db_utils.py

        Args:
            n: Number of rows to return
            interval: Filter by interval (None = all)

        Returns:
            DataFrame with OHLC + engineered features
        """
        with self._lock:
            if self.ohlc_df.empty:
                return pd.DataFrame()

            df = self.ohlc_df
            if interval is not None and 'interval' in df.columns:
                df = df[df['interval'] == interval]

            return df.tail(n).copy().reset_index(drop=True)

    # =========================================================================
    # TRADE STATE (replaces spirit_temp_trade)
    # =========================================================================

    def _pg_key(self, key: str, strategy_name: str = '') -> str:
        """Return prefixed PG state key.

        Multi-strategy format: 'open_trade:zone_bounce:XBTUSD'
        Single-strategy format: 'open_trade:XBTUSD' (backward compat)
        """
        if strategy_name:
            return f"{key}:{strategy_name}:{self.pair}"
        return f"{key}:{self.pair}"

    def set_open_trade(self, trade_record, strategy_name: str = ''):
        """Record a new open trade. Persists to PG."""
        self.open_trade = trade_record
        self.health['trades_opened'] += 1
        if self.persist_to_pg:
            self._save_to_pg(self._pg_key('open_trade', strategy_name), self._serialize_trade(trade_record))

    def clear_open_trade(self, strategy_name: str = ''):
        """Clear the open trade after close. Persists to PG."""
        self.open_trade = None
        if self.persist_to_pg:
            self._save_to_pg(self._pg_key('open_trade', strategy_name), None)

    def set_equity(self, equity: float):
        """Update paper trading equity. Persists to PG."""
        self.equity = equity
        if self.persist_to_pg:
            self._save_to_pg(self._pg_key('paper_equity'), equity)

    # =========================================================================
    # PG PERSISTENCE (spirit_state table)
    # =========================================================================

    def _ensure_pg_table(self):
        """Verify spirit_state table exists, creating it only if needed."""
        try:
            from spirit.utils.db_connection import execute_query
            # Check existence first (works with read-only perms)
            row = execute_query(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'spirit_state'",
                fetch='one',
            )
            if row:
                logger.debug("[CONTEXT] spirit_state table exists")
                return
            # Table doesn't exist — try to create it
            execute_query("""
                CREATE TABLE IF NOT EXISTS spirit_state (
                    key TEXT PRIMARY KEY,
                    value JSONB NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """, fetch='none')
            logger.info("[CONTEXT] spirit_state table created")
        except Exception as e:
            logger.warning(f"[CONTEXT] Failed to ensure spirit_state table: {e}")
            self.persist_to_pg = False

    def _save_to_pg(self, key: str, value):
        """Save a key-value pair to spirit_state (upsert)."""
        try:
            from spirit.utils.db_connection import execute_query
            json_val = json.dumps(value, default=str)
            execute_query("""
                INSERT INTO spirit_state (key, value, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """, (key, json_val), fetch='none')
        except Exception as e:
            logger.warning(f"[CONTEXT] Failed to save state '{key}' to PG: {e}")
            self.health['errors'] += 1

    def _load_from_pg(self, key: str) -> Optional[Any]:
        """Load a value from spirit_state by key."""
        try:
            from spirit.utils.db_connection import execute_query
            row = execute_query(
                "SELECT value FROM spirit_state WHERE key = %s",
                (key,),
                fetch='one',
            )
            if row:
                return row['value']
            return None
        except Exception as e:
            logger.warning(f"[CONTEXT] Failed to load state '{key}' from PG: {e}")
            return None

    def save_state(self):
        """Persist all critical state to PG. Called periodically or on state changes."""
        if not self.persist_to_pg:
            return
        self._save_to_pg(self._pg_key('paper_equity'), self.equity)
        if self.open_trade is not None:
            self._save_to_pg(self._pg_key('open_trade'), self._serialize_trade(self.open_trade))
        else:
            self._save_to_pg(self._pg_key('open_trade'), None)
        self._save_to_pg(self._pg_key('startup_config'), {
            'pair': self.pair,
            'last_save': datetime.now(timezone.utc).isoformat(),
            'candles_processed': self.health['candles_processed'],
        })

    def restore_state(self) -> bool:
        """
        Restore state from PG on startup (crash recovery).

        Tries pair-prefixed keys first (multi-pair format), then falls back
        to legacy un-prefixed keys for backward compatibility.

        Returns True if state was restored, False if fresh start.
        """
        if not self.persist_to_pg:
            return False

        restored = False

        # Restore equity (try pair-prefixed first, then legacy)
        equity_val = self._load_from_pg(self._pg_key('paper_equity'))
        if equity_val is None:
            equity_val = self._load_from_pg('paper_equity')
        if equity_val is not None:
            try:
                self.equity = float(equity_val)
                logger.info(f"[CONTEXT:{self.pair}] Restored equity: ${self.equity:.2f}")
                restored = True
            except (TypeError, ValueError):
                pass

        # Restore open trade (try pair-prefixed first, then legacy)
        trade_val = self._load_from_pg(self._pg_key('open_trade'))
        if trade_val is None:
            trade_val = self._load_from_pg('open_trade')
        if trade_val is not None and trade_val != 'null':
            try:
                self.open_trade = self._deserialize_trade(trade_val)
                if self.open_trade is not None:
                    entry_price = getattr(self.open_trade, 'entry_price', '?')
                    logger.info(f"[CONTEXT:{self.pair}] Restored open trade: entry_price={entry_price}")
                    restored = True
            except Exception as e:
                logger.warning(f"[CONTEXT:{self.pair}] Failed to restore open trade: {e}")

        if restored:
            config_val = self._load_from_pg(self._pg_key('startup_config'))
            if config_val is None:
                config_val = self._load_from_pg('startup_config')
            if config_val and isinstance(config_val, dict):
                logger.info(f"[CONTEXT:{self.pair}] Last save: {config_val.get('last_save', 'unknown')}")
        else:
            logger.info(f"[CONTEXT:{self.pair}] No state to restore (fresh start)")

        return restored

    # =========================================================================
    # HELPERS
    # =========================================================================

    @staticmethod
    def _serialize_trade(trade_record) -> Optional[dict]:
        """Serialize a TradeRecord to a JSON-safe dict."""
        if trade_record is None:
            return None
        d = {}
        for k, v in trade_record.__dict__.items():
            if isinstance(v, (np.integer, np.floating, np.bool_)):
                d[k] = v.item()
            elif isinstance(v, pd.Timestamp):
                d[k] = v.isoformat()
            elif isinstance(v, datetime):
                d[k] = v.isoformat()
            else:
                d[k] = v
        return d

    @staticmethod
    def _deserialize_trade(data) -> Optional[Any]:
        """Deserialize a dict back into a TradeRecord."""
        if data is None:
            return None
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            return None
        try:
            from spirit.trade_types import TradeRecord
            fields = set(TradeRecord.__dataclass_fields__.keys())
            kwargs = {k: v for k, v in data.items() if k in fields}
            return TradeRecord(**kwargs)
        except Exception:
            return None
