"""
Paper Order Executor — Simulated order execution for paper trading mode.

Same interface as KrakenOrderExecutor (place_order / close_order), swappable
at init time in spirit_main.py. Uses Kraken's validate=true to confirm order
validity, then fetches real bid/ask from the public Ticker API for fill prices.

Equity tracks true portfolio value: cash + unrealized position value.
Individual trade results are persisted to PostgreSQL strategy_performance.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional

from spirit.logger import get_logger

logger = get_logger("paper_executor")


@dataclass
class _OpenPosition:
    """Tracks an open paper position for equity calculation."""
    pair: str
    volume: float          # Base asset quantity (e.g. BTC)
    entry_price: float     # Fill price at entry
    notional_usd: float    # USD value at entry (volume * entry_price)
    order_id: str


class PaperOrderExecutor:
    def __init__(
        self,
        starting_equity: float = 1000.0,
        max_trade_usd: float = 250.0,
        fee_pct: float = 0.40,
        pair: Optional[str] = None,
        volume_step: float = 0.0001,
    ):
        from spirit.config import KRAKEN_PAIR

        self.pair = pair or KRAKEN_PAIR
        self.volume_step = float(volume_step)
        self.cash = float(starting_equity)
        self._open_positions: Dict[str, _OpenPosition] = {}  # order_id → position
        self._last_prices: Dict[str, float] = {}  # pair → last known bid price
        self.max_trade_usd = float(max_trade_usd)
        self.fee_pct = float(fee_pct)
        self._seq = 0

        logger.info(
            f"[PAPER] Initialized: equity=${self.equity:.2f} "
            f"max_trade=${self.max_trade_usd:.0f} fee={self.fee_pct:.2f}%"
        )

    @property
    def equity(self) -> float:
        """Portfolio value = cash + unrealized position value (mark-to-market).

        Uses last known bid price per pair (updated on every ticker fetch).
        Falls back to entry price if no market price is cached yet.
        """
        position_value = 0.0
        for p in self._open_positions.values():
            market_price = self._last_prices.get(p.pair, p.entry_price)
            position_value += p.volume * market_price
        return self.cash + position_value

    @equity.setter
    def equity(self, value: float):
        """Set equity directly (used by crash recovery).

        Adjusts cash to match — assumes no open positions at restore time
        (positions are restored separately via TradeStateManager).
        """
        position_value = 0.0
        for p in self._open_positions.values():
            market_price = self._last_prices.get(p.pair, p.entry_price)
            position_value += p.volume * market_price
        self.cash = float(value) - position_value

    def _round_volume(self, volume: float) -> float:
        step = self.volume_step
        steps = int(volume / step)
        rounded = max(step, steps * step)
        return float(f"{rounded:.10f}")

    def _next_order_id(self) -> str:
        self._seq += 1
        return f"paper-{int(time.time())}-{self._seq}"

    def _get_ticker(self, pair: str = None) -> dict:
        from spirit.utils.kraken_api_client import get_ticker
        p = pair or self.pair
        ticker = get_ticker(p)
        # Cache latest bid for mark-to-market equity
        if 'bid' in ticker:
            self._last_prices[p] = ticker['bid']
        return ticker

    def _validate_order(self, side: str, volume: float, pair: str = None) -> dict:
        """Call Kraken AddOrder with validate=true to confirm order is valid."""
        from spirit.utils.kraken_api_client import place_order
        return place_order(
            pair or self.pair, side, volume, ordertype='market', validate=True,
        )

    def place_order(self, trade_record) -> dict:
        """
        Paper BUY: validate via API, fetch ask price from Ticker, record fill.

        Caps buy_amount at max_trade_usd, deducts entry fee, updates equity.
        Sets trade_record fields: entry_price, buy_amount, order_id, mode, fee.
        """
        # Extract pair from trade_record (multi-pair support)
        pair = getattr(trade_record, 'symbol', None) or self.pair

        # Get real ask price first (needed for USD/BTC detection)
        ticker = self._get_ticker(pair)
        ask_price = ticker['ask']

        # Determine USD size (capped)
        buy_usd = getattr(trade_record, 'buy_amount', None)
        if buy_usd is None:
            from spirit.config import TRADE_USD_AMOUNT
            buy_usd = TRADE_USD_AMOUNT
        buy_usd = float(buy_usd)

        # If buy_amount looks like BTC volume (< $1 when price > $100), convert to USD
        if buy_usd < 1.0 and ask_price > 100:
            buy_usd = buy_usd * ask_price

        buy_usd = min(buy_usd, self.max_trade_usd, max(0.0, self.cash))

        if buy_usd <= 0:
            logger.warning("[PAPER] Insufficient cash for trade")
            return {}

        # Compute volume in base asset
        volume = self._round_volume(buy_usd / ask_price)

        # Validate with Kraken
        try:
            self._validate_order('buy', volume, pair)
        except Exception as e:
            logger.error(f"[PAPER] Order validation failed: {e}")
            return {}

        # Compute fee (half of round-trip at entry)
        entry_fee_usd = buy_usd * (self.fee_pct / 100.0) / 2.0

        # Update cash and track position
        self.cash -= (buy_usd + entry_fee_usd)
        order_id = self._next_order_id()
        self._open_positions[order_id] = _OpenPosition(
            pair=pair,
            volume=volume,
            entry_price=ask_price,
            notional_usd=buy_usd,
            order_id=order_id,
        )

        # Update trade_record — preserve signal price before overwriting with fill
        trade_record.signal_entry_price = trade_record.entry_price  # strategy's intended price
        trade_record.entry_price = ask_price                         # actual fill price
        trade_record.slippage = ask_price - (trade_record.signal_entry_price or ask_price)
        trade_record.buy_amount = volume
        trade_record.order_id = order_id
        trade_record.mode = 'paper'
        trade_record.fee = entry_fee_usd

        sig = trade_record.signal_entry_price
        logger.info(
            f"[PAPER BUY] signal={sig:.2f} fill={ask_price:.2f} "
            f"slip={trade_record.slippage:+.2f} vol={volume:.6f} "
            f"notional=${buy_usd:.2f} fee=${entry_fee_usd:.2f} "
            f"cash=${self.cash:.2f} equity=${self.equity:.2f}"
            if sig is not None else
            f"[PAPER BUY] fill={ask_price:.2f} vol={volume:.6f} "
            f"notional=${buy_usd:.2f} fee=${entry_fee_usd:.2f} "
            f"cash=${self.cash:.2f} equity=${self.equity:.2f}"
        )
        return {'txid': [order_id], 'descr': {'order': f'paper buy {volume} {pair} @ market'}}

    def close_order(self, open_trade, trade_record) -> dict:
        """
        Paper SELL: validate via API, fetch bid price from Ticker, compute PnL.

        Deducts exit fee, updates equity, writes to PG strategy_performance.
        Sets trade_record fields: exit_price, order_id, fee (total round-trip).
        """
        # Extract pair from open_trade (multi-pair support)
        pair = getattr(open_trade, 'symbol', None) or self.pair

        buy_amount = getattr(open_trade, 'buy_amount', None) or getattr(trade_record, 'buy_amount', None)
        if buy_amount is None:
            logger.error("[PAPER] Cannot close trade: missing buy_amount")
            return {}

        volume = self._round_volume(float(buy_amount))

        # Get real bid price
        ticker = self._get_ticker(pair)
        bid_price = ticker['bid']

        # Validate with Kraken
        try:
            self._validate_order('sell', volume, pair)
        except Exception as e:
            logger.error(f"[PAPER] Sell validation failed: {e}")
            return {}

        # Compute PnL and fees
        entry_price = getattr(open_trade, 'entry_price', None) or 0.0
        notional_at_exit = volume * bid_price
        notional_at_entry = volume * entry_price
        exit_fee_usd = notional_at_exit * (self.fee_pct / 100.0) / 2.0
        entry_fee_usd = getattr(open_trade, 'fee', 0.0) or 0.0
        total_fee = entry_fee_usd + exit_fee_usd
        pnl = notional_at_exit - notional_at_entry - total_fee

        # Remove position from tracking and credit cash with proceeds
        open_order_id = getattr(open_trade, 'order_id', None)
        if open_order_id and open_order_id in self._open_positions:
            del self._open_positions[open_order_id]
        self.cash += (notional_at_exit - exit_fee_usd)

        # Update trade_record — preserve signal price before overwriting with fill
        order_id = self._next_order_id()
        trade_record.signal_exit_price = trade_record.exit_price  # strategy's intended price
        trade_record.exit_price = bid_price                        # actual fill price
        trade_record.order_id = order_id
        trade_record.fee = total_fee
        trade_record.pnl = pnl  # Store net PnL (after fees)

        sig = trade_record.signal_exit_price
        logger.info(
            f"[PAPER SELL] signal={sig:.2f} fill={bid_price:.2f} "
            f"pnl=${pnl:.2f} (net) fee=${total_fee:.4f} "
            f"cash=${self.cash:.2f} equity=${self.equity:.2f}"
            if sig is not None else
            f"[PAPER SELL] fill={bid_price:.2f} "
            f"pnl=${pnl:.2f} (net) fee=${total_fee:.4f} "
            f"cash=${self.cash:.2f} equity=${self.equity:.2f}"
        )

        # Persist to PostgreSQL strategy_performance
        self._record_to_pg(open_trade, trade_record, pnl)

        return {'txid': [order_id], 'descr': {'order': f'paper sell {volume} {pair} @ market'}}

    def _record_to_pg(self, open_trade, trade_record, pnl: float):
        """Write completed paper trade to strategy_performance table."""
        try:
            from spirit.indicators.decision_engine.engine.strategy_performance_writer import record_trade
            from datetime import datetime, timezone

            entry_price = getattr(open_trade, 'entry_price', None) or 0.0
            exit_price = getattr(trade_record, 'exit_price', None) or 0.0
            buy_amount = getattr(open_trade, 'buy_amount', 0) or 0
            notional_at_entry = buy_amount * entry_price
            pnl_pct = (pnl / notional_at_entry * 100.0) if notional_at_entry else 0.0

            now = datetime.now(timezone.utc)
            entry_ts = getattr(open_trade, 'entry_datetime', None)
            pair = getattr(open_trade, 'symbol', None) or self.pair
            strategy = getattr(open_trade, 'strategy_name', None) or 'zone_bounce'
            regime = getattr(open_trade, 'trend_direction_entry', None)
            exit_reason = getattr(trade_record, 'exit_reason', None)

            record_trade(
                timestamp=now,
                entry_timestamp=entry_ts,
                pair=pair,
                strategy_name=strategy,
                is_win=(pnl > 0),
                pnl_pct=round(pnl_pct, 4),
                entry_price=entry_price,
                exit_price=exit_price,
                exit_reason=exit_reason,
                regime_at_entry=regime,
                source='paper',
            )
            logger.info(f"[PAPER] Recorded to strategy_performance: pnl_pct={pnl_pct:.2f}%")
        except Exception as e:
            logger.error(f"[PAPER] Failed to write to strategy_performance: {e}")
