

import threading
import time
import pandas as pd
from collections import deque
from spirit.logger import logger
from spirit.config import KRAKEN_OHLC_COUNT, KRAKEN_OHLC_INTERVAL, KRAKEN_OHLC_BUFFER_DELAY_SECONDS

class KrakenOHLCBuffer:
    def stop(self):
        """
        Stops the background updater thread if running.
        """
        if hasattr(self, '_stop_event'):
            self._stop_event.set()
            logger.info("[KrakenOHLCBuffer] Background updater stopped.")
    def _get_interval_seconds(self):
        """Return interval in seconds (self.interval is normalized to int minutes)."""
        return int(self.interval) * 60
    def start_background_updater(self):
        """
        Starts a background thread that updates the buffer at the correct interval.
        """
        import threading, time, datetime
        def updater():
            # 1) Immediate warmup fetch (fills buffer with historical candles right away)
            self.update_buffer_from_api()

            import os
            interval_min = int(self.interval)
            # 2) Polling loop with per-iteration alignment to avoid drift
            while not self._stop_event.is_set():
                try:
                    if os.environ.get("FORCE_NO_SYNC", "").lower() not in ["1", "true", "yes"]:
                        now = datetime.datetime.utcnow()
                        # compute next aligned grid for this interval
                        next_grid = now.replace(second=0, microsecond=0)
                        minute = ((now.minute // interval_min) + 1) * interval_min
                        if minute >= 60:
                            next_grid = next_grid.replace(minute=0) + datetime.timedelta(hours=1)
                        else:
                            next_grid = next_grid.replace(minute=minute)
                        delay = max(0.0, (next_grid - now).total_seconds())
                        logger.debug(f"[TimeSync][int={interval_min}] Sleeping {delay:.2f}s until {next_grid} UTC, then {self.buffer_delay_seconds}s buffer delay.")
                        time.sleep(delay)
                        time.sleep(self.buffer_delay_seconds)
                    # Fetch and update after alignment
                    self.update_buffer_from_api()
                except Exception as e:
                    logger.error(f"[KrakenOHLCBuffer] Exception in polling loop: {e}")
                    # brief backoff on error
                    time.sleep(min(10, self._get_interval_seconds()))


        self._stop_event = threading.Event()
        thread = threading.Thread(target=updater, daemon=True)
        thread.start()
        self._polling_thread = thread
    def wait_for_buffer(self, min_size, timeout=30):
        """
        Blocks until the buffer contains at least min_size candles or timeout is reached.
        # IMPORTANT: Do not remove this polling loop; it is required for data readiness checks before pipeline starts
        """
        import time, inspect, threading
        start = time.time()
        while True:
            logger.debug(f"[KrakenOHLCBuffer.wait_for_buffer] [{inspect.currentframe().f_code.co_name}] Thread={threading.get_ident()} Attempting to acquire buffer lock at {time.time()}")
            try:
                with self.lock:
                    logger.debug(f"[KrakenOHLCBuffer.wait_for_buffer] [{inspect.currentframe().f_code.co_name}] Thread={threading.get_ident()} buffer lock acquired at {time.time()}. Buffer size: {len(self.buffer)}")
                    if len(self.buffer) >= min_size:
                        logger.debug(f"[KrakenOHLCBuffer.wait_for_buffer] Buffer filled to {len(self.buffer)} >= {min_size}. Releasing lock and returning.")
                        return
            except Exception as e:
                logger.error(f"[KrakenOHLCBuffer.wait_for_buffer] Exception inside lock: {e}")
            logger.debug(f"[KrakenOHLCBuffer.wait_for_buffer] [{inspect.currentframe().f_code.co_name}] Thread={threading.get_ident()} buffer lock released at {time.time()}. Buffer size: {len(self.buffer)}")
            if time.time() - start > timeout:
                logger.debug(f"[KrakenOHLCBuffer.wait_for_buffer] Timeout reached. Buffer size: {len(self.buffer)}")
                raise TimeoutError(f"Buffer did not fill to {min_size} in {timeout} seconds.")
            time.sleep(0.5)

    def __init__(self, buffer_size=None, poll_interval=None, interval=None, buffer_delay_seconds=None, pair=None):
        """
        KrakenOHLCBuffer constructor. All timing and sizing is controlled by system_config.py.
        :param buffer_size: Number of candles to keep in buffer (default: KRAKEN_OHLC_COUNT)
        :param poll_interval: (legacy, not used in new pipeline)
        :param interval: Candle interval as string (default: generated from KRAKEN_OHLC_INTERVAL)
        :param buffer_delay_seconds: Delay after expected new candle (default: KRAKEN_OHLC_BUFFER_DELAY_SECONDS)
        :param pair: Trading pair symbol (e.g., 'XBTUSD')
        """
        self.buffer_size = buffer_size if buffer_size is not None else KRAKEN_OHLC_COUNT
        # Normalize interval to int minutes
        if interval is None:
            interval_val = int(KRAKEN_OHLC_INTERVAL)
        elif isinstance(interval, str) and interval.endswith("min"):
            interval_val = int(interval.replace("min", ""))
        else:
            interval_val = int(interval)
        self.interval = interval_val
        self.poll_interval = poll_interval if poll_interval is not None else 60  # Not used in new pipeline
        self.buffer_delay_seconds = buffer_delay_seconds if buffer_delay_seconds is not None else KRAKEN_OHLC_BUFFER_DELAY_SECONDS
        self.pair = pair if pair is not None else 'XBTUSD'
        self.buffer = deque(maxlen=self.buffer_size)
        self.last_datetime = None
        self.lock = threading.RLock()
        self._callbacks = []
        self._is_warm = False
        # Remove background polling thread for new pipeline; update is on demand
        # self._stop_event = threading.Event()
        # self.thread = threading.Thread(target=self._poll_loop, daemon=True)
        # self.thread.start()
    def register_callback(self, func):
        self._callbacks.append(func)

    def _trigger_callbacks(self, candle):
        for cb in self._callbacks:
            try:
                cb(candle)
            except Exception as e:
                logger.error(f"[KrakenOHLCBuffer] Error in callback: {e}")


    def update_buffer_from_api(self):
        """
        Fetches the latest OHLC data from Kraken and updates the buffer.
        Only called when a new candle is expected (on demand).
        """
        logger.debug(f"[KrakenOHLCBuffer.update_buffer_from_api] Attempting to acquire buffer lock...")
        try:
            with self.lock:
                logger.debug(f"[KrakenOHLCBuffer.update_buffer_from_api] buffer lock acquired for reading latest_dt. Buffer size: {len(self.buffer)}")
                latest_dt = None
                if self.buffer:
                    # Support both dict-based candles and OHLCRecord dataclass instances
                    last_candle = self.buffer[-1]
                    try:
                        if isinstance(last_candle, dict):
                            latest_dt = pd.to_datetime(last_candle.get('datetime'), utc=True)
                        else:
                            latest_dt = pd.to_datetime(getattr(last_candle, 'datetime', None), utc=True)
                    except Exception as _e:
                        logger.debug(f"[KrakenOHLCBuffer.update_buffer_from_api] Failed to parse latest_dt from last_candle: {_e}")
            logger.debug(f"[KrakenOHLCBuffer.update_buffer_from_api] buffer lock released after reading latest_dt.")
            if latest_dt is not None:
                logger.debug(f"[KrakenOHLCBuffer.update_buffer_from_api] latest_dt detected: {latest_dt}")
            # Fetch new data from Kraken (outside lock)
            logger.debug(f"[KrakenOHLCBuffer] Fetching OHLC data via ExchangeProvider count={self.buffer_size + 1}, interval={self.interval}, pair={self.pair}...")
            from spirit.exchange import get_exchange_provider
            ep = get_exchange_provider()
            raw_candles = ep.get_ohlc(self.pair, interval=int(self.interval), count=self.buffer_size + 1)
            # Convert OHLCCandle dataclasses to dicts for downstream compat
            candles = [
                {
                    'datetime': time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(c.timestamp)),
                    'timestamp': c.timestamp,
                    'open': c.open,
                    'high': c.high,
                    'low': c.low,
                    'close': c.close,
                    'vwap': c.vwap,
                    'volume': c.volume,
                    'count': c.count,
                }
                for c in raw_candles
            ]
            if candles is None or len(candles) < 2:
                logger.warning("[KrakenOHLCBuffer] Not enough candles returned to skip open candle.")
                return
            # Convert list of dicts to OHLCRecord objects
            from spirit.data_types import OHLCRecord
            new_candles = []
            for candle in candles:
                candle_dt = pd.to_datetime(candle['datetime'], utc=True)
                if latest_dt is None or candle_dt > latest_dt:
                    new_candles.append(OHLCRecord.from_raw(
                        pair=self.pair,
                        interval=int(self.interval),
                        dt_raw=candle['datetime'],
                        open_=candle['open'],
                        high=candle['high'],
                        low=candle['low'],
                        close=candle['close'],
                        vwap=candle.get('vwap', 0.0),
                        volume=candle['volume'],
                        count=candle.get('count', 0),
                        timestamp=candle.get('timestamp', 0)
                    ))
            logger.debug(f"[KrakenOHLCBuffer] {len(new_candles)} new candles to append to buffer.")
            # Acquire lock only for buffer update
            with self.lock:
                logger.debug(f"[KrakenOHLCBuffer.update_buffer_from_api] buffer lock acquired for buffer update. Buffer size: {len(self.buffer)}")
                if new_candles:
                    logger.debug(f"[KrakenOHLCBuffer] Adding {len(new_candles)} new candles to buffer.")
                    for candle in new_candles:
                        self.buffer.append(candle)

            # After buffer is warmed, only then trigger callback for newest candle
            if not self._is_warm and len(self.buffer) >= 200:
                self._is_warm = True
                logger.info(f"[KrakenOHLCBuffer] Warmup complete. Buffer size: {len(self.buffer)}")

            # If warm, then trigger only for the last candle
            if self._is_warm and new_candles:
                self._trigger_callbacks(new_candles[-1])
            # Trim buffer to max size
            while len(self.buffer) > self.buffer_size:
                self.buffer.popleft()
            if not new_candles:
                logger.debug("[KrakenOHLCBuffer] Buffer already up-to-date. No new candles added.")
            logger.debug(f"[KrakenOHLCBuffer.update_buffer_from_api] buffer lock released after buffer update.")
        except Exception as e:
            logger.error(f"[KrakenOHLCBuffer.update_buffer_from_api] Exception inside lock: {e}")

    def get_next_candle(self, block=True, timeout=None):
        """
        Returns the next candle from the buffer, blocking if needed.
        """
        start = time.time()
        while True:
            logger.debug(f"[KrakenOHLCBuffer.get_next_candle] Attempting to acquire buffer lock...")
            try:
                with self.lock:
                    logger.debug(f"[KrakenOHLCBuffer.get_next_candle] buffer lock acquired. Buffer size: {len(self.buffer)}")
                    if self.buffer:
                        logger.debug(f"[KrakenOHLCBuffer.get_next_candle] Returning next candle and releasing lock.")
                        return self.buffer.popleft()
            except Exception as e:
                logger.error(f"[KrakenOHLCBuffer.get_next_candle] Exception inside lock: {e}")
            logger.debug(f"[KrakenOHLCBuffer.get_next_candle] buffer lock released. Buffer size: {len(self.buffer)}")
            if not block:
                return None
            if timeout and (time.time() - start) > timeout:
                return None
            time.sleep(1)

    # No stop() needed; background thread is removed in new pipeline
