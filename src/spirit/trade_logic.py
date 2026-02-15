from logger import get_logger
logger = get_logger("trade_logic")
def log_trade_entry(trade_record):
    logger.debug(f"[TRADE ENTRY] Entered trade: {trade_record}")

def log_trade_exit(trade_record):
    logger.debug(f"[TRADE EXIT] Exited trade: {trade_record}")

import pandas as pd
import sqlite3
from system_config import DB_PATH, TRADE_USD_AMOUNT

# Canonical expectation: temp_data.create_spirit_temp_tables creates spirit_temp_trade
# with interval stored as TEXT. This module should NOT create the table; it only writes to it.
# If the table is missing or has the wrong schema, we log an error and abort the write.
def _check_temp_trade_schema(conn):
    """Return True if spirit_temp_trade exists and interval is TEXT; else log ERROR and return False."""
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(spirit_temp_trade);")
        rows = cur.fetchall()
        if not rows:
            logger.error("spirit_temp_trade not found. Ensure temp_data.create_spirit_temp_tables() ran before trading.")
            return False
        cols = {row[1]: row[2].upper() if isinstance(row[2], str) else row[2] for row in rows}
        interval_type = cols.get('interval')
        if interval_type is None:
            logger.error("spirit_temp_trade missing 'interval' column. Recreate temp tables via temp_data.create_spirit_temp_tables().")
            return False
        if interval_type != 'TEXT':
            logger.error(f"spirit_temp_trade.interval type is {interval_type}, expected TEXT. Recreate temp tables via temp_data.create_spirit_temp_tables().")
            return False
        return True
    except Exception as e:
        logger.error(f"Failed to validate spirit_temp_trade schema: {e}")
        return False

class TradeStateManager:
    def __init__(self, use_sqlite: bool = True):
        from trade_types import TradeRecord
        self.open_trade: TradeRecord = None
        self.trade_history: list[TradeRecord] = []
        self.use_sqlite = use_sqlite
        # On startup, check DB for open trades (V1 only)
        if use_sqlite:
            self._init_open_trade_from_db()

    def _init_open_trade_from_db(self):
        """Initialize open_trade from DB if any trade is open (no exit info)."""
        try:
            conn = sqlite3.connect(DB_PATH)
            # Open trade: no exit_datetime
            row = pd.read_sql_query("SELECT * FROM spirit_temp_trade WHERE exit_datetime IS NULL ORDER BY entry_datetime DESC LIMIT 1", conn)
            if not row.empty:
                from trade_types import TradeRecord
                # Build TradeRecord from row
                fields = set(TradeRecord.__dataclass_fields__.keys())
                kwargs = {k: row[k].iloc[0] for k in row.columns if k in fields}
                self.open_trade = TradeRecord(**kwargs)
        except Exception as e:
            logger.error(f"Failed to initialize open_trade from DB: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def open_trade_if_none(self, trade_record):
        """Open a new trade only if no trade is currently open."""
        logger.debug(f"[DEBUG] Attempting to open trade: {trade_record}")
        logger.debug(f"[DEBUG] Current open_trade before open: {self.open_trade}")
        if self.use_sqlite:
            # V1 persistent check: query SQLite for open trades before opening
            try:
                conn = sqlite3.connect(DB_PATH)
                row = pd.read_sql_query("SELECT * FROM spirit_temp_trade WHERE exit_datetime IS NULL ORDER BY entry_datetime DESC LIMIT 1", conn)
                if not row.empty:
                    logger.info("[GUARD] Persistent check: open trade found in DB, blocking new buy.")
                    return False
            except Exception as e:
                logger.error(f"Failed persistent open-trade check: {e}")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        # In-memory guard (primary check for V2, secondary for V1)
        if self.open_trade is not None:
            logger.debug("[STATE] Entry ignored: a trade is already open.")
            logger.debug(f"[DEBUG] Current open_trade after ignored open: {self.open_trade}")
            return False
        self.open_trade = trade_record
        self.trade_history.append(trade_record)
        if self.use_sqlite:
            trade_id = insert_trade(trade_record)
            trade_record.trade_id = trade_id
            logger.debug(f"[STATE] Opened trade_id={trade_id} with entry_price={trade_record.entry_price} at {trade_record.entry_datetime}")
        else:
            logger.debug(f"[STATE] Opened trade (memory-only) with entry_price={trade_record.entry_price} at {trade_record.entry_datetime}")
        logger.debug(f"[DEBUG] Current open_trade after open: {self.open_trade}")
        return True

    def close_trade_if_open(self, trade_record, pnl):
        """Close the current open trade if one exists."""
        logger.debug(f"[DEBUG] Attempting to close trade with: {trade_record}")
        logger.debug(f"[DEBUG] Current open_trade before close: {self.open_trade}")
        if self.open_trade is not None:
            logger.debug(f"Closing trade: trade_id={self.open_trade.trade_id}, open_trade={self.open_trade}")
            self.open_trade.exit_datetime = trade_record.exit_datetime
            self.open_trade.exit_price = trade_record.exit_price
            self.open_trade.signal_exit_price = trade_record.signal_exit_price
            self.open_trade.exit_index = trade_record.exit_index
            self.open_trade.pnl = pnl
            self.open_trade.exit_reason = trade_record.exit_reason
            # Copy all indicator exit fields
            self.open_trade.macd_bullish_cross_exit = trade_record.macd_bullish_cross_exit
            self.open_trade.atr_exit = trade_record.atr_exit
            self.open_trade.sma200_exit = trade_record.sma200_exit
            self.open_trade.rsi_exit = trade_record.rsi_exit
            self.open_trade.impulse_macd_exit = trade_record.impulse_macd_exit
            self.open_trade.adx_exit = trade_record.adx_exit
            self.open_trade.plus_di_exit = trade_record.plus_di_exit
            self.open_trade.minus_di_exit = trade_record.minus_di_exit
            self.open_trade.trend_direction_exit = trade_record.trend_direction_exit
            self.open_trade.fee = trade_record.fee
            self.open_trade.slippage = trade_record.slippage
            self.trade_history.append(self.open_trade)
            if self.use_sqlite:
                update_trade(self.open_trade.trade_id, self.open_trade)
            logger.debug(f"Trade updated and open_trade set to None for trade_id={self.open_trade.trade_id}")
            logger.debug(f"[DEBUG] Current open_trade after close: {self.open_trade}")
            self.open_trade = None
            return True
        else:
            logger.debug("[STATE] No open trade to close in close_trade_if_open.")
            logger.debug(f"[DEBUG] Current open_trade after failed close: {self.open_trade}")
        return False

    def get_trade_history_df(self):
        return pd.DataFrame([t.__dict__ for t in self.trade_history])

    # Legacy-friendly wrapper methods expected by tests
    def open(self, trade_record):
        return self.open_trade_if_none(trade_record)

    def close(self, trade_record, pnl=None):
        # If pnl not provided, compute with available info when possible
        if pnl is None and self.open_trade and trade_record.exit_price is not None:
            buy_amount = self.open_trade.buy_amount if self.open_trade.buy_amount is not None else 1.0
            pnl = (trade_record.exit_price - self.open_trade.entry_price) * buy_amount
        return self.close_trade_if_open(trade_record, pnl)
def insert_trade(trade_record, db_path=DB_PATH):
    """
    Insert an open trade into spirit_temp_trade. Returns trade_id.
    """
    logger = get_logger("trade_logic")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        # Validate expected schema (table exists and interval is TEXT)
        if not _check_temp_trade_schema(conn):
            return None
        columns = [
            'symbol', 'interval', 'entry_index', 'exit_index', 'entry_datetime', 'exit_datetime',
            'entry_price', 'exit_price', 'signal_entry_price', 'signal_exit_price',
            'buy_amount', 'pnl',
            'macd_bullish_cross_entry', 'atr_entry', 'sma200_entry', 'rsi_entry', 'impulse_macd_entry', 'adx_entry',
            'plus_di_entry', 'minus_di_entry', 'trend_direction_entry',
            'macd_bullish_cross_exit', 'atr_exit', 'sma200_exit', 'rsi_exit', 'impulse_macd_exit', 'adx_exit',
            'plus_di_exit', 'minus_di_exit', 'trend_direction_exit',
            'exit_reason', 'order_id', 'strategy_name', 'mode', 'fee', 'slippage'
        ]
        import datetime
        def normalize(v):
            if hasattr(v, 'item'): return v.item()
            if isinstance(v, pd.Timestamp): return str(v)
            if isinstance(v, datetime.datetime): return v.isoformat()
            return v
        values = [
            trade_record.symbol, str(trade_record.interval) if trade_record.interval is not None else None, trade_record.entry_index, trade_record.exit_index,
            trade_record.entry_datetime, trade_record.exit_datetime, trade_record.entry_price, trade_record.exit_price,
            trade_record.signal_entry_price, trade_record.signal_exit_price,
            trade_record.buy_amount, trade_record.pnl, trade_record.macd_bullish_cross_entry, trade_record.atr_entry,
            trade_record.sma200_entry, trade_record.rsi_entry, trade_record.impulse_macd_entry, trade_record.adx_entry,
            trade_record.plus_di_entry, trade_record.minus_di_entry, trade_record.trend_direction_entry,
            trade_record.macd_bullish_cross_exit, trade_record.atr_exit, trade_record.sma200_exit, trade_record.rsi_exit,
            trade_record.impulse_macd_exit, trade_record.adx_exit, trade_record.plus_di_exit, trade_record.minus_di_exit,
            trade_record.trend_direction_exit, trade_record.exit_reason, trade_record.order_id, trade_record.strategy_name,
            trade_record.mode, trade_record.fee, trade_record.slippage
        ]
        values = [normalize(v) for v in values]
        placeholders = ','.join(['?'] * len(values))
        logger.debug(f"[DEBUG] Inserting trade into spirit_temp_trade: {dict(zip(columns, values))}")
        cursor.execute(f"INSERT INTO spirit_temp_trade ({','.join(columns)}) VALUES ({placeholders})", values)
        conn.commit()
        trade_id = cursor.lastrowid
        logger.info(f"Inserted open trade with trade_id {trade_id}")
        logger.debug(f"[DEBUG] spirit_temp_trade table updated with trade_id {trade_id}")
        return trade_id
    except Exception as e:
        logger.error(f"Failed to insert open trade: {e}")
        return None
    finally:
        conn.close()

def update_trade(trade_id, trade_record, db_path=DB_PATH):
    """
    Update an open trade with exit info in spirit_temp_trade.
    """
    logger = get_logger("trade_logic")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        # Validate expected schema (table exists and interval is TEXT)
        if not _check_temp_trade_schema(conn):
            return
        columns = [
            'exit_index', 'exit_datetime', 'exit_price', 'signal_exit_price', 'pnl',
            'macd_bullish_cross_exit', 'atr_exit', 'sma200_exit', 'rsi_exit', 'impulse_macd_exit', 'adx_exit',
            'plus_di_exit', 'minus_di_exit', 'trend_direction_exit',
            'exit_reason', 'order_id', 'fee', 'slippage'
        ]
        import numpy as np, datetime
        def normalize_value(value):
            if isinstance(value, np.generic):
                return value.item()
            if isinstance(value, pd.Timestamp):
                # Store as ISO string
                return value.strftime('%Y-%m-%dT%H:%M:%S')
            if isinstance(value, datetime.datetime):
                return value.isoformat()
            return value
        values = [
            normalize_value(trade_record.exit_index), normalize_value(trade_record.exit_datetime), normalize_value(trade_record.exit_price), normalize_value(trade_record.signal_exit_price), normalize_value(trade_record.pnl),
            normalize_value(trade_record.macd_bullish_cross_exit), normalize_value(trade_record.atr_exit), normalize_value(trade_record.sma200_exit), normalize_value(trade_record.rsi_exit),
            normalize_value(trade_record.impulse_macd_exit), normalize_value(trade_record.adx_exit), normalize_value(trade_record.plus_di_exit), normalize_value(trade_record.minus_di_exit),
            normalize_value(trade_record.trend_direction_exit), normalize_value(trade_record.exit_reason), normalize_value(trade_record.order_id), normalize_value(trade_record.fee), normalize_value(trade_record.slippage)
        ]
        logger.debug(f"[DEBUG] Updating trade_id {trade_id} in spirit_temp_trade with exit values: {dict(zip(columns, values))}")
        set_clause = ', '.join([f'{col}=?' for col in columns])
        cursor.execute(f"UPDATE spirit_temp_trade SET {set_clause} WHERE trade_id=?", values + [trade_id])
        conn.commit()
        logger.info(f"Updated trade_id {trade_id} with exit info")
        logger.debug(f"[DEBUG] spirit_temp_trade table updated for trade_id {trade_id}")
    except Exception as e:
        logger.error(f"Failed to update trade_id {trade_id}: {e}")
    finally:
        conn.close()

def count_temp_trades(db_path=DB_PATH):
    """Return the number of rows currently in spirit_temp_trade (0 on error)."""
    try:
        conn = sqlite3.connect(db_path)
        if not _check_temp_trade_schema(conn):
            return 0
        df = pd.read_sql_query("SELECT COUNT(*) AS cnt FROM spirit_temp_trade", conn)
        return int(df['cnt'].iloc[0]) if not df.empty else 0
    except Exception:
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass

def get_trade_amount(entry_price, usd_trade_size: float = TRADE_USD_AMOUNT):
    """
    Calculate trade amount in BTC for a given USD notional size.
    """
    return usd_trade_size / entry_price if entry_price else None

from trade_types import TradeRecord
def process_trade_signals(arg1, arg2=None, mode='test', trade_state_manager=None, order_executor=None, logger=None):
    """
    Backward-compatible trade processing API.
    Supports:
      1) New style: process_trade_signals('buy'|'sell', TradeRecord, mode, trade_state_manager, ...)
      2) Legacy style: process_trade_signals(row: pd.Series, open_trade, mode='test')
    """
    # Lazy import to avoid circulars in tests (already imported at top)
    import pandas as pd

    # Ensure we have a TradeStateManager
    if trade_state_manager is None:
        trade_state_manager = TradeStateManager()

    # Detect legacy signature: first arg is a pandas Series
    if 'pandas' in str(type(arg1)) and hasattr(arg1, 'get'):
        row = arg1
        open_trade_legacy = arg2  # unused, kept for API compatibility
        # Determine signal
        entry_signal = bool(row.get('entry_signal', False))
        exit_signal = bool(row.get('exit_signal', False))
        # Build TradeRecord from row
        def _safe(v, default=None):
            try:
                return row.get(v, default)
            except Exception:
                return default
        symbol = _safe('pair', 'XBTUSD')
        interval = _safe('interval', 15)
        if entry_signal:
            tr = TradeRecord(
                symbol=symbol,
                interval=interval,
                entry_datetime=_safe('datetime'),
                entry_price=_safe('close'),
                macd_bullish_cross_entry=bool(_safe('macd_bullish_cross')),
                atr_entry=_safe('atr'),
                sma200_entry=_safe('sma200'),
                rsi_entry=_safe('rsi'),
                impulse_macd_entry=_safe('impulse_macd'),
                adx_entry=_safe('adx'),
                plus_di_entry=_safe('plus_di'),
                minus_di_entry=_safe('minus_di'),
                trend_direction_entry=_safe('trend_direction'),
                strategy_name='TestStrategy',
                mode=mode,
            )
            return process_trade_signals('buy', tr, mode=mode, trade_state_manager=trade_state_manager, order_executor=order_executor, logger=logger)
        if exit_signal:
            tr = TradeRecord(
                symbol=symbol,
                interval=interval,
                exit_datetime=_safe('datetime'),
                exit_price=_safe('close'),
                macd_bullish_cross_exit=bool(_safe('macd_bullish_cross')),
                atr_exit=_safe('atr'),
                sma200_exit=_safe('sma200'),
                rsi_exit=_safe('rsi'),
                impulse_macd_exit=_safe('impulse_macd'),
                adx_exit=_safe('adx'),
                plus_di_exit=_safe('plus_di'),
                minus_di_exit=_safe('minus_di'),
                trend_direction_exit=_safe('trend_direction'),
                strategy_name='TestStrategy',
                mode=mode,
            )
            return process_trade_signals('sell', tr, mode=mode, trade_state_manager=trade_state_manager, order_executor=order_executor, logger=logger)
        # Neither signal set; nothing to do.
        if logger:
            logger.debug("[SIGNAL] No entry/exit signal found in legacy row.")
        return None

    # New-style signature
    signal_type = arg1
    trade_record: TradeRecord = arg2

    # High-level diagnostic for signal processing
    if logger:
        logger.debug(f"[SIGNAL] Processing signal_type={signal_type} for trade_record={trade_record}")

    if signal_type == "buy":
        # Hard guard: never place a live order if a trade is already open
        if trade_state_manager.open_trade is not None:
            if logger:
                logger.info("[GUARD] Trade already open. Skipping additional BUY order.")
            return None

        # Ensure buy_amount is set for notional sizing
        if trade_record.entry_price is not None and (trade_record.buy_amount is None):
            trade_record.buy_amount = get_trade_amount(trade_record.entry_price)

        # Place order only after confirming no open trade
        if mode in ('live', 'paper') and order_executor:
            api_response = order_executor.place_order(trade_record)
            if api_response and hasattr(api_response, 'price'):
                trade_record.entry_price = api_response.price
        else:
            if logger:
                logger.info(f"[TEST MODE] Would place order: {trade_record}")

        # Persist as open only once
        opened = trade_state_manager.open_trade_if_none(trade_record)
        if not opened:
            if logger:
                logger.info("Trade entry signal ignored after order attempt: trade already open.")
    elif signal_type == "sell":
        open_trade = trade_state_manager.open_trade
        if not open_trade:
            if logger:
                logger.warning("No open trade to close on sell signal.")
            return None
        exit_price = trade_record.exit_price
        if exit_price is None and mode == 'test':
            # Fallback: use latest close from temp table if available
            # Uses get_spirit_temp_ti() which routes V2→in-memory, V1→SQLite
            try:
                from utils.db_utils import get_spirit_temp_ti
                df = get_spirit_temp_ti()
                if not df.empty:
                    last = df.iloc[-1]
                    exit_price = float(last['close'])
                    trade_record.exit_price = exit_price
                    trade_record.exit_datetime = str(last['datetime'])
                    try:
                        if getattr(trade_record, 'exit_index', None) is None and 'id' in df.columns:
                            trade_record.exit_index = int(last['id'])
                    except Exception:
                        pass
                    if logger:
                        logger.debug(f"[SELL] Using latest close as exit_price={exit_price} at {trade_record.exit_datetime}")
                else:
                    # Fall back to entry price if no data
                    exit_price = open_trade.exit_price or open_trade.entry_price
                    trade_record.exit_price = exit_price
                    trade_record.exit_datetime = getattr(trade_record, 'exit_datetime', None) or getattr(open_trade, 'exit_datetime', None)
            except Exception as e:
                if logger:
                    logger.error(f"[SELL] Failed to fetch latest close for exit price: {e}")
        if exit_price is None:
            error_msg = "No exit price found in trade_record for sell. Aborting trade processing."
            if logger:
                logger.error(error_msg)
            return None
        buy_amount = open_trade.buy_amount if open_trade.buy_amount is not None else 1.0
        trade_record.exit_reason = trade_record.exit_reason or 'signal'
        # Executor sets trade_record.pnl to net PnL (after fees) for paper/live.
        # For test mode (no executor), compute gross PnL.
        if mode in ('live', 'paper') and order_executor:
            api_response = order_executor.close_order(open_trade, trade_record)
            if api_response and hasattr(api_response, 'price'):
                trade_record.exit_price = api_response.price
            pnl = trade_record.pnl  # Net PnL from executor
        else:
            pnl = (exit_price - open_trade.entry_price) * buy_amount
            if logger:
                logger.info(f"[TEST MODE] Would close order: {open_trade} -> {trade_record}")
        if logger:
            logger.debug(f"[SELL] Prepared exit: price={trade_record.exit_price}, entry_price={open_trade.entry_price}, buy_amount={buy_amount}, pnl={pnl}")
        closed = trade_state_manager.close_trade_if_open(trade_record, pnl)
        if not closed:
            if logger:
                logger.info("Trade exit signal ignored: no open trade to close.")
    return None
# from logger import get_logger  # duplicate import removed

# --- Account management for balance and trade amounts ---
class Account:
    """
    Simple account class to track balance, open trade, and trade history.
    All trade amounts are calculated from USD notional using get_trade_amount.
    """
    def __init__(self, initial_balance=10000.0):
        self.balance = initial_balance
        self.open_trade = None
        self.trade_history = []


    def can_open_trade(self, usd_trade_size: float = TRADE_USD_AMOUNT, entry_price=None):
        amt = get_trade_amount(entry_price, usd_trade_size) if entry_price else 0
        return self.balance >= amt

    def open_trade_entry(self, trade_info, usd_trade_size: float = TRADE_USD_AMOUNT):
        entry_price = trade_info.get('price')
        amt = get_trade_amount(entry_price, usd_trade_size)
        if self.can_open_trade(usd_trade_size, entry_price):
            self.balance -= amt
            self.open_trade = {**trade_info, 'buy_amount': amt}
            return True
        return False

    def close_trade_exit(self, exit_info):
        if self.open_trade:
            amt = self.open_trade.get('buy_amount', 0)
            pnl = (exit_info['price'] - self.open_trade['price']) * amt
            self.balance += amt + pnl
            trade_record = {**self.open_trade, **exit_info, 'pnl': pnl}
            self.trade_history.append(trade_record)
            self.open_trade = None
            return pnl
        return 0

    def get_balance(self):
        return self.balance

    def get_trade_history(self):
        return self.trade_history

# DB_PATH defined at top of file

def record_trade(entry: TradeRecord, exit: TradeRecord, pnl, extra=None, db_path=DB_PATH):
    """
    Insert a trade into the spirit_temp_trade table.
    entry, exit: dicts with keys like datetime, price, index, etc.
    pnl: float
    extra: dict of any extra fields (optional)
    """
    logger = get_logger("trade_logic")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        # Validate expected schema first
        if not _check_temp_trade_schema(conn):
            return
        if entry.buy_amount is None:
            raise ValueError("buy_amount must be set in entry before recording trade.")
        columns = [
            'symbol', 'interval', 'entry_index', 'exit_index', 'entry_datetime', 'exit_datetime',
            'entry_price', 'exit_price', 'signal_entry_price', 'signal_exit_price',
            'buy_amount', 'pnl',
            'macd_bullish_cross_entry', 'atr_entry', 'sma200_entry', 'rsi_entry', 'impulse_macd_entry', 'adx_entry',
            'plus_di_entry', 'minus_di_entry', 'trend_direction_entry',
            'macd_bullish_cross_exit', 'atr_exit', 'sma200_exit', 'rsi_exit', 'impulse_macd_exit', 'adx_exit',
            'plus_di_exit', 'minus_di_exit', 'trend_direction_exit',
            'exit_reason', 'order_id', 'strategy_name', 'mode', 'fee', 'slippage'
        ]
        import datetime
        def normalize(v):
            if hasattr(v, 'item'): return v.item()
            if isinstance(v, pd.Timestamp): return str(v)
            if isinstance(v, datetime.datetime): return v.isoformat()
            return v
        values = [
            entry.symbol, str(entry.interval) if entry.interval is not None else None, entry.entry_index, exit.exit_index,
            entry.entry_datetime, exit.exit_datetime, entry.entry_price, exit.exit_price,
            entry.signal_entry_price, exit.signal_exit_price,
            entry.buy_amount, pnl, entry.macd_bullish_cross_entry, entry.atr_entry, entry.sma200_entry, entry.rsi_entry,
            entry.impulse_macd_entry, entry.adx_entry, entry.plus_di_entry, entry.minus_di_entry, entry.trend_direction_entry,
            exit.macd_bullish_cross_exit, exit.atr_exit, exit.sma200_exit, exit.rsi_exit, exit.impulse_macd_exit, exit.adx_exit,
            exit.plus_di_exit, exit.minus_di_exit, exit.trend_direction_exit, exit.exit_reason, exit.order_id, entry.strategy_name,
            entry.mode, exit.fee, exit.slippage
        ]
        values = [normalize(v) for v in values]
        placeholders = ','.join(['?'] * len(values))
        cursor.execute(f"INSERT INTO spirit_temp_trade ({','.join(columns)}) VALUES ({placeholders})", values)
        conn.commit()
        logger.info(f"Trade recorded: entry {entry.entry_datetime} exit {exit.exit_datetime} PnL {pnl}")
    except Exception as e:
        logger.error(f"Failed to record trade: {e}")
    finally:
        conn.close()


