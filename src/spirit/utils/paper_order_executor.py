"""
Paper Order Executor — Simulated order execution for paper trading mode.

Same interface as LiveOrderExecutor (place_order / close_order), swappable
at init time via the OrderExecutor ABC.  Fetches real bid/ask from the
public Ticker API for fill prices (skipped in replay mode).

Equity tracks true portfolio value: cash + unrealized position value.
Individual trade results are persisted to PostgreSQL strategy_performance.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional

from spirit.exchange.executor import OrderExecutor
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


class PaperOrderExecutor(OrderExecutor):
    def __init__(
        self,
        starting_equity: float = 1000.0,
        max_trade_usd: float = 250.0,
        fee_pct: float = 0.40,
        pair: Optional[str] = None,
        pair_info: Optional[Dict] = None,
        replay_mode: bool = False,
        run_id: str = 'live',
    ):
        super().__init__(
            pair=pair or 'XBTUSD',
            pair_info=pair_info,
            starting_equity=starting_equity,
            run_id=run_id,
        )
        self.cash = float(starting_equity)
        self._open_positions: Dict[str, _OpenPosition] = {}  # order_id → position
        self._last_prices: Dict[str, float] = {}  # pair → last known bid price
        self.max_trade_usd = float(max_trade_usd)
        self.fee_pct = float(fee_pct)
        self._seq = 0
        self.replay_mode = replay_mode
        self.run_id = run_id

        mode_label = "REPLAY" if replay_mode else "PAPER"
        logger.info(
            f"[PAPER] Initialized ({mode_label}): equity=${self.equity:.2f} "
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

    def _next_order_id(self) -> str:
        """Mint a deterministic paper-mode order id.

        Format: ``paper-{seq:06d}`` where seq is a per-instance monotonic
        counter. The wall-clock prefix the previous version used was
        non-deterministic across runs and broke replay log diffs (#521).
        Uniqueness is guaranteed by the counter alone within a run; PG
        uniqueness constraints (strategy_performance, risk_gate_decisions)
        are keyed by ``(strategy_name, pair, entry_timestamp, run_id, instance)``,
        not order_id, so the change is safe.
        """
        self._seq += 1
        return f"paper-{self._seq:06d}"

    def _get_ticker(self, pair: str = None) -> dict:
        from spirit.exchange import get_exchange_provider
        p = pair or self.pair
        ticker = get_exchange_provider().get_ticker(p)
        # Cache latest bid for mark-to-market equity
        self._last_prices[p] = ticker.bid
        return {'bid': ticker.bid, 'ask': ticker.ask, 'last': ticker.last}

    def _validate_order(self, side: str, volume: float, pair: str = None) -> dict:
        """Call Kraken AddOrder with validate=true to confirm order is valid.

        Skipped in paper mode (#48) — paper trades should never depend on
        external API availability. Only used in live mode.
        """
        logger.debug(
            f"[PAPER] Order validation skipped (paper mode): "
            f"side={side} vol={volume} pair={pair or self.pair}"
        )
        return {}

    def place_limit_order(self, trade_record, limit_price: float) -> dict:
        """
        Paper LIMIT BUY: validate with Kraken, return paper txid.
        Does NOT deduct cash yet — cash is deducted on fill via finalize_limit_fill().
        """
        pair = getattr(trade_record, 'symbol', None) or self.pair

        # Get real ask price for validation (skip in replay mode)
        if not self.replay_mode:
            ticker = self._get_ticker(pair)

        # Determine USD size (capped)
        buy_usd = getattr(trade_record, 'buy_amount', None)
        if buy_usd is None:
            from spirit.config import TRADE_USD_AMOUNT
            buy_usd = TRADE_USD_AMOUNT
        buy_usd = float(buy_usd)

        # Convert to volume at limit price
        if buy_usd < 1.0 and limit_price > 100:
            buy_usd = buy_usd * limit_price
        buy_usd = min(buy_usd, self.max_trade_usd, max(0.0, self.cash))

        if buy_usd <= 0:
            logger.warning("[PAPER] Insufficient cash for limit order")
            return {}

        volume = self._round_volume(buy_usd / limit_price, pair)
        actual_notional = volume * limit_price

        # Validate with Kraken (skip in replay mode)
        if not self.replay_mode:
            try:
                self._validate_order('buy', volume, pair)
            except Exception as e:
                logger.error(f"[PAPER] Limit order validation failed: {e}")
                return {}

        order_id = self._next_order_id()
        trade_record.order_id = order_id
        trade_record.order_type = 'limit'
        trade_record.limit_price = limit_price

        # Store pending limit for fill simulation
        if not hasattr(self, '_pending_limits'):
            self._pending_limits: Dict[str, dict] = {}

        self._pending_limits[order_id] = {
            'pair': pair,
            'limit_price': limit_price,
            'volume': volume,
            'notional': actual_notional,
            'placed_at': time.time(),
        }

        logger.info(
            f"[PAPER LIMIT BUY] txid={order_id} limit={limit_price:.2f} "
            f"vol={volume:.6f} notional=${actual_notional:.2f} "
            f"cash=${self.cash:.2f}"
        )
        return {'txid': [order_id], 'descr': {'order': f'paper limit buy {volume} {pair} @ {limit_price}'}}

    def check_order_status(self, txid: str, candle: Optional[dict] = None) -> dict:
        """
        Simulate limit fill: if candle low <= limit_price, order is filled
        at the limit price (conservative simulation).

        Returns dict with 'status': 'closed' (filled) or 'open' (not yet).
        """
        if not hasattr(self, '_pending_limits'):
            return {'status': 'unknown'}

        pending = self._pending_limits.get(txid)
        if pending is None:
            return {'status': 'unknown'}

        low = float(candle.get('low', float('inf')))
        limit_price = pending['limit_price']

        if low <= limit_price:
            actual_notional = pending['notional']
            return {
                'status': 'closed',
                'fill_price': limit_price,
                'fill_volume': pending['volume'],
                'fill_fee': actual_notional * (self.fee_pct / 100.0) / 2.0,
                'fill_cost': actual_notional,
            }

        return {'status': 'open', 'fill_price': None, 'fill_volume': None,
                'fill_fee': None, 'fill_cost': None}

    def cancel_order(self, txid: str) -> bool:
        """Remove a pending limit order (paper mode)."""
        if not hasattr(self, '_pending_limits'):
            return False

        if txid in self._pending_limits:
            del self._pending_limits[txid]
            logger.info(f"[PAPER] Cancelled limit order: txid={txid}")
            return True
        return False

    def finalize_limit_fill(self, txid: str, trade_record) -> None:
        """
        After simulated fill: deduct cash, create _OpenPosition, update trade_record.
        """
        if not hasattr(self, '_pending_limits'):
            return

        pending = self._pending_limits.pop(txid, None)
        if pending is None:
            logger.warning(f"[PAPER] finalize_limit_fill: no pending order for {txid}")
            return

        pair = pending['pair']
        limit_price = pending['limit_price']
        volume = pending['volume']
        actual_notional = pending['notional']
        entry_fee_usd = actual_notional * (self.fee_pct / 100.0) / 2.0

        # Deduct actual cost (rounded volume * price) from cash
        self.cash -= (actual_notional + entry_fee_usd)
        self._open_positions[txid] = _OpenPosition(
            pair=pair,
            volume=volume,
            entry_price=limit_price,
            notional_usd=actual_notional,
            order_id=txid,
        )

        # Update trade_record
        trade_record.signal_entry_price = trade_record.entry_price
        trade_record.entry_price = limit_price
        trade_record.slippage = 0.0  # Limit fill at exact price
        trade_record.buy_amount = volume
        trade_record.order_id = txid
        trade_record.mode = 'paper'
        trade_record.fee = entry_fee_usd

        logger.info(
            f"[PAPER LIMIT FILL] fill={limit_price:.2f} vol={volume:.6f} "
            f"notional=${actual_notional:.2f} fee=${entry_fee_usd:.2f} "
            f"cash=${self.cash:.2f} equity=${self.equity:.2f}"
        )

    def place_order(self, trade_record) -> dict:
        """
        Paper BUY: validate via API, fetch ask price from Ticker, record fill.

        Caps buy_amount at max_trade_usd, deducts entry fee, updates equity.
        Sets trade_record fields: entry_price, buy_amount, order_id, mode, fee.

        In replay_mode: uses trade_record.entry_price as fill (no API calls).
        """
        # Extract pair from trade_record (multi-pair support)
        pair = getattr(trade_record, 'symbol', None) or self.pair

        if self.replay_mode:
            # Replay: use strategy's signal price as fill (no API needed)
            ask_price = float(getattr(trade_record, 'entry_price', 0))
            if ask_price <= 0:
                logger.error("[PAPER] Replay: trade_record.entry_price is missing or zero")
                return {}
        else:
            # Live/paper: get real ask price from Kraken
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

        # Compute volume in base asset (rounded to exchange lot size)
        volume = self._round_volume(buy_usd / ask_price, pair)
        actual_notional = volume * ask_price  # actual cost after rounding

        # Validate with Kraken (skip in replay mode)
        if not self.replay_mode:
            try:
                self._validate_order('buy', volume, pair)
            except Exception as e:
                logger.error(f"[PAPER] Order validation failed: {e}")
                return {}

        # Compute fee on actual notional (half of round-trip at entry)
        entry_fee_usd = actual_notional * (self.fee_pct / 100.0) / 2.0

        # Deduct actual cost (rounded volume * price) from cash
        self.cash -= (actual_notional + entry_fee_usd)
        order_id = self._next_order_id()
        self._open_positions[order_id] = _OpenPosition(
            pair=pair,
            volume=volume,
            entry_price=ask_price,
            notional_usd=actual_notional,
            order_id=order_id,
        )

        # Update trade_record — preserve signal price before overwriting with fill
        trade_record.signal_entry_price = trade_record.entry_price  # strategy's intended price
        trade_record.entry_price = ask_price                         # actual fill price
        trade_record.slippage = 0.0 if self.replay_mode else (ask_price - (trade_record.signal_entry_price or ask_price))
        trade_record.buy_amount = volume
        trade_record.order_id = order_id
        trade_record.mode = 'paper'
        trade_record.fee = entry_fee_usd

        sig = trade_record.signal_entry_price
        logger.info(
            f"[PAPER BUY] signal={sig:.2f} fill={ask_price:.2f} "
            f"slip={trade_record.slippage:+.2f} vol={volume:.6f} "
            f"notional=${actual_notional:.2f} fee=${entry_fee_usd:.2f} "
            f"cash=${self.cash:.2f} equity=${self.equity:.2f}"
            if sig is not None else
            f"[PAPER BUY] fill={ask_price:.2f} vol={volume:.6f} "
            f"notional=${actual_notional:.2f} fee=${entry_fee_usd:.2f} "
            f"cash=${self.cash:.2f} equity=${self.equity:.2f}"
        )
        return {'txid': [order_id], 'descr': {'order': f'paper buy {volume} {pair} @ market'}}

    def close_order(self, open_trade, trade_record) -> dict:
        """
        Paper SELL: validate via API, fetch bid price from Ticker, compute PnL.

        Deducts exit fee, updates equity, writes to PG strategy_performance.
        Sets trade_record fields: exit_price, order_id, fee (total round-trip).

        In replay_mode: uses trade_record.exit_price as fill (no API calls).
        """
        # Extract pair from open_trade (multi-pair support)
        pair = getattr(open_trade, 'symbol', None) or self.pair

        buy_amount = getattr(open_trade, 'buy_amount', None) or getattr(trade_record, 'buy_amount', None)
        if buy_amount is None:
            logger.error("[PAPER] Cannot close trade: missing buy_amount")
            return {}

        volume = self._round_volume(float(buy_amount), pair)

        if self.replay_mode:
            # Replay: use strategy's exit price as fill (no API needed)
            bid_price = float(getattr(trade_record, 'exit_price', 0))
            if bid_price <= 0:
                logger.error("[PAPER] Replay: trade_record.exit_price is missing or zero")
                return {}
            self._last_prices[pair] = bid_price
        else:
            # Live/paper: get real bid price from Kraken
            ticker = self._get_ticker(pair)
            bid_price = ticker['bid']

        # Validate with Kraken (skip in replay mode)
        if not self.replay_mode:
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
        trade_record.pnl_pct = round((pnl / notional_at_entry * 100.0) if notional_at_entry else 0.0, 4)

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

            entry_ts = getattr(open_trade, 'entry_datetime', None)
            # In replay mode, use exit datetime from trade record; live uses NOW()
            exit_dt = getattr(trade_record, 'exit_datetime', None)
            now = exit_dt if (self.replay_mode and exit_dt) else datetime.now(timezone.utc)
            pair = getattr(open_trade, 'symbol', None) or self.pair
            strategy = getattr(open_trade, 'strategy_name', None) or 'zone_bounce'
            regime = getattr(open_trade, 'trend_direction_entry', None)
            exit_reason = getattr(trade_record, 'exit_reason', None)

            order_type = getattr(open_trade, 'order_type', None) or 'market'
            limit_px = getattr(open_trade, 'limit_price', None)

            # trend_direction_entry holds D-Limit trend_state (with regime fallback)
            dlimit_ts = getattr(open_trade, 'trend_direction_entry', None)

            # entry_context JSONB — stashed by strategy at entry time
            entry_ctx = getattr(open_trade, 'entry_context', None)

            # MFE/MAE stashed on open_trade by strategy monitoring tick
            mfe = getattr(open_trade, 'mfe_pct', None)
            mae = getattr(open_trade, 'mae_pct', None)

            rowcount = record_trade(
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
                dlimit_trend_state=dlimit_ts,
                source='replay' if self.replay_mode else 'paper',
                order_type=order_type,
                limit_price=float(limit_px) if limit_px else None,
                run_id=self.run_id,
                entry_context=entry_ctx,
                mfe_pct=round(mfe, 4) if mfe is not None else None,
                mae_pct=round(mae, 4) if mae is not None else None,
            )
            if rowcount == 0:
                logger.warning(
                    f"[PAPER] strategy_performance write returned 0 rows "
                    f"(possible ON CONFLICT skip): pair={pair} entry_ts={entry_ts}"
                )
            else:
                logger.info(f"[PAPER] Recorded to strategy_performance: pnl_pct={pnl_pct:.2f}%")
        except Exception as e:
            logger.error(f"[PAPER] Failed to write to strategy_performance: {e}")
