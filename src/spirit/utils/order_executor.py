"""
Order executor adapter that bridges TradeRecord-based calls from trade_logic
to the low-level Kraken API client functions. Ensures correct side/volume
are passed and captures txid into TradeRecord.order_id.

Enhanced with fill reconciliation, PG recording, equity tracking, and
slippage measurement to match PaperOrderExecutor capabilities.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from spirit.logger import get_logger

logger = get_logger("order_executor")


class KrakenOrderExecutor:
    def __init__(
        self,
        pair: Optional[str] = None,
        pair_info: Optional[dict] = None,
        starting_equity: float = 10000.0,
        fill_poll_interval: float = 2.0,
        fill_poll_timeout: float = 30.0,
        run_id: str = 'live',
    ):
        from spirit.config import KRAKEN_PAIR

        self.pair = pair or KRAKEN_PAIR
        self._pair_info = pair_info or {}
        self.equity = float(starting_equity)
        self.fill_poll_interval = fill_poll_interval
        self.fill_poll_timeout = fill_poll_timeout
        self._entry_txid: Optional[str] = None
        self.run_id = run_id

        logger.info(
            f"[LIVE] Initialized: equity=${self.equity:.2f} pair={self.pair} "
            f"pairs_configured={len(self._pair_info)}"
        )

    def _round_volume(self, volume: float, pair: str = None) -> float:
        if volume is None:
            return None
        p = pair or self.pair
        decimals = self._pair_info.get(p, {}).get('lot_decimals', 8)
        step = 10 ** (-decimals)
        steps = int(volume / step)
        rounded = max(step, steps * step)
        return float(f"{rounded:.10f}")

    def _get_ticker(self) -> dict:
        """Fetch bid/ask/last from Kraken public Ticker API."""
        from spirit.utils.kraken_api_client import get_ticker
        return get_ticker(self.pair)

    def _wait_for_fill(self, txid: str) -> dict:
        """
        Poll QueryOrders until status='closed' or timeout.
        Returns the order data dict from Kraken.
        """
        from spirit.utils.kraken_order_info import get_order_info

        deadline = time.time() + self.fill_poll_timeout
        last_status = None

        while time.time() < deadline:
            try:
                result = get_order_info(txid)
                order_data = result.get(txid, {})
                status = order_data.get('status', '')
                last_status = status

                if status == 'closed':
                    logger.info(f"[LIVE] Order {txid} filled: status=closed")
                    return order_data

                if status in ('canceled', 'expired'):
                    logger.warning(f"[LIVE] Order {txid} {status}")
                    return order_data

                logger.debug(f"[LIVE] Order {txid} status={status}, polling...")
            except Exception as e:
                logger.warning(f"[LIVE] QueryOrders error for {txid}: {e}")

            time.sleep(self.fill_poll_interval)

        logger.error(
            f"[LIVE] Fill timeout for {txid} after {self.fill_poll_timeout}s "
            f"(last_status={last_status})"
        )
        # Return whatever we last got — caller handles incomplete fills
        try:
            result = get_order_info(txid)
            return result.get(txid, {})
        except Exception:
            return {}

    def _record_to_live_orders(
        self,
        txid: str,
        side: str,
        ordertype: str,
        requested_volume: float,
        fill_data: dict,
        submitted_at: datetime,
        mid_price: Optional[float],
        signal_price: Optional[float],
        strategy_name: Optional[str],
        trade_side: str,
        linked_txid: Optional[str] = None,
    ):
        """Write fill details to PG live_orders table."""
        try:
            from spirit.utils.db_connection import execute_query

            fill_price = float(fill_data.get('price', 0)) or None
            fill_volume = float(fill_data.get('vol_exec', 0)) or None
            fill_cost = float(fill_data.get('cost', 0)) or None
            fill_fee = float(fill_data.get('fee', 0)) or None
            order_status = fill_data.get('status')

            # Fee percentage
            fill_fee_pct = None
            if fill_fee and fill_cost and fill_cost > 0:
                fill_fee_pct = round((fill_fee / fill_cost) * 100, 4)

            # Timestamps from Kraken (unix epoch → datetime)
            kraken_opened_at = None
            kraken_closed_at = None
            if fill_data.get('opentm'):
                kraken_opened_at = datetime.fromtimestamp(
                    float(fill_data['opentm']), tz=timezone.utc
                )
            if fill_data.get('closetm'):
                kraken_closed_at = datetime.fromtimestamp(
                    float(fill_data['closetm']), tz=timezone.utc
                )

            # Latency
            fill_latency_ms = None
            if kraken_closed_at:
                fill_latency_ms = int(
                    (kraken_closed_at - submitted_at).total_seconds() * 1000
                )

            # Slippage
            slippage = None
            slippage_pct = None
            if fill_price and mid_price and mid_price > 0:
                slippage = fill_price - mid_price
                slippage_pct = round((slippage / mid_price) * 100, 4)

            query = """
            INSERT INTO live_orders (
                txid, pair, side, ordertype, requested_volume,
                fill_price, fill_volume, fill_cost, fill_fee, fill_fee_pct,
                order_status, submitted_at, kraken_opened_at, kraken_closed_at,
                fill_latency_ms, mid_price, signal_price, slippage, slippage_pct,
                strategy_name, trade_side, linked_txid, mode, limit_price
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, 'live', %s
            )
            ON CONFLICT (txid) DO NOTHING
            """
            # Extract limit_price if this was a limit order
            lo_limit_price = None
            if ordertype == 'limit' and signal_price:
                lo_limit_price = signal_price

            execute_query(
                query,
                (
                    txid, self.pair, side, ordertype, requested_volume,
                    fill_price, fill_volume, fill_cost, fill_fee, fill_fee_pct,
                    order_status, submitted_at, kraken_opened_at, kraken_closed_at,
                    fill_latency_ms, mid_price, signal_price, slippage, slippage_pct,
                    strategy_name, trade_side, linked_txid, lo_limit_price,
                ),
                fetch='none',
            )
            logger.info(f"[LIVE] Recorded to live_orders: txid={txid} side={trade_side}")
        except Exception as e:
            logger.error(f"[LIVE] Failed to write to live_orders: {e}")

    def _record_to_strategy_performance(self, open_trade, trade_record, pnl: float):
        """Write completed live trade to strategy_performance table."""
        try:
            from spirit.indicators.decision_engine.engine.strategy_performance_writer import record_trade

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

            order_type = getattr(open_trade, 'order_type', None) or 'market'
            limit_px = getattr(open_trade, 'limit_price', None)

            # trend_direction_entry holds D-Limit trend_state (with regime fallback)
            dlimit_ts = getattr(open_trade, 'trend_direction_entry', None)

            entry_ctx = getattr(open_trade, 'entry_context', None)

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
                source='live',
                order_type=order_type,
                limit_price=float(limit_px) if limit_px else None,
                run_id=self.run_id,
                entry_context=entry_ctx,
            )
            if rowcount == 0:
                logger.warning(
                    f"[LIVE] strategy_performance write returned 0 rows "
                    f"(possible ON CONFLICT skip): pair={pair} entry_ts={entry_ts}"
                )
            else:
                logger.info(f"[LIVE] Recorded to strategy_performance: pnl_pct={pnl_pct:.2f}%")
        except Exception as e:
            logger.error(f"[LIVE] Failed to write to strategy_performance: {e}")

    def place_limit_order(self, trade_record, limit_price: float) -> dict:
        """
        Place a limit buy order. Does NOT wait for fill — returns immediately
        with txid. Caller is responsible for polling check_order_status().
        """
        from spirit.config import TRADE_USD_AMOUNT
        from utils import kraken_api_client as kc

        submitted_at = datetime.now(timezone.utc)

        # Get ticker for volume calculation
        try:
            ticker = self._get_ticker()
            ask_price = ticker['ask']
        except Exception as e:
            logger.error(f"[LIVE] Ticker fetch failed for limit order: {e}")
            raise

        # Determine USD size
        buy_usd = getattr(trade_record, 'buy_amount', None)
        if buy_usd is None:
            buy_usd = TRADE_USD_AMOUNT

        buy_usd = float(buy_usd)
        # Convert USD to volume at limit price
        volume = self._round_volume(buy_usd / limit_price)

        logger.info(
            f"[LIVE] Placing LIMIT BUY: pair={self.pair} "
            f"price={limit_price:.2f} volume={volume}"
        )

        result = kc.place_order(
            self.pair, "buy", volume,
            price=limit_price, ordertype="limit", validate=False,
        )

        txids = result.get("txid") or []
        if not txids:
            logger.error(f"[LIVE] No txid from limit order: {result}")
            return result

        txid = txids[0]
        trade_record.order_id = txid
        trade_record.order_type = 'limit'
        trade_record.limit_price = limit_price
        self._entry_txid = txid

        logger.info(
            f"[LIVE LIMIT BUY] txid={txid} limit={limit_price:.2f} "
            f"vol={volume:.6f} notional=${buy_usd:.2f}"
        )
        return result

    def check_order_status(self, txid: str) -> dict:
        """
        Query order status without waiting. Returns dict with:
        status, fill_price, fill_volume, fill_fee, fill_cost.
        """
        from spirit.utils.kraken_order_info import get_order_info

        try:
            result = get_order_info(txid)
            order_data = result.get(txid, {})
            return {
                'status': order_data.get('status', 'unknown'),
                'fill_price': float(order_data.get('price', 0)) or None,
                'fill_volume': float(order_data.get('vol_exec', 0)) or None,
                'fill_fee': float(order_data.get('fee', 0)) or None,
                'fill_cost': float(order_data.get('cost', 0)) or None,
                'raw': order_data,
            }
        except Exception as e:
            logger.warning(f"[LIVE] check_order_status({txid}) failed: {e}")
            return {'status': 'error', 'fill_price': None, 'fill_volume': None,
                    'fill_fee': None, 'fill_cost': None, 'raw': {}}

    def cancel_order(self, txid: str) -> bool:
        """Cancel an unfilled limit order. Returns True on success."""
        from utils import kraken_api_client as kc

        try:
            kc.close_order(txid)
            logger.info(f"[LIVE] Cancelled order: txid={txid}")
            return True
        except Exception as e:
            logger.error(f"[LIVE] Failed to cancel {txid}: {e}")
            return False

    def finalize_limit_fill(self, txid: str, trade_record) -> None:
        """
        After a limit order is confirmed filled: update trade_record fields,
        record to live_orders, and update equity.
        """
        from spirit.utils.kraken_order_info import get_order_info

        submitted_at = datetime.now(timezone.utc)

        try:
            result = get_order_info(txid)
            fill_data = result.get(txid, {})
        except Exception as e:
            logger.error(f"[LIVE] finalize_limit_fill query failed: {e}")
            fill_data = {}

        fill_price = float(fill_data.get('price', 0)) or None
        fill_cost = float(fill_data.get('cost', 0)) or 0.0
        fill_fee = float(fill_data.get('fee', 0)) or 0.0
        fill_vol = float(fill_data.get('vol_exec', 0)) or 0.0

        signal_price = trade_record.limit_price or trade_record.entry_price

        # Update trade_record with actual fill
        trade_record.signal_entry_price = signal_price
        if fill_price:
            trade_record.entry_price = fill_price
        trade_record.buy_amount = fill_vol
        trade_record.mode = 'live'
        trade_record.fee = fill_fee
        if fill_price and signal_price:
            trade_record.slippage = fill_price - signal_price

        # Update equity
        self.equity -= (fill_cost + fill_fee)

        # Record to live_orders
        try:
            ticker = self._get_ticker()
            mid_price = (ticker['ask'] + ticker['bid']) / 2.0
        except Exception:
            mid_price = None

        strategy_name = getattr(trade_record, 'strategy_name', None)
        self._record_to_live_orders(
            txid=txid, side='buy', ordertype='limit',
            requested_volume=fill_vol, fill_data=fill_data,
            submitted_at=submitted_at, mid_price=mid_price,
            signal_price=signal_price, strategy_name=strategy_name,
            trade_side='entry',
        )

        logger.info(
            f"[LIVE LIMIT FILL] txid={txid} fill={fill_price} "
            f"vol={fill_vol:.6f} cost=${fill_cost:.2f} "
            f"fee=${fill_fee:.4f} equity=${self.equity:.2f}"
        )

    def place_order(self, trade_record) -> dict:
        """
        Place a buy order using trade_record fields.
        Fetches ticker for mid-price reference, places market buy,
        waits for fill, updates trade_record with actual fill details,
        records to live_orders, and updates equity.
        """
        from spirit.config import TRADE_USD_AMOUNT
        from utils import kraken_api_client as kc

        submitted_at = datetime.now(timezone.utc)

        # Get ticker for mid-price reference
        try:
            ticker = self._get_ticker()
            mid_price = (ticker['ask'] + ticker['bid']) / 2.0
            ask_price = ticker['ask']
        except Exception as e:
            logger.error(f"[LIVE] Ticker fetch failed: {e}")
            mid_price = None
            ask_price = None

        # Determine volume
        price = getattr(trade_record, "entry_price", None)
        buy_amount = getattr(trade_record, "buy_amount", None)
        if buy_amount is None:
            if price is None and ask_price is not None:
                price = ask_price
            elif price is None:
                raise ValueError("Cannot compute buy volume: missing entry_price, buy_amount, and ticker")
            buy_amount = TRADE_USD_AMOUNT / float(price)
            trade_record.buy_amount = buy_amount

        # If buy_amount is in USD (> 1.0 for BTC pairs), convert to BTC volume
        ref_price = ask_price or price
        if ref_price and float(buy_amount) > 1.0 and ref_price > 100:
            buy_amount_btc = float(buy_amount) / ref_price
        else:
            buy_amount_btc = float(buy_amount)

        volume = self._round_volume(buy_amount_btc)
        signal_price = price  # preserve strategy's intended price

        logger.info(f"[LIVE] Placing BUY: pair={self.pair} volume={volume} mid={mid_price}")

        # Place market buy
        result = kc.place_order(self.pair, "buy", volume, ordertype="market", validate=False)

        # Extract txid
        txids = result.get("txid") or []
        if not txids:
            logger.error(f"[LIVE] No txid returned from place_order: {result}")
            return result

        txid = txids[0]
        trade_record.order_id = txid
        self._entry_txid = txid

        # Wait for fill
        fill_data = self._wait_for_fill(txid)
        fill_price = float(fill_data.get('price', 0)) or None
        fill_cost = float(fill_data.get('cost', 0)) or 0.0
        fill_fee = float(fill_data.get('fee', 0)) or 0.0
        fill_vol = float(fill_data.get('vol_exec', 0)) or volume

        # Update trade_record with actual fill
        trade_record.signal_entry_price = signal_price
        if fill_price:
            trade_record.entry_price = fill_price
        trade_record.buy_amount = fill_vol
        trade_record.mode = 'live'
        trade_record.fee = fill_fee
        if fill_price and mid_price:
            trade_record.slippage = fill_price - mid_price

        # Update equity
        self.equity -= (fill_cost + fill_fee)

        # Record to live_orders
        strategy_name = getattr(trade_record, 'strategy_name', None)
        self._record_to_live_orders(
            txid=txid,
            side='buy',
            ordertype='market',
            requested_volume=volume,
            fill_data=fill_data,
            submitted_at=submitted_at,
            mid_price=mid_price,
            signal_price=signal_price,
            strategy_name=strategy_name,
            trade_side='entry',
        )

        logger.info(
            f"[LIVE BUY] txid={txid} fill={fill_price} vol={fill_vol:.6f} "
            f"cost=${fill_cost:.2f} fee=${fill_fee:.4f} equity=${self.equity:.2f}"
        )
        return result

    def close_order(self, open_trade, trade_record) -> dict:
        """
        Close the position by selling the originally bought amount.
        Waits for fill, computes PnL, updates trade_record, records to
        live_orders and strategy_performance, and updates equity.
        """
        from utils import kraken_api_client as kc

        submitted_at = datetime.now(timezone.utc)

        # Get ticker for mid-price reference
        try:
            ticker = self._get_ticker()
            mid_price = (ticker['ask'] + ticker['bid']) / 2.0
        except Exception as e:
            logger.error(f"[LIVE] Ticker fetch failed on close: {e}")
            mid_price = None

        buy_amount = getattr(open_trade, "buy_amount", None) or getattr(trade_record, "buy_amount", None)
        if buy_amount is None:
            raise ValueError("Cannot close trade: missing buy_amount on open_trade/trade_record")
        volume = self._round_volume(float(buy_amount))

        signal_price = getattr(trade_record, "exit_price", None)
        logger.info(f"[LIVE] Placing SELL: pair={self.pair} volume={volume} mid={mid_price}")

        # Place market sell
        result = kc.place_order(self.pair, "sell", volume, ordertype="market", validate=False)

        # Extract txid
        txids = result.get("txid") or []
        if not txids:
            logger.error(f"[LIVE] No txid returned from close_order: {result}")
            return result

        txid = txids[0]
        trade_record.order_id = txid

        # Wait for fill
        fill_data = self._wait_for_fill(txid)
        fill_price = float(fill_data.get('price', 0)) or None
        fill_cost = float(fill_data.get('cost', 0)) or 0.0
        fill_fee = float(fill_data.get('fee', 0)) or 0.0
        fill_vol = float(fill_data.get('vol_exec', 0)) or volume

        # Compute PnL
        entry_price = getattr(open_trade, 'entry_price', None) or 0.0
        entry_fee = getattr(open_trade, 'fee', 0.0) or 0.0
        notional_at_exit = fill_vol * (fill_price or 0.0)
        notional_at_entry = fill_vol * entry_price
        total_fee = entry_fee + fill_fee
        pnl = notional_at_exit - notional_at_entry - total_fee

        # Update trade_record with actual fill
        trade_record.signal_exit_price = signal_price
        if fill_price:
            trade_record.exit_price = fill_price
        trade_record.fee = total_fee
        trade_record.pnl = pnl
        trade_record.pnl_pct = round((pnl / notional_at_entry * 100.0) if notional_at_entry else 0.0, 4)

        # Update equity
        self.equity += (fill_cost - fill_fee)

        # Record to live_orders (exit leg)
        entry_txid = self._entry_txid
        strategy_name = getattr(open_trade, 'strategy_name', None) or getattr(trade_record, 'strategy_name', None)
        self._record_to_live_orders(
            txid=txid,
            side='sell',
            ordertype='market',
            requested_volume=volume,
            fill_data=fill_data,
            submitted_at=submitted_at,
            mid_price=mid_price,
            signal_price=signal_price,
            strategy_name=strategy_name,
            trade_side='exit',
            linked_txid=entry_txid,
        )

        # Record to strategy_performance
        self._record_to_strategy_performance(open_trade, trade_record, pnl)

        # Clear entry txid
        self._entry_txid = None

        logger.info(
            f"[LIVE SELL] txid={txid} fill={fill_price} "
            f"pnl=${pnl:.2f} fee=${total_fee:.4f} equity=${self.equity:.2f}"
        )
        return result
