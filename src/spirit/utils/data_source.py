

import pandas as pd
import time
import datetime
from spirit.logger import logger
from spirit.data_types import OHLCRecord, OHLCData
from spirit.config import KRAKEN_OHLC_COUNT, KRAKEN_OHLC_INTERVAL, KRAKEN_PAIR, KRAKEN_OHLC_BUFFER_DELAY_SECONDS
import abc



class DataSource(abc.ABC):
    def __init__(self):
        self._callbacks = []

    def register_callback(self, func):
        """Register a callback to be called when a new candle is processed."""
        self._callbacks.append(func)

    def _trigger_callbacks(self, *args, **kwargs):
        import os
        verbose = os.environ.get('DEBUG_VERBOSE', '').lower() in ['1', 'true', 'yes']
        logger.debug(f"[BaseDataSource] _trigger_callbacks called with {len(self._callbacks)} callbacks.")
        if verbose:
            logger.debug(f"[BaseDataSource] Callback args: {args}, kwargs: {kwargs}")
        import time
        for i, cb in enumerate(self._callbacks):
            cb_name = getattr(cb, '__name__', repr(cb))
            logger.debug(f"[BaseDataSource] Triggering callback {i}: {cb_name}")
            start_time = time.time()
            try:
                cb(*args, **kwargs)
                elapsed = time.time() - start_time
                logger.debug(f"[BaseDataSource] Callback {i} ({cb_name}) executed successfully in {elapsed:.4f} seconds.")
            except Exception as e:
                import traceback
                elapsed = time.time() - start_time
                logger.error(f"[BaseDataSource] Exception in callback {i} ({cb_name}) after {elapsed:.4f} seconds: {e}")
                tb_str = traceback.format_exc()
                logger.debug(f"[BaseDataSource] Exception traceback for callback {i} ({cb_name}):\n{tb_str}")

    @abc.abstractmethod
    def __next__(self):
        """Return the next window of OHLCData (advances pointer/buffer)."""
        pass

    @abc.abstractmethod
    def get_window(self, window_size=None):
        """Return the current/latest window of OHLCData (does not advance)."""
        pass



class CsvDataSource(DataSource):
    def __init__(self, csv_path, window_size=KRAKEN_OHLC_COUNT, interval=None, pair=KRAKEN_PAIR, replay_delay=None):
        super().__init__()
        # Match live types: interval as int minutes, pair as symbol string
        if interval is None:
            interval_val = KRAKEN_OHLC_INTERVAL
        elif isinstance(interval, str) and interval.endswith("min"):
            interval_val = int(interval.replace("min", ""))
        else:
            interval_val = int(interval)
        self.interval = int(interval_val)
        self.pair = pair
        self.window_size = int(window_size)
        self.replay_delay = replay_delay  # seconds; None = run as fast as possible

        df = pd.read_csv(csv_path)

        # Validate required columns
        required = ['datetime', 'open', 'high', 'low', 'close', 'volume']
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")

        # Normalize datetime to ISO8601 with 'T' in UTC
        df['datetime'] = pd.to_datetime(df['datetime'], utc=True).dt.strftime('%Y-%m-%dT%H:%M:%S')

        # Optional fields default to None to avoid fake zeros
        if 'vwap' not in df.columns:
            df['vwap'] = None
        if 'count' not in df.columns:
            df['count'] = None
        if 'timestamp' not in df.columns:
            df['timestamp'] = None

        # Sort ascending like live
        df = df.sort_values('datetime').reset_index(drop=True)

        # Add pair column to match live API shape
        df['pair'] = self.pair

        # Keep canonical columns order + extras
        api_cols = ['pair', 'datetime', 'open', 'high', 'low', 'close', 'vwap', 'volume', 'count', 'timestamp']
        self.df = df[api_cols + [c for c in df.columns if c not in api_cols]]

        # Require a full window before starting
        total_rows = len(self.df)
        if total_rows < self.window_size:
            logger.error(f"[CsvDataSource] CSV rows ({total_rows}) < window_size ({self.window_size}).")
            raise ValueError(f"CSV has only {total_rows} rows; need at least {self.window_size} for warmup.")

        # First window will have exactly window_size rows
        self.pointer = self.window_size - 1

    def __iter__(self):
        return self

    def get_window(self, window_size=None):
        """Return the current/latest window of OHLCData (does not advance)."""
        size = int(window_size) if window_size is not None else int(self.window_size)
        end = min(self.pointer + 1, len(self.df))
        start = max(0, end - size)
        slice_df = self.df.iloc[start:end]
        records = [
            OHLCRecord.from_raw(
                pair=row.get('pair', self.pair),
                interval=int(self.interval),
                dt_raw=row['datetime'],
                open_=row['open'],
                high=row['high'],
                low=row['low'],
                close=row['close'],
                vwap=row.get('vwap', None),
                volume=row['volume'],
                count=row.get('count', None),
                timestamp=row.get('timestamp', None),
            )
            for _, row in slice_df.iterrows()
        ]
        return OHLCData(records=records)

    def __next__(self):
        """Advance one candle and emit callbacks with the latest window."""
        next_ptr = self.pointer + 1
        if next_ptr >= len(self.df):
            raise StopIteration
        self.pointer = next_ptr
        window = self.get_window()
        # Trigger callbacks with the latest window
        self._trigger_callbacks(window)
        if self.replay_delay:
            time.sleep(self.replay_delay)
        return window

class CsvMultiIntervalDataSource:
    """
    Multi-interval CSV data source.

    Exposes an API compatible with MultiIntervalLiveDataSource:
      - register_callback(cb) where cb(interval:int, window:OHLCData)
      - wait_for_data(interval, min_size, timeout)
      - get_window(interval, window_size)
      - stop()
      - iteration replays candles and triggers callbacks per interval

    CSV format requirements:
      pair, interval, datetime, open, high, low, close, volume
      optional: vwap, count, timestamp
    """

    def __init__(
        self,
        csv_path: str,
        primary_interval: int,
        intervals=None,
        pair: str = KRAKEN_PAIR,
        window_size: int = KRAKEN_OHLC_COUNT,
        replay_delay: float | None = None,
    ):
        self.csv_path = csv_path
        self.primary_interval = int(primary_interval)
        self.pair = pair
        self.window_size = int(window_size)
        self.replay_delay = replay_delay

        self._callbacks = []  # cb(interval, window)
        self._stopped = False

        # Load and partition by interval
        df = pd.read_csv(self.csv_path)
        required = {"pair", "interval", "datetime", "open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        df["interval"] = df["interval"].astype(int)
        if intervals is None:
            self.intervals = sorted(df["interval"].unique().tolist())
        else:
            self.intervals = sorted(int(i) for i in intervals)
            df = df[df["interval"].isin(self.intervals)]
        # Ensure pair
        if "pair" not in df.columns:
            df["pair"] = self.pair
        # Split by interval
        self.df_by_interval = {itvl: df[df["interval"] == int(itvl)].sort_values("datetime").reset_index(drop=True) for itvl in self.intervals}
        # Minimal buffer-like attribute to signal multi-interval behavior to callers
        self.buffers = {itvl: None for itvl in self.intervals}
        # Initialize pointers after warmup. We'll start just before the first post-warmup index.
        self.pointers = {itvl: max(self.window_size - 1, 0) for itvl in self.intervals}
        self.warmup_complete = False

    # --- Public API ---
    def register_callback(self, func):
        self._callbacks.append(func)

    def _trigger_callbacks(self, interval: int, window: OHLCData):
        for cb in list(self._callbacks):
            try:
                cb(interval, window)
            except Exception as e:
                logger.error(f"[CsvMultiInterval] Callback error interval={interval}: {e}")

    def list_intervals(self):
        return list(self.intervals)

    def wait_for_data(self, interval: int, min_size: int = 1, timeout: float | None = None) -> bool:
        itvl = int(interval)
        df = self.df_by_interval.get(itvl)
        return (len(df) if df is not None else 0) >= int(min_size)

    def get_window(self, interval: int, window_size: int | None = None) -> OHLCData:
        itvl = int(interval)
        df = self.df_by_interval.get(itvl)
        if df is None or df.empty:
            return OHLCData(records=[])
        size = int(window_size if window_size is not None else self.window_size)
        end = len(df)
        start = max(0, end - size)
        slice_df = df.iloc[start:end]
        records = [
            OHLCRecord.from_raw(
                pair=row.get("pair", self.pair),
                interval=itvl,
                dt_raw=row["datetime"],
                open_=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                vwap=row.get("vwap", None),
                volume=row["volume"],
                count=row.get("count", None),
                timestamp=row.get("timestamp", None),
            )
            for _, row in slice_df.iterrows()
        ]
        return OHLCData(records=records)

    def stop(self):
        self._stopped = True

    # --- Replay iteration: step each interval and emit latest windows ---
    def __iter__(self):
        return self

    def __next__(self):
        if self._stopped:
            raise StopIteration
        progressed = False
        for itvl in self.intervals:
            df = self.df_by_interval[itvl]
            ptr = self.pointers[itvl]
            next_ptr = ptr + 1
            if next_ptr < len(df):
                self.pointers[itvl] = next_ptr
                # Build window ending at next_ptr
                start = max(0, next_ptr - self.window_size + 1)
                slice_df = df.iloc[start: next_ptr + 1]
                records = [
                    OHLCRecord.from_raw(
                        pair=row.get("pair", self.pair),
                        interval=int(itvl),
                        dt_raw=row["datetime"],
                        open_=row["open"],
                        high=row["high"],
                        low=row["low"],
                        close=row["close"],
                        vwap=row.get("vwap", None),
                        volume=row["volume"],
                        count=row.get("count", None),
                        timestamp=row.get("timestamp", None),
                    )
                    for _, row in slice_df.iterrows()
                ]
                window = OHLCData(records=records)
                self._trigger_callbacks(int(itvl), window)
                progressed = True
        if not progressed:
            raise StopIteration
        if self.replay_delay:
            time.sleep(self.replay_delay)
        return True

# Example stub for live Kraken API (to be implemented as needed)


from spirit.utils.kraken_ohlc_buffer import KrakenOHLCBuffer

class LiveDataSource(DataSource):
    """Single-interval live data source (compat wrapper).

    Note: For multi-timeframe, prefer utils.multi_interval_data_source.MultiIntervalLiveDataSource.
    """
    def wait_for_data(self, min_size, timeout=30):
        """
        Waits for the buffer to fill with at least min_size candles.
        """
        logger.debug(f"[LiveDataSource] wait_for_data called: min_size={min_size}, timeout={timeout}")
        self.buffer.wait_for_buffer(min_size, timeout=timeout)

    def __init__(self, buffer_size=KRAKEN_OHLC_COUNT, interval=KRAKEN_OHLC_INTERVAL, pair=KRAKEN_PAIR, buffer_delay_seconds=None):
        super().__init__()
        self.interval = interval
        self.pair = pair
        logger.debug(f"[LiveDataSource.__init__] interval={interval} (type={type(interval)})")
        self.buffer = KrakenOHLCBuffer(buffer_size=buffer_size, interval=interval, pair=pair)
        self.window_size = buffer_size
        self.buffer_delay_seconds = buffer_delay_seconds if buffer_delay_seconds is not None else KRAKEN_OHLC_BUFFER_DELAY_SECONDS
        self.warmup_complete = False
        # Start background updater for buffer
        self.buffer.start_background_updater()
        # Register internal callback for new candle
        self.buffer.register_callback(self._on_new_candle)

    def _on_new_candle(self, *args, **kwargs):
        # When buffer receives a new candle, trigger callbacks with the latest window
        window = self.get_window()
        self._trigger_callbacks(window)

    def __iter__(self):
        return self

    def __next__(self):
        import os
        logger.debug(f"[LiveDataSource] __next__ called. Waiting for buffer to fill if needed...")
        self.wait_for_data(self.window_size)
        logger.debug(f"[LiveDataSource] Buffer filled. Getting window...")
        window = self.get_window()
        if window is not None and len(window.records) > 0:
            logger.debug(f"[LiveDataSource] Returning window: window_size={len(window.records)}")
            # Trigger callbacks with the new window
            self._trigger_callbacks(window)
            return window
        else:
            logger.debug(f"[LiveDataSource] No data in window after buffer update. Raising StopIteration.")
            raise StopIteration

    def get_window(self, window_size=None):
        """
        Return the current/latest window of OHLCData (does not advance buffer).
        If window_size is None, use self.window_size.
        """
        import threading, time
        thread_id = threading.get_ident()
        logger.debug(f"[LiveDataSource.get_window] ENTER thread={thread_id} window_size={window_size}")
        if window_size is None:
            window_size = self.window_size
        logger.debug(f"[LiveDataSource.get_window] Attempting to acquire buffer.lock thread={thread_id} at {time.time()}")
        try:
            with self.buffer.lock:
                logger.debug(f"[LiveDataSource.get_window] buffer.lock ACQUIRED thread={thread_id} at {time.time()} Buffer size: {len(self.buffer.buffer)} Type: {type(self.buffer.buffer)}")
                candles = list(self.buffer.buffer)[-window_size:]
        except Exception as e:
            logger.error(f"[LiveDataSource.get_window] Exception inside buffer.lock: {e}")
            raise
        logger.debug(f"[LiveDataSource.get_window] buffer.lock RELEASED thread={thread_id} at {time.time()} Returning {len(candles)} candles.")
        # All candles are OHLCRecord objects (from buffer)
        from spirit.data_types import OHLCData
        return OHLCData(records=candles)
