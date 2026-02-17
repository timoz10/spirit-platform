"""
spirit_main.py

Main orchestration script for kraken-bot project.
Coordinates data sources (live/CSV, single/multi-pair, single/multi-interval),
warmup/backfill, feature engineering, strategy evaluation, and graceful shutdown.

Multi-pair support (Phase 1): Each pair gets its own SpiritContext, strategy
instance, and TradeStateManager. Equity is shared via the order executor.
"""

import faulthandler; faulthandler.enable()
import os
import sys
import time
import argparse
import pandas as pd

from spirit.logger import get_logger
from spirit.config import KRAKEN_OHLC_COUNT, KRAKEN_OHLC_INTERVAL
from spirit.utils.config_loader import get_config
from spirit.utils.data_source import LiveDataSource, CsvDataSource

# Optional multi-interval sources (may not exist on all branches)
try:
    from spirit.utils.multi_interval_data_source import MultiIntervalLiveDataSource
except Exception:
    MultiIntervalLiveDataSource = None
try:
    from spirit.utils.data_source import CsvMultiIntervalDataSource
except Exception:
    CsvMultiIntervalDataSource = None

from spirit.strategy_config import get_strategy
from spirit.trade_types import TradeRecord
from spirit.trade_logic import (
    MultiPairTradeStateManager,
    MultiStrategyTradeStateManager,
    process_trade_signals,
)


_ENTRY_COPY_ATTRS = [
    'entry_index', 'entry_datetime', 'entry_price',
    'macd_bullish_cross_entry', 'atr_entry', 'sma200_entry',
    'rsi_entry', 'impulse_macd_entry', 'adx_entry',
    'plus_di_entry', 'minus_di_entry', 'trend_direction_entry',
    'buy_amount', 'symbol', 'interval', 'strategy_name', 'mode',
]


class SpiritOrchestrator:
    """Owns candle processing, trade execution, and state management.

    Multi-pair: each pair gets its own context, strategy instance, and trade
    state manager. Equity is shared via the single order executor.
    """

    def __init__(self, context_manager, strategies, trade_state_manager, order_executor,
                 risk_gate, mode, interval, monitoring_intervals, pairs,
                 update_state=None, web_inc=None, web_sig=None, web_dec=None,
                 registry=None):
        self.context_manager = context_manager      # ContextManager
        self.strategies = strategies                 # Dict[str, BaseStrategy]
        self.pairs = pairs                           # List[str]
        self.trading_enabled = bool(strategies) or (registry is not None and len(registry) > 0)
        self.trade_state_manager = trade_state_manager  # MultiPairTradeStateManager or MultiStrategyTradeStateManager
        self.order_executor = order_executor
        self.risk_gate = risk_gate
        self.mode = mode
        self.interval = interval
        self.monitoring_intervals = set(monitoring_intervals)
        self.registry = registry                     # StrategyRegistry (None for non-Spine)
        self.data_source = None  # set after data source creation
        self.csv_thread = None

        # Web dashboard helpers (None = disabled)
        self._update_state = update_state
        self._web_inc = web_inc
        self._web_sig = web_sig
        self._web_dec = web_dec

        self.logger = get_logger("spirit_orchestrator")
        self._cb_logger = get_logger("spirit_callback")

    # -----------------------------------------------------------------
    # Multi-pair entry point: (pair, interval, window)
    # -----------------------------------------------------------------

    def on_pair_candle(self, pair, interval_val, window_df, is_csv=False):
        """Multi-pair callback: routes (pair, interval, window) to the right context."""
        try:
            iv = int(interval_val)
            ctx = self.context_manager.get(pair)

            # Append to per-pair context
            records = getattr(window_df, 'records', None)
            if records:
                latest = records[-1]
                candle_dict = latest.__dict__.copy() if hasattr(latest, '__dict__') else dict(latest)
                ctx.append_candle(candle_dict, interval=iv)

            # Registry-based routing (Spine multi-strategy)
            if self.registry is not None:
                for slot in self.registry.get_signal_strategies(iv):
                    if pair in slot.pairs:
                        self._evaluate_pair_strategy(pair, slot.name, slot.strategy, window_df)

                for slot in self.registry.get_monitor_strategies(iv):
                    if pair in slot.pairs:
                        self._monitor_pair_strategy(pair, slot.name, slot.strategy, iv, window_df)
                return

            # Legacy single-strategy routing
            if iv == int(self.interval):
                self._evaluate_pair(pair, window_df)
            elif iv in self.monitoring_intervals:
                self._monitor_pair(pair, iv, window_df)
        except Exception as e:
            self.logger.error(f"[{pair}] on_pair_candle error: {e}")

    # -----------------------------------------------------------------
    # Per-pair strategy evaluation and monitoring
    # -----------------------------------------------------------------

    def _evaluate_pair(self, pair, window_df):
        """Run strategy evaluation for a specific pair."""
        if self.data_source and not getattr(self.data_source, 'warmup_complete', True):
            return

        ctx = self.context_manager.get(pair)
        tsm = self.trade_state_manager.get(pair)
        strategy = self.strategies.get(pair)
        if not strategy:
            return

        try:
            if self._web_inc:
                self._web_inc()

            # Thread-local routing for get_spirit_temp_ti()
            from spirit.utils.db_utils import set_active_pair
            set_active_pair(pair)

            result = strategy.evaluate_trade(
                pair, mode=self.mode, open_trade=tsm.open_trade
            )
            entry_flag = False
            exit_flag = False
            trade_record = None

            if isinstance(result, TradeRecord):
                trade_record = result
            elif isinstance(result, dict):
                entry_flag = bool(result.get('entry', False))
                exit_flag = bool(result.get('exit', False))
                details = result.get('details') or {}
                trade_record = self._build_trade_record(details)

                # Record signal to web dashboard
                if self._web_sig and details.get('signal') is not None:
                    sig = details['signal']
                    self._web_sig({
                        'datetime': getattr(sig, 'datetime', None),
                        'confidence_score': getattr(sig, 'confidence_score', None),
                        'zone_id': getattr(sig, 'zone_id', None),
                        'regime': getattr(sig, 'regime', None),
                        'price': getattr(sig, 'price', None),
                        'pair': pair,
                    })

                # RiskGate integration
                if self.risk_gate and entry_flag and trade_record is not None:
                    signal = details.get('signal')
                    if signal is not None:
                        risk_decision = self.risk_gate.evaluate(signal)
                        if self._web_dec:
                            self._web_dec(risk_decision.to_dict())
                        if not risk_decision.trade:
                            self._cb_logger.info(
                                f"[{pair}][RISK_GATE] Skipped: {risk_decision.skip_reason}"
                            )
                            entry_flag = False
                        else:
                            trade_record.buy_amount = risk_decision.position_size_usd
                            self._cb_logger.info(
                                f"[{pair}][RISK_GATE] Sized: ${risk_decision.position_size_usd:.0f} "
                                f"({risk_decision.profile_tier}, R:R={risk_decision.rr_ratio:.1f})"
                            )
                            # Capture entry context for dynamic exit
                            signal_for_exit = details.get('signal')
                            if hasattr(strategy, 'on_entry_confirmed') and signal_for_exit:
                                strategy.on_entry_confirmed(pair, signal_for_exit, risk_decision)

            if entry_flag and trade_record is not None:
                process_trade_signals(
                    "buy", trade_record, self.mode, tsm,
                    order_executor=self.order_executor, logger=self._cb_logger,
                )
                if tsm.open_trade is not None:
                    ctx.set_open_trade(tsm.open_trade)
                if self._update_state and tsm.open_trade is not None:
                    ot = tsm.open_trade
                    self._update_state(open_trade={
                        'pair': pair,
                        'entry_price': getattr(ot, 'entry_price', None),
                        'entry_datetime': str(getattr(ot, 'entry_datetime', '')),
                        'buy_amount': getattr(ot, 'buy_amount', None),
                    })

            if exit_flag and trade_record is not None and tsm.open_trade is not None:
                self._process_exit(pair, trade_record)

            # Periodic state save (every 10 candles)
            if ctx.health['candles_processed'] % 10 == 0:
                ctx.save_state()
        except Exception as e:
            self._cb_logger.exception(f"[{pair}] Exception in _evaluate_pair: {e}")
            ctx.health['errors'] += 1

    def _evaluate_pair_strategy(self, pair, strategy_name, strategy, window_df):
        """Run strategy evaluation for a (pair, strategy) slot.

        Used by multi-strategy Spine: each strategy gets its own trade state slot.
        """
        if self.data_source and not getattr(self.data_source, 'warmup_complete', True):
            return

        ctx = self.context_manager.get(pair)

        # Multi-strategy: get (pair, strategy_name) slot from MultiStrategyTradeStateManager
        if isinstance(self.trade_state_manager, MultiStrategyTradeStateManager):
            tsm = self.trade_state_manager.get(pair, strategy_name)
        else:
            tsm = self.trade_state_manager.get(pair)

        try:
            if self._web_inc:
                self._web_inc()

            from spirit.utils.db_utils import set_active_pair
            set_active_pair(pair)

            result = strategy.evaluate_trade(
                pair, mode=self.mode, open_trade=tsm.open_trade
            )
            entry_flag = False
            exit_flag = False
            trade_record = None

            if isinstance(result, TradeRecord):
                trade_record = result
            elif isinstance(result, dict):
                entry_flag = bool(result.get('entry', False))
                exit_flag = bool(result.get('exit', False))
                details = result.get('details') or {}
                trade_record = self._build_trade_record(details)

                if self._web_sig and details.get('signal') is not None:
                    sig = details['signal']
                    self._web_sig({
                        'datetime': getattr(sig, 'datetime', None),
                        'confidence_score': getattr(sig, 'confidence_score', None),
                        'zone_id': getattr(sig, 'zone_id', None),
                        'regime': getattr(sig, 'regime', None),
                        'price': getattr(sig, 'price', None),
                        'pair': pair,
                    })

                # RiskGate integration
                if self.risk_gate and entry_flag and trade_record is not None:
                    signal = details.get('signal')
                    if signal is not None:
                        deployed = 0.0
                        if isinstance(self.trade_state_manager, MultiStrategyTradeStateManager):
                            deployed = self.trade_state_manager.total_deployed_usd()
                        risk_decision = self.risk_gate.evaluate(signal, deployed_usd=deployed)
                        if self._web_dec:
                            self._web_dec(risk_decision.to_dict())
                        if not risk_decision.trade:
                            self._cb_logger.info(
                                f"[{pair}:{strategy_name}][RISK_GATE] Skipped: {risk_decision.skip_reason}"
                            )
                            entry_flag = False
                        else:
                            trade_record.buy_amount = risk_decision.position_size_usd
                            self._cb_logger.info(
                                f"[{pair}:{strategy_name}][RISK_GATE] Sized: ${risk_decision.position_size_usd:.0f} "
                                f"({risk_decision.profile_tier}, R:R={risk_decision.rr_ratio:.1f})"
                            )
                            # Capture entry context for dynamic exit
                            signal_for_exit = details.get('signal')
                            if hasattr(strategy, 'on_entry_confirmed') and signal_for_exit:
                                strategy.on_entry_confirmed(pair, signal_for_exit, risk_decision)

            # Check concurrency limit before opening
            if entry_flag and trade_record is not None:
                if isinstance(self.trade_state_manager, MultiStrategyTradeStateManager):
                    if not self.trade_state_manager.can_open_for_pair(pair):
                        self._cb_logger.info(
                            f"[{pair}:{strategy_name}] Concurrency limit hit "
                            f"({self.trade_state_manager.count_open_for_pair(pair)}"
                            f"/{self.trade_state_manager.max_concurrent_per_pair})"
                        )
                        entry_flag = False

            if entry_flag and trade_record is not None:
                process_trade_signals(
                    "buy", trade_record, self.mode, tsm,
                    order_executor=self.order_executor, logger=self._cb_logger,
                )
                if tsm.open_trade is not None:
                    ctx.set_open_trade(tsm.open_trade)
                if self._update_state and tsm.open_trade is not None:
                    ot = tsm.open_trade
                    self._update_state(open_trade={
                        'pair': pair,
                        'strategy': strategy_name,
                        'entry_price': getattr(ot, 'entry_price', None),
                        'entry_datetime': str(getattr(ot, 'entry_datetime', '')),
                        'buy_amount': getattr(ot, 'buy_amount', None),
                    })

            if exit_flag and trade_record is not None and tsm.open_trade is not None:
                self._process_exit_strategy(pair, strategy_name, trade_record)

            if ctx.health['candles_processed'] % 10 == 0:
                ctx.save_state()
        except Exception as e:
            self._cb_logger.exception(f"[{pair}:{strategy_name}] Exception in _evaluate_pair_strategy: {e}")
            ctx.health['errors'] += 1

    def _process_exit_strategy(self, pair, strategy_name, trade_record):
        """Exit logic for a (pair, strategy) slot."""
        if isinstance(self.trade_state_manager, MultiStrategyTradeStateManager):
            tsm = self.trade_state_manager.get(pair, strategy_name)
        else:
            tsm = self.trade_state_manager.get(pair)
        ctx = self.context_manager.get(pair)

        for attr in _ENTRY_COPY_ATTRS:
            setattr(trade_record, attr, getattr(tsm.open_trade, attr, None))
        process_trade_signals(
            "sell", trade_record, self.mode, tsm,
            order_executor=self.order_executor, logger=self._cb_logger,
        )
        if self.risk_gate and self.mode in ('paper', 'live') and self.order_executor is not None:
            self.risk_gate.update_equity(self.order_executor.equity)
        ctx.clear_open_trade()
        if self.order_executor and hasattr(self.order_executor, 'equity'):
            ctx.set_equity(self.order_executor.equity)
        if self._update_state:
            eq = self.order_executor.equity if (self.order_executor and hasattr(self.order_executor, 'equity')) else None
            self._update_state(open_trade=None, **(({'equity': eq}) if eq is not None else {}))

    def _monitor_pair_strategy(self, pair, strategy_name, strategy, interval_val, window_df):
        """Route monitoring ticks to a specific (pair, strategy) slot."""
        if isinstance(self.trade_state_manager, MultiStrategyTradeStateManager):
            tsm = self.trade_state_manager.get(pair, strategy_name)
        else:
            tsm = self.trade_state_manager.get(pair)

        if not strategy or tsm.open_trade is None:
            return
        try:
            records = getattr(window_df, 'records', None)
            if not records:
                return
            latest = records[-1]
            candle_dict = latest.__dict__.copy() if hasattr(latest, '__dict__') else dict(latest)

            from spirit.utils.db_utils import set_active_pair
            set_active_pair(pair)

            result = strategy.on_monitoring_tick(pair, int(interval_val), candle_dict, tsm.open_trade)
            if result and result.get('exit'):
                details = result.get('details', {})
                tr = self._build_trade_record(details)
                if tr is not None and tsm.open_trade is not None:
                    self._process_exit_strategy(pair, strategy_name, tr)
        except Exception as e:
            self._cb_logger.exception(f"[{pair}:{strategy_name}] Exception in _monitor_pair_strategy: {e}")

    def _monitor_pair(self, pair, interval_val, window_df):
        """Route monitoring-interval candles to exit monitoring or entry scanning.

        When a trade is open: delegates to strategy.on_monitoring_tick() for exit checks.
        When no trade is open: delegates to strategy.on_entry_scan_tick() for sub-signal
        entry detection (e.g. 1m zone proximity).
        """
        tsm = self.trade_state_manager.get(pair)
        strategy = self.strategies.get(pair)
        if not strategy:
            return
        try:
            records = getattr(window_df, 'records', None)
            if not records:
                return
            latest = records[-1]
            candle_dict = latest.__dict__.copy() if hasattr(latest, '__dict__') else dict(latest)

            from spirit.utils.db_utils import set_active_pair
            set_active_pair(pair)

            if tsm.open_trade is not None:
                # EXISTING: exit monitoring (unchanged)
                result = strategy.on_monitoring_tick(
                    pair, int(interval_val), candle_dict, tsm.open_trade
                )
                if result and result.get('exit'):
                    details = result.get('details', {})
                    trade_record = self._build_trade_record(details)
                    if trade_record is not None and tsm.open_trade is not None:
                        self._process_exit(pair, trade_record)
            else:
                # NEW: entry scan on sub-signal ticks
                result = strategy.on_entry_scan_tick(pair, int(interval_val), candle_dict)
                if result and result.get('entry'):
                    self._process_entry(pair, result)
        except Exception as e:
            self._cb_logger.exception(f"[{pair}] Exception in _monitor_pair: {e}")

    # -----------------------------------------------------------------
    # Legacy single-pair callbacks (backward compat with old data sources)
    # -----------------------------------------------------------------

    def on_new_candle(self, window_df):
        """Single-pair single-interval callback: routes to first pair."""
        pair = self.pairs[0]
        if self.data_source and not getattr(self.data_source, 'warmup_complete', True):
            return
        ctx = self.context_manager.get(pair)
        # Append candle if not handled by multi-interval
        if self.data_source and not hasattr(self.data_source, 'buffers'):
            records = getattr(window_df, 'records', None)
            if records:
                latest = records[-1]
                candle_dict = latest.__dict__.copy() if hasattr(latest, '__dict__') else dict(latest)
                ctx.append_candle(candle_dict, interval=self.interval)
        self._evaluate_pair(pair, window_df)

    def on_monitoring_tick(self, interval_val, window_df):
        """Single-pair multi-interval monitoring callback."""
        self._monitor_pair(self.pairs[0], interval_val, window_df)

    def on_interval_window(self, interval_val, window_df, is_csv=False):
        """Single-pair multi-interval callback."""
        self.on_pair_candle(self.pairs[0], interval_val, window_df, is_csv=is_csv)

    # -----------------------------------------------------------------
    # Shutdown
    # -----------------------------------------------------------------

    def graceful_shutdown(self, no_pause=False, is_csv=False):
        """Save state for all pairs and clean up."""
        self.context_manager.save_all()
        self.logger.info("Final state saved to PG for all pairs")

        try:
            if not no_pause and not is_csv and sys.stdin.isatty():
                input("\n[PAUSE] Press Enter to stop data sources and exit...\n")
        except Exception:
            pass

        if self.data_source and hasattr(self.data_source, 'stop'):
            try:
                self.data_source.stop()
            except Exception:
                pass

        try:
            if self.csv_thread is not None and self.csv_thread.is_alive():
                self.csv_thread.join(timeout=2)
        except Exception:
            pass

    # -----------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------

    def _process_entry(self, pair, result):
        """Common entry logic extracted from _evaluate_pair().

        Handles RiskGate evaluation, process_trade_signals("buy"),
        state updates, and web dashboard recording.

        Args:
            pair: Trading pair symbol
            result: Strategy result dict with 'entry', 'details', etc.
        """
        ctx = self.context_manager.get(pair)
        tsm = self.trade_state_manager.get(pair)
        strategy = self.strategies.get(pair)

        entry_flag = bool(result.get('entry', False))
        details = result.get('details') or {}
        trade_record = self._build_trade_record(details)

        # Record signal to web dashboard
        if self._web_sig and details.get('signal') is not None:
            sig = details['signal']
            self._web_sig({
                'datetime': getattr(sig, 'datetime', None),
                'confidence_score': getattr(sig, 'confidence_score', None),
                'zone_id': getattr(sig, 'zone_id', None),
                'regime': getattr(sig, 'regime', None),
                'price': getattr(sig, 'price', None),
                'pair': pair,
            })

        # RiskGate integration
        if self.risk_gate and entry_flag and trade_record is not None:
            signal = details.get('signal')
            if signal is not None:
                risk_decision = self.risk_gate.evaluate(signal)
                if self._web_dec:
                    self._web_dec(risk_decision.to_dict())
                if not risk_decision.trade:
                    self._cb_logger.info(
                        f"[{pair}][RISK_GATE] Skipped: {risk_decision.skip_reason}"
                    )
                    entry_flag = False
                else:
                    trade_record.buy_amount = risk_decision.position_size_usd
                    self._cb_logger.info(
                        f"[{pair}][RISK_GATE] Sized: ${risk_decision.position_size_usd:.0f} "
                        f"({risk_decision.profile_tier}, R:R={risk_decision.rr_ratio:.1f})"
                    )
                    # Capture entry context for dynamic exit
                    signal_for_exit = details.get('signal')
                    if strategy and hasattr(strategy, 'on_entry_confirmed') and signal_for_exit:
                        strategy.on_entry_confirmed(pair, signal_for_exit, risk_decision)

        if entry_flag and trade_record is not None:
            process_trade_signals(
                "buy", trade_record, self.mode, tsm,
                order_executor=self.order_executor, logger=self._cb_logger,
            )
            if tsm.open_trade is not None:
                ctx.set_open_trade(tsm.open_trade)
            if self._update_state and tsm.open_trade is not None:
                ot = tsm.open_trade
                self._update_state(open_trade={
                    'pair': pair,
                    'entry_price': getattr(ot, 'entry_price', None),
                    'entry_datetime': str(getattr(ot, 'entry_datetime', '')),
                    'buy_amount': getattr(ot, 'buy_amount', None),
                })

    def _process_exit(self, pair, trade_record):
        """Common exit logic for a specific pair."""
        tsm = self.trade_state_manager.get(pair)
        ctx = self.context_manager.get(pair)

        # Copy entry context to exit record
        for attr in _ENTRY_COPY_ATTRS:
            setattr(trade_record, attr, getattr(tsm.open_trade, attr, None))
        process_trade_signals(
            "sell", trade_record, self.mode, tsm,
            order_executor=self.order_executor, logger=self._cb_logger,
        )
        # Sync paper equity into RiskGate after trade closes
        if self.risk_gate and self.mode in ('paper', 'live') and self.order_executor is not None:
            self.risk_gate.update_equity(self.order_executor.equity)
        # Persist state change to PG
        ctx.clear_open_trade()
        if self.order_executor and hasattr(self.order_executor, 'equity'):
            ctx.set_equity(self.order_executor.equity)
        # Update web dashboard after trade close
        if self._update_state:
            eq = self.order_executor.equity if (self.order_executor and hasattr(self.order_executor, 'equity')) else None
            self._update_state(open_trade=None, **(({'equity': eq}) if eq is not None else {}))

    @staticmethod
    def _build_trade_record(details):
        """Build a TradeRecord from strategy details dict, or None on failure."""
        try:
            fields = set(TradeRecord.__dataclass_fields__.keys())
            kwargs = {k: v for k, v in details.items() if k in fields}
            return TradeRecord(**kwargs)
        except Exception:
            return None


# =====================================================================
# main()
# =====================================================================

def main():
    logger = get_logger("spirit_main")
    logger.info("---------- SPIRIT starting ----------")

    # Pre-flight validation
    from spirit.utils.preflight import run_preflight
    preflight = run_preflight()
    if not preflight.passed:
        logger.error("Pre-flight checks FAILED. Spirit cannot start.")
        for f in preflight.fatal_failures:
            logger.error(f"  {f.name}: {f.message}")
        raise SystemExit(1)
    logger.info("Pre-flight checks passed.")

    # Start web dashboard if enabled
    if get_config('SPIRIT_WEB', '').lower() in ('1', 'true', 'yes'):
        from spirit.web import start_web_server, update_state
        web_port = int(get_config('SPIRIT_WEB_PORT', '8377'))
        start_web_server(port=web_port)
    else:
        update_state = None

    # CLI
    parser = argparse.ArgumentParser(description="SPIRIT Main Orchestration")
    parser.add_argument('--csv', dest='data_source', action='store_const', const='csv', default='kraken')
    parser.add_argument('--csv-path', type=str, default='test_data.csv')
    parser.add_argument('--buffer-size', type=int, default=KRAKEN_OHLC_COUNT)
    parser.add_argument('--mode', type=str, choices=['test', 'paper', 'live'], default='test')
    parser.add_argument('--multi-interval', action='store_true')
    parser.add_argument('--duration', type=int, default=None)
    parser.add_argument('--no-pause', action='store_true')
    parser.add_argument('--exit-after-warmup', action='store_true')
    args = parser.parse_args()

    # ---------------------------------------------------------------
    # Determine pairs and create per-pair strategy instances
    # ---------------------------------------------------------------

    # SPIRIT_PAIRS config override (e.g. "XBTUSD,ETHUSD,SOLUSD")
    pairs_str = (get_config('SPIRIT_PAIRS', '') or '').strip()
    config_pairs = [p.strip() for p in pairs_str.split(',') if p.strip()] if pairs_str else []

    # Create a probe strategy to read DataRequirements (cheap — no cache loading)
    probe_strategy = get_strategy()
    trading_enabled = probe_strategy is not None

    requirements = None
    if trading_enabled:
        requirements = probe_strategy.get_data_requirements()

    # Final pair list: config override > strategy requirements > default
    if config_pairs:
        pairs = config_pairs
    elif requirements:
        pairs = requirements.pairs
    else:
        pairs = [get_config('KRAKEN_PAIR', 'XBTUSD')]

    # Create one strategy instance per pair
    strategies = {}
    if trading_enabled:
        for pair in pairs:
            s = get_strategy(extra_params={'filter_pair': pair})
            if s is not None:
                strategies[pair] = s
        if not strategies:
            trading_enabled = False

    if not trading_enabled:
        logger.warning("=" * 60)
        logger.warning("  NO TRADING ALGORITHM LOADED")
        logger.warning("  Spirit is running in MONITOR-ONLY mode.")
        logger.warning("  Set SPIRIT_STRATEGY env var to enable trading.")
        logger.warning("=" * 60)
    else:
        # Requirements from first pair's strategy (all share same intervals/warmup)
        first_strategy = next(iter(strategies.values()))
        requirements = first_strategy.get_data_requirements()
        logger.info(f"Trading algorithm: {type(first_strategy).__name__}")
        logger.info(
            f"[HANDSHAKE] pairs={pairs} "
            f"signal_interval={requirements.signal_interval}m "
            f"monitoring_intervals={requirements.monitoring_intervals} "
            f"warmup_candles={requirements.warmup_candles} "
            f"uses_risk_gate={first_strategy.uses_risk_gate}"
        )
        env_interval = int(KRAKEN_OHLC_INTERVAL)
        if env_interval != requirements.signal_interval:
            logger.warning(
                f"[HANDSHAKE] KRAKEN_OHLC_INTERVAL={env_interval} but strategy declares "
                f"signal_interval={requirements.signal_interval}. "
                f"Strategy's signal_interval will be used."
            )

    # ---------------------------------------------------------------
    # Per-pair context and trade state
    # ---------------------------------------------------------------

    from spirit.context_manager import ContextManager
    from spirit.utils.db_utils import set_spirit_context
    context_manager = ContextManager(pairs=pairs, persist_to_pg=True)
    set_spirit_context(context_manager)
    logger.info(f"ContextManager active for pairs={pairs}")
    context_manager.restore_all()

    # Detect Spine multi-strategy mode
    registry = None
    is_spine = False
    first_strategy = next(iter(strategies.values()), None) if strategies else None
    if first_strategy is not None:
        from spirit.strategies.spine import SpineStrategy
        is_spine = isinstance(first_strategy, SpineStrategy)

    if is_spine and first_strategy is not None:
        # Build StrategyRegistry from Spine's config-loaded children
        from spirit.strategy_registry import StrategyRegistry

        registry = StrategyRegistry()
        spine_strategy = first_strategy  # All pairs got the same SpineStrategy type

        for child_name, child_strategy in spine_strategy.children.items():
            child_pairs = spine_strategy.child_pairs.get(child_name, pairs)
            # Filter to only pairs in our global pair list
            child_pairs = [p for p in child_pairs if p in pairs]
            if child_pairs:
                registry.register(child_name, child_strategy, child_pairs)

        # Expand pairs to include all registry pairs
        registry_pairs = registry.all_pairs()
        for rp in registry_pairs:
            if rp not in pairs:
                pairs.append(rp)

        # Override requirements with registry's union
        requirements = first_strategy.get_data_requirements()

        trade_state_manager = MultiStrategyTradeStateManager(
            max_concurrent_per_pair=spine_strategy.max_concurrent_per_pair,
        )
        logger.info(
            f"[SPINE] Registry mode: {len(registry)} strategies, "
            f"pairs={pairs}, max_concurrent={spine_strategy.max_concurrent_per_pair}"
        )
    else:
        trade_state_manager = MultiPairTradeStateManager()

    # Order executor (shared — one account across all pairs)
    order_executor = None
    try:
        if trading_enabled and args.mode == 'live':
            from spirit.utils.order_executor import KrakenOrderExecutor
            order_executor = KrakenOrderExecutor(
                starting_equity=float(os.getenv('ACCOUNT_EQUITY', '10000')),
            )
        elif trading_enabled and args.mode == 'paper':
            from spirit.utils.paper_order_executor import PaperOrderExecutor
            order_executor = PaperOrderExecutor(
                starting_equity=float(get_config('PAPER_STARTING_EQUITY', '1000')),
                max_trade_usd=float(get_config('PAPER_MAX_TRADE_USD', '250')),
                fee_pct=float(get_config('PAPER_FEE_PCT', '0.40')),
            )
    except (ImportError, ValueError, OSError) as e:
        logger.exception(f"Order executor init failed: {e}")
        order_executor = None

    # RiskGate (shared — portfolio-level risk management)
    risk_gate = None
    rg_strategy = next(iter(strategies.values()), None) if strategies else None
    if trading_enabled and rg_strategy and rg_strategy.uses_risk_gate:
        try:
            from spirit.indicators.decision_engine.engine.risk_gate import RiskGate
            if args.mode in ('paper', 'live') and order_executor is not None:
                equity = order_executor.equity
            else:
                equity = float(os.getenv('ACCOUNT_EQUITY', '10000'))
            risk_gate_kwargs = {'account_equity': equity}
            if is_spine and first_strategy is not None:
                budget_cfg = first_strategy.risk_budget_config
                if 'max_portfolio_exposure_pct' in budget_cfg:
                    risk_gate_kwargs['max_portfolio_exposure_pct'] = float(budget_cfg['max_portfolio_exposure_pct'])
            risk_gate = RiskGate(**risk_gate_kwargs)
            logger.info(f"[RISK_GATE] Initialized with equity=${equity:.0f}")
        except (ImportError, ValueError, OSError) as e:
            logger.debug(f"RiskGate init skipped: {e}")

    # Crash recovery: sync restored state per pair
    for pair in pairs:
        ctx = context_manager.get(pair)
        if isinstance(trade_state_manager, MultiStrategyTradeStateManager):
            # Multi-strategy: restore into the appropriate (pair, strategy) slot
            if ctx.open_trade is not None:
                strat_name = getattr(ctx.open_trade, 'strategy_name', '') or ''
                tsm = trade_state_manager.get(pair, strat_name)
                tsm.open_trade = ctx.open_trade
                logger.info(f"[{pair}:{strat_name}] Restored open trade into MultiStrategyTSM")
        else:
            tsm = trade_state_manager.get(pair)
            if ctx.open_trade is not None:
                tsm.open_trade = ctx.open_trade
                logger.info(f"[{pair}] Restored open trade into TradeStateManager")
        if ctx.equity > 0 and order_executor and hasattr(order_executor, 'equity'):
            order_executor.equity = ctx.equity
            logger.info(f"[{pair}] Restored equity: ${ctx.equity:.2f}")
            if risk_gate:
                risk_gate.update_equity(ctx.equity)

    # Primary interval and monitoring intervals
    interval = requirements.signal_interval if requirements is not None else int(KRAKEN_OHLC_INTERVAL)
    monitoring_intervals = set(requirements.monitoring_intervals) if requirements else set()

    # Web dashboard helpers
    web_inc = web_sig = web_dec = None
    if update_state is not None:
        init_equity = order_executor.equity if (order_executor and hasattr(order_executor, 'equity')) else float(os.getenv('ACCOUNT_EQUITY', '10000'))
        update_state(mode=args.mode, equity=init_equity)
        from spirit.web import increment_candles as web_inc, record_signal as web_sig, record_decision as web_dec

    # ---------------------------------------------------------------
    # Create orchestrator
    # ---------------------------------------------------------------

    orch = SpiritOrchestrator(
        context_manager=context_manager,
        strategies=strategies,
        trade_state_manager=trade_state_manager,
        order_executor=order_executor,
        risk_gate=risk_gate,
        mode=args.mode,
        interval=interval,
        monitoring_intervals=monitoring_intervals,
        pairs=pairs,
        update_state=update_state,
        web_inc=web_inc,
        web_sig=web_sig,
        web_dec=web_dec,
        registry=registry,
    )

    # ---------------------------------------------------------------
    # Select data source
    # ---------------------------------------------------------------

    # Build full intervals list
    all_intervals = {interval}
    if requirements is not None:
        for mi in requirements.all_intervals:
            all_intervals.add(mi)
    try:
        from spirit.config import OHLC_INTERVALS as _OHLC_INTERVALS
        for x in _OHLC_INTERVALS:
            all_intervals.add(int(x))
    except Exception:
        pass
    intervals_list = sorted(all_intervals)

    multi_pair = len(pairs) > 1

    if args.data_source == 'kraken':
        if multi_pair:
            from spirit.utils.multi_pair_data_source import MultiPairLiveDataSource
            logger.info(f"[MultiPair Live] pairs={pairs} intervals={intervals_list} primary={interval}")
            data_source = MultiPairLiveDataSource(
                pairs=pairs,
                intervals=intervals_list,
                primary_interval=interval,
                buffer_size=args.buffer_size,
            )
        else:
            # Single-pair: use existing data source hierarchy
            logger.info(f"Starting live with primary interval={interval}")
            force_single = os.environ.get('FORCE_SINGLE_INTERVAL', '').lower() in ['1', 'true', 'yes']
            use_multi_interval = (
                (len(intervals_list) > 1 or args.multi_interval)
                and not force_single
                and MultiIntervalLiveDataSource is not None
            )
            if use_multi_interval:
                logger.info(f"[MultiInterval Live] intervals={intervals_list} primary={interval}")
                data_source = MultiIntervalLiveDataSource(
                    intervals=intervals_list, primary_interval=interval,
                    buffer_size=args.buffer_size, pair=pairs[0],
                )
            else:
                data_source = LiveDataSource(
                    buffer_size=args.buffer_size, interval=interval, pair=pairs[0],
                )
    else:
        if multi_pair:
            from spirit.utils.multi_pair_data_source import MultiPairCsvDataSource
            logger.info(f"[MultiPair CSV] pairs={pairs} primary={interval}")
            data_source = MultiPairCsvDataSource(
                csv_path=args.csv_path,
                pairs=pairs,
                primary_interval=interval,
                intervals=intervals_list if len(intervals_list) > 1 else None,
                window_size=args.buffer_size,
            )
        else:
            logger.info(f"Starting CSV with default primary interval={interval}")
            use_multi_csv = False
            try:
                _hdr = pd.read_csv(args.csv_path, nrows=0).columns
                use_multi_csv = ('interval' in _hdr) and (CsvMultiIntervalDataSource is not None)
            except Exception:
                pass
            if use_multi_csv:
                ints = pd.read_csv(args.csv_path, usecols=['interval']).dropna()['interval'].astype(int).unique().tolist()
                file_intervals = sorted(set(int(x) for x in ints))
                if interval not in file_intervals:
                    interval = int(file_intervals[0])
                logger.info(f"[MultiInterval CSV] intervals={file_intervals} primary={interval}")
                data_source = CsvMultiIntervalDataSource(
                    args.csv_path, primary_interval=interval,
                    intervals=file_intervals, window_size=args.buffer_size,
                )
            else:
                data_source = CsvDataSource(args.csv_path, interval=interval, pair=pairs[0])

    orch.data_source = data_source

    # ---------------------------------------------------------------
    # Warmup
    # ---------------------------------------------------------------

    warmup_candles = requirements.warmup_candles if requirements is not None else 720
    target = max(warmup_candles, int(args.buffer_size))

    if multi_pair:
        # Multi-pair warmup: wait for all pairs on primary interval
        if args.data_source == 'kraken':
            logger.info(f"Waiting for all pairs to warm up ({target} candles on {interval}m)...")
            data_source.wait_for_all(interval, min_size=target - 1, timeout=300)
            for pair in pairs:
                window_df = data_source.get_window(pair, interval, target)
                context_manager.get(pair).warmup(window_df.records, interval=interval)
                logger.info(f"  [{pair}] warmup complete ({len(window_df.records)} candles)")
        else:
            # CSV multi-pair
            for pair in pairs:
                window_df = data_source.get_window(pair, interval, target)
                context_manager.get(pair).warmup(window_df.records, interval=interval)
                logger.info(f"  [{pair}] warmup complete ({len(window_df.records)} candles)")
    else:
        # Single-pair warmup (preserved from before)
        pair = pairs[0]
        if args.data_source == 'kraken':
            logger.info(f"Waiting for live buffer to reach {target} on primary={interval}m...")
            if hasattr(data_source, 'buffer'):
                data_source.buffer.wait_for_buffer(min_size=target - 1)
                window_df = data_source.get_window(target)
            else:
                data_source.wait_for_data(interval, min_size=target - 1, timeout=180)
                window_df = data_source.get_window(interval, target)
        else:
            if hasattr(data_source, 'df_by_interval'):
                try:
                    from spirit.data_types import OHLCRecord, OHLCData
                    df_primary = data_source.df_by_interval.get(int(interval))
                    if df_primary is None or df_primary.empty:
                        raise RuntimeError(f"No data for primary interval={interval} in CSV.")
                    slice_df = df_primary.head(target)
                    records = [
                        OHLCRecord.from_raw(
                            pair=row.get("pair", pair), interval=int(interval),
                            dt_raw=row["datetime"], open_=row["open"],
                            high=row["high"], low=row["low"],
                            close=row["close"], vwap=row.get("vwap", None),
                            volume=row["volume"], count=row.get("count", None),
                            timestamp=row.get("timestamp", None),
                        )
                        for _, row in slice_df.iterrows()
                    ]
                    window_df = OHLCData(records=records)
                except Exception as e:
                    logger.error(f"CSV warmup build failed: {e}; falling back to last window.")
                    window_df = data_source.get_window(interval, target)
            elif hasattr(data_source, 'buffers'):
                try:
                    data_source.wait_for_data(interval, min_size=target - 1, timeout=1)
                except Exception:
                    pass
                window_df = data_source.get_window(interval, target)
            else:
                window_df = data_source.get_window(target)

        context_manager.get(pair).warmup(window_df.records, interval=interval)

    # Set equity on all contexts
    if order_executor and hasattr(order_executor, 'equity'):
        for pair in pairs:
            context_manager.get(pair).set_equity(order_executor.equity)
    data_source.warmup_complete = True
    logger.info("Warmup complete.")

    # Green light check per pair
    for pair in pairs:
        strategy = strategies.get(pair)
        if strategy is None:
            continue
        ready, issues = strategy.validate_readiness()
        if ready:
            logger.info(
                f"[{pair}][GREEN LIGHT] Strategy {type(strategy).__name__} ready. "
                f"signal={interval}m "
                f"monitoring={list(monitoring_intervals)} "
                f"risk_gate={'ON' if risk_gate else 'OFF'}"
            )
        else:
            logger.warning(
                f"[{pair}][YELLOW LIGHT] Strategy {type(strategy).__name__} has issues "
                f"(proceeding anyway): {issues}"
            )

    # ---------------------------------------------------------------
    # Register callbacks
    # ---------------------------------------------------------------

    is_csv = (args.data_source == 'csv')
    if multi_pair:
        # Multi-pair data source emits (pair, interval, window)
        data_source.register_callback(
            lambda p, iv, wdf: orch.on_pair_candle(p, iv, wdf, is_csv=is_csv)
        )
    elif hasattr(data_source, 'buffers'):
        # Single-pair multi-interval
        data_source.register_callback(
            lambda iv, wdf: orch.on_interval_window(iv, wdf, is_csv=is_csv)
        )
    else:
        # Single-pair single-interval
        data_source.register_callback(orch.on_new_candle)

    # ---------------------------------------------------------------
    # CSV replay
    # ---------------------------------------------------------------

    if is_csv:
        import threading

        def _csv_replay_loop():
            try:
                steps = 0
                for _ in iter(data_source):
                    steps += 1
                    if steps % 1000 == 0:
                        logger.info(f"[CSV Replay] progress steps={steps}")
            except Exception as e:
                logger.error(f"[CSV Replay] error: {e}")

        csv_thread = threading.Thread(target=_csv_replay_loop, name="CsvReplayThread", daemon=True)
        orch.csv_thread = csv_thread
        csv_thread.start()
        logger.info("[MAIN] Waiting for CSV replay to finish...")
        try:
            csv_thread.join()
        except Exception:
            pass
        logger.info("[MAIN] CSV replay finished; initiating graceful shutdown...")
        orch.graceful_shutdown(no_pause=args.no_pause, is_csv=True)
        return

    # Early exit after warmup
    if args.exit_after_warmup:
        logger.info("--exit-after-warmup set; initiating graceful shutdown...")
        raise KeyboardInterrupt

    # Optional timed shutdown
    if args.duration:
        import threading, signal as _signal

        def _shutdown_after_delay():
            try:
                time.sleep(int(args.duration))
                logger.info(f"Duration {args.duration}s reached; requesting shutdown...")
                import os as _os
                _os.kill(_os.getpid(), _signal.SIGINT)
            except Exception:
                pass
        threading.Thread(target=_shutdown_after_delay, name="ShutdownTimer", daemon=True).start()

    # Idle until interrupted
    import signal
    try:
        signal.pause()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received. Exiting...")
    finally:
        orch.graceful_shutdown(no_pause=args.no_pause)


if __name__ == "__main__":
    main()
