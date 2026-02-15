from spirit.logger import get_logger
logger = get_logger("trade_logic")
def log_trade_entry(trade_record):
    logger.debug(f"[TRADE ENTRY] Entered trade: {trade_record}")

def log_trade_exit(trade_record):
    logger.debug(f"[TRADE EXIT] Exited trade: {trade_record}")

import pandas as pd
from spirit.config import TRADE_USD_AMOUNT


class TradeStateManager:
    def __init__(self):
        from spirit.trade_types import TradeRecord
        self.open_trade: TradeRecord = None
        self.trade_history: list[TradeRecord] = []

    def open_trade_if_none(self, trade_record):
        """Open a new trade only if no trade is currently open."""
        logger.debug(f"[DEBUG] Attempting to open trade: {trade_record}")
        logger.debug(f"[DEBUG] Current open_trade before open: {self.open_trade}")
        if self.open_trade is not None:
            logger.debug("[STATE] Entry ignored: a trade is already open.")
            return False
        self.open_trade = trade_record
        self.trade_history.append(trade_record)
        logger.debug(f"[STATE] Opened trade with entry_price={trade_record.entry_price} at {trade_record.entry_datetime}")
        return True

    def close_trade_if_open(self, trade_record, pnl):
        """Close the current open trade if one exists."""
        logger.debug(f"[DEBUG] Attempting to close trade with: {trade_record}")
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
            logger.debug(f"Trade closed for trade_id={self.open_trade.trade_id}")
            self.open_trade = None
            return True
        else:
            logger.debug("[STATE] No open trade to close in close_trade_if_open.")
        return False

    def get_trade_history_df(self):
        return pd.DataFrame([t.__dict__ for t in self.trade_history])

    # Legacy-friendly wrapper methods expected by tests
    def open(self, trade_record):
        return self.open_trade_if_none(trade_record)

    def close(self, trade_record, pnl=None):
        if pnl is None and self.open_trade and trade_record.exit_price is not None:
            buy_amount = self.open_trade.buy_amount if self.open_trade.buy_amount is not None else 1.0
            pnl = (trade_record.exit_price - self.open_trade.entry_price) * buy_amount
        return self.close_trade_if_open(trade_record, pnl)


class MultiPairTradeStateManager:
    """Manages per-pair TradeStateManagers for multi-pair operation."""

    def __init__(self):
        self._managers: dict[str, TradeStateManager] = {}

    def get(self, pair: str) -> TradeStateManager:
        """Get or create the TradeStateManager for a specific pair."""
        if pair not in self._managers:
            self._managers[pair] = TradeStateManager()
        return self._managers[pair]

    def get_open_trade(self, pair: str):
        """Get the open trade for a specific pair, or None."""
        return self.get(pair).open_trade

    def all_open_trades(self) -> dict:
        """Return {pair: TradeRecord} for all pairs with open trades."""
        return {p: m.open_trade for p, m in self._managers.items() if m.open_trade is not None}

    def get_trade_history_df(self):
        """Combined trade history across all pairs."""
        dfs = []
        for pair, mgr in self._managers.items():
            df = mgr.get_trade_history_df()
            if not df.empty:
                df['pair'] = pair
                dfs.append(df)
        if dfs:
            return pd.concat(dfs, ignore_index=True)
        return pd.DataFrame()


class StrategyTradeStateManager:
    """Trade state for one (pair, strategy_name) slot.

    Same interface as TradeStateManager but scoped to a specific
    pair + strategy combination. Used by MultiStrategyTradeStateManager.
    """

    def __init__(self, pair: str, strategy_name: str):
        from spirit.trade_types import TradeRecord
        self.pair = pair
        self.strategy_name = strategy_name
        self.open_trade: TradeRecord = None
        self.trade_history: list = []

    def open_trade_if_none(self, trade_record):
        if self.open_trade is not None:
            logger.debug(f"[STATE:{self.pair}:{self.strategy_name}] Entry ignored: trade already open.")
            return False
        self.open_trade = trade_record
        self.trade_history.append(trade_record)
        logger.debug(
            f"[STATE:{self.pair}:{self.strategy_name}] Opened trade "
            f"entry_price={trade_record.entry_price} at {trade_record.entry_datetime}"
        )
        return True

    def close_trade_if_open(self, trade_record, pnl):
        if self.open_trade is not None:
            self.open_trade.exit_datetime = trade_record.exit_datetime
            self.open_trade.exit_price = trade_record.exit_price
            self.open_trade.signal_exit_price = trade_record.signal_exit_price
            self.open_trade.exit_index = trade_record.exit_index
            self.open_trade.pnl = pnl
            self.open_trade.exit_reason = trade_record.exit_reason
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
            self.open_trade = None
            return True
        return False

    def get_trade_history_df(self):
        return pd.DataFrame([t.__dict__ for t in self.trade_history])


class MultiStrategyTradeStateManager:
    """Keyed by (pair, strategy_name). Replaces MultiPairTradeStateManager for Spine.

    Supports multiple concurrent trades per pair (one per strategy), controlled
    by max_concurrent_per_pair.
    """

    def __init__(self, max_concurrent_per_pair: int = 1):
        self.max_concurrent_per_pair = max_concurrent_per_pair
        self._managers: dict[tuple[str, str], StrategyTradeStateManager] = {}

    def get(self, pair: str, strategy_name: str = '') -> StrategyTradeStateManager:
        """Get or create the StrategyTradeStateManager for a (pair, strategy) slot."""
        key = (pair, strategy_name)
        if key not in self._managers:
            self._managers[key] = StrategyTradeStateManager(pair, strategy_name)
        return self._managers[key]

    def can_open_for_pair(self, pair: str) -> bool:
        """Check if the pair has room for another concurrent trade."""
        return self.count_open_for_pair(pair) < self.max_concurrent_per_pair

    def count_open_for_pair(self, pair: str) -> int:
        """Count how many strategies currently have open trades for this pair."""
        return sum(
            1 for (p, _), m in self._managers.items()
            if p == pair and m.open_trade is not None
        )

    def open_trades_for_pair(self, pair: str) -> dict:
        """Return {strategy_name: TradeRecord} for all open trades on this pair."""
        return {
            s: m.open_trade
            for (p, s), m in self._managers.items()
            if p == pair and m.open_trade is not None
        }

    def all_open_trades(self) -> dict:
        """Return {pair: {strategy: TradeRecord}} for all open trades."""
        result: dict[str, dict] = {}
        for (pair, strat), m in self._managers.items():
            if m.open_trade is not None:
                result.setdefault(pair, {})[strat] = m.open_trade
        return result

    def total_deployed_usd(self) -> float:
        """Sum of all open position sizes (buy_amount * entry_price) across all slots."""
        total = 0.0
        for m in self._managers.values():
            if m.open_trade is not None:
                buy_amt = getattr(m.open_trade, 'buy_amount', None) or 0
                entry_px = getattr(m.open_trade, 'entry_price', None) or 0
                total += float(buy_amt) * float(entry_px)
        return total

    def get_pair_manager(self, pair: str) -> StrategyTradeStateManager:
        """Backward compat: get single-strategy manager for a pair.

        When used with a single strategy, returns the first (only) manager.
        """
        for (p, s), m in self._managers.items():
            if p == pair:
                return m
        # Create a default slot
        return self.get(pair, '')

    def get_trade_history_df(self):
        """Combined trade history across all slots."""
        dfs = []
        for (pair, strat), mgr in self._managers.items():
            df = mgr.get_trade_history_df()
            if not df.empty:
                df['pair'] = pair
                df['strategy_name'] = strat
                dfs.append(df)
        if dfs:
            return pd.concat(dfs, ignore_index=True)
        return pd.DataFrame()


def get_trade_amount(entry_price, usd_trade_size: float = TRADE_USD_AMOUNT):
    """Calculate trade amount in BTC for a given USD notional size."""
    return usd_trade_size / entry_price if entry_price else None


from spirit.trade_types import TradeRecord
def process_trade_signals(arg1, arg2=None, mode='test', trade_state_manager=None, order_executor=None, logger=None):
    """
    Backward-compatible trade processing API.
    Supports:
      1) New style: process_trade_signals('buy'|'sell', TradeRecord, mode, trade_state_manager, ...)
      2) Legacy style: process_trade_signals(row: pd.Series, open_trade, mode='test')
    """
    import pandas as pd

    if trade_state_manager is None:
        trade_state_manager = TradeStateManager()

    # Detect legacy signature: first arg is a pandas Series
    if 'pandas' in str(type(arg1)) and hasattr(arg1, 'get'):
        row = arg1
        entry_signal = bool(row.get('entry_signal', False))
        exit_signal = bool(row.get('exit_signal', False))
        def _safe(v, default=None):
            try:
                return row.get(v, default)
            except Exception:
                return default
        symbol = _safe('pair', 'XBTUSD')
        interval = _safe('interval', 15)
        if entry_signal:
            tr = TradeRecord(
                symbol=symbol, interval=interval,
                entry_datetime=_safe('datetime'), entry_price=_safe('close'),
                macd_bullish_cross_entry=bool(_safe('macd_bullish_cross')),
                atr_entry=_safe('atr'), sma200_entry=_safe('sma200'),
                rsi_entry=_safe('rsi'), impulse_macd_entry=_safe('impulse_macd'),
                adx_entry=_safe('adx'), plus_di_entry=_safe('plus_di'),
                minus_di_entry=_safe('minus_di'),
                trend_direction_entry=_safe('trend_direction'),
                strategy_name='TestStrategy', mode=mode,
            )
            return process_trade_signals('buy', tr, mode=mode, trade_state_manager=trade_state_manager, order_executor=order_executor, logger=logger)
        if exit_signal:
            tr = TradeRecord(
                symbol=symbol, interval=interval,
                exit_datetime=_safe('datetime'), exit_price=_safe('close'),
                macd_bullish_cross_exit=bool(_safe('macd_bullish_cross')),
                atr_exit=_safe('atr'), sma200_exit=_safe('sma200'),
                rsi_exit=_safe('rsi'), impulse_macd_exit=_safe('impulse_macd'),
                adx_exit=_safe('adx'), plus_di_exit=_safe('plus_di'),
                minus_di_exit=_safe('minus_di'),
                trend_direction_exit=_safe('trend_direction'),
                strategy_name='TestStrategy', mode=mode,
            )
            return process_trade_signals('sell', tr, mode=mode, trade_state_manager=trade_state_manager, order_executor=order_executor, logger=logger)
        if logger:
            logger.debug("[SIGNAL] No entry/exit signal found in legacy row.")
        return None

    # New-style signature
    signal_type = arg1
    trade_record: TradeRecord = arg2

    if logger:
        logger.debug(f"[SIGNAL] Processing signal_type={signal_type} for trade_record={trade_record}")

    if signal_type == "buy":
        if trade_state_manager.open_trade is not None:
            if logger:
                logger.info("[GUARD] Trade already open. Skipping additional BUY order.")
            return None

        if trade_record.entry_price is not None and (trade_record.buy_amount is None):
            trade_record.buy_amount = get_trade_amount(trade_record.entry_price)

        if mode in ('live', 'paper') and order_executor:
            api_response = order_executor.place_order(trade_record)
            if api_response and hasattr(api_response, 'price'):
                trade_record.entry_price = api_response.price
        else:
            if logger:
                logger.info(f"[TEST MODE] Would place order: {trade_record}")

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
            try:
                from spirit.utils.db_utils import get_spirit_temp_ti
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
                    exit_price = open_trade.exit_price or open_trade.entry_price
                    trade_record.exit_price = exit_price
                    trade_record.exit_datetime = getattr(trade_record, 'exit_datetime', None) or getattr(open_trade, 'exit_datetime', None)
            except Exception as e:
                if logger:
                    logger.error(f"[SELL] Failed to fetch latest close for exit price: {e}")
        if exit_price is None:
            if logger:
                logger.error("No exit price found in trade_record for sell. Aborting trade processing.")
            return None
        buy_amount = open_trade.buy_amount if open_trade.buy_amount is not None else 1.0
        trade_record.exit_reason = trade_record.exit_reason or 'signal'
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
