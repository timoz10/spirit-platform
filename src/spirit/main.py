"""
spirit_main.py

Main orchestration script for kraken-bot project.
Coordinates data sources (live/CSV, single/multi-pair, single/multi-interval),
warmup/backfill, feature engineering, strategy evaluation, and graceful shutdown.

Multi-pair support (Phase 1): Each pair gets its own SpiritContext, strategy
instance, and TradeStateManager. Equity is shared via the order executor.
"""

import faulthandler; faulthandler.enable()
import json
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
from spirit.pending_order_manager import PendingOrderManager, PendingLimitOrder

# Risk gate decision audit trail (lazy init — never blocks startup)
_risk_gate_writer = None

def _get_risk_gate_writer():
    """Lazy-load risk_gate_writer to avoid import errors on branches without it.

    Table must be pre-created via the SQL script (same pattern as strategy_performance).
    """
    global _risk_gate_writer
    if _risk_gate_writer is None:
        try:
            from spirit.indicators.decision_engine.engine import risk_gate_writer
            _risk_gate_writer = risk_gate_writer
        except Exception as e:
            get_logger("spirit_main").debug(f"risk_gate_writer unavailable: {e}")
            _risk_gate_writer = False  # sentinel: tried and failed
    return _risk_gate_writer if _risk_gate_writer is not False else None


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
                 registry=None, event_bus=None, readiness_gate=None,
                 run_id='live'):
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
        self.event_bus = event_bus                   # PgEventBus or None
        self.readiness_gate = readiness_gate         # DataReadinessGate or None
        self.run_id = run_id                         # 'live' or UUID for replay
        self.data_source = None  # set after data source creation
        self.csv_thread = None
        self._instance = get_config('SPIRIT_INSTANCE', 'prod')

        # Pending limit order manager
        self.pending_orders = PendingOrderManager()

        # Limit order config
        from spirit.config import LIMIT_ORDER_MODE, LIMIT_ORDER_TTL_MINUTES
        self.limit_order_mode = LIMIT_ORDER_MODE
        self.limit_order_ttl = LIMIT_ORDER_TTL_MINUTES

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

    def _heartbeat_tick(self, pair, ctx):
        """Write an 'ok' heartbeat on a periodic cadence.

        Called from on_pair_candle so that every routing path (legacy and
        Spine registry) keeps spirit:{instance} fresh in daemon_heartbeats.
        """
        if self.run_id != 'live':
            return
        if ctx.health['candles_processed'] % 10 != 0:
            return
        try:
            from spirit.pipeline.daemon_health import record_heartbeat
            metadata = {
                'pairs_active': len(self.pairs),
                'pair': pair,
                'candles': ctx.health['candles_processed'],
            }
            # Expose composite threshold calibrator health
            for strat in self.strategies.values():
                if hasattr(strat, '_composite_calibrator') and strat._composite_calibrator:
                    cal = strat._composite_calibrator
                    metadata['composite_cal_age_h'] = round(
                        cal.hours_since_calibration(), 1)
                    metadata['composite_cal_healthy'] = cal.health_check().get(
                        'healthy', False)
                    break  # all pairs share the same calibrator
            record_heartbeat(f'spirit:{self._instance}', status='ok', metadata=metadata)
        except Exception:
            pass

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

            # Heartbeat on signal-interval candles only (avoid 1m spam)
            if iv == int(self.interval):
                self._heartbeat_tick(pair, ctx)

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

        # Periodic state save — before any early returns so pairs with pending
        # limits still persist startup_config and paper_equity (#193)
        if ctx.health['candles_processed'] % 10 == 0:
            ctx.save_state()

        # Dedup: skip 60m evaluation if limit is pending — but still check limit lifecycle
        if self.pending_orders.has_pending(pair):
            # Build candle dict from latest 60m row for fill/expiry checks
            try:
                from spirit.utils.db_utils import set_active_pair, get_spirit_temp_ti
                set_active_pair(pair)
                _df = get_spirit_temp_ti(db_path=None)
                if _df is not None and not _df.empty:
                    _row = _df.iloc[-1]
                    _candle = _row.to_dict() if hasattr(_row, 'to_dict') else dict(_row)
                    self._check_pending_limit(pair, _candle)
            except Exception as e:
                self._cb_logger.debug(f"[{pair}] 60m pending check error: {e}")
            if self.pending_orders.has_pending(pair):
                self._cb_logger.info(f"[{pair}][EVAL] SKIP — predictive limit pending")
                return
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

            # Pipeline readiness gate: wait for upstream D-Limit data
            if self.readiness_gate is not None and self.readiness_gate.enabled:
                candle_dt_iso = ctx.health.get('last_candle_time')
                if candle_dt_iso:
                    ready = self.readiness_gate.wait_for_dlimit(
                        pair, candle_dt_iso, interval=self.interval
                    )
                    if not ready:
                        self._cb_logger.warning(
                            f"[{pair}][PIPELINE] D-Limit not ready for {candle_dt_iso}, "
                            f"evaluating with possibly stale data"
                        )

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
                        # Record decision to audit trail
                        try:
                            writer = _get_risk_gate_writer()
                            if writer:
                                writer.record_decision(
                                    signal, risk_decision, pair,
                                    getattr(signal, 'strategy_name', None) or 'zone_bounce',
                                    source=self.mode,
                                    run_id=self.run_id,
                                )
                        except Exception:
                            pass
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
                            # Attach entry_context to trade_record for PG persistence (#175)
                            _atc = getattr(strategy, '_active_trade_context', {}).get(pair, {})
                            if _atc.get('entry_context'):
                                trade_record.entry_context = _atc['entry_context']

            if entry_flag and trade_record is not None:
                # Route limit orders through pending system, market orders immediate
                order_type = getattr(trade_record, 'order_type', None) or 'market'
                if order_type == 'limit' and self.mode in ('live', 'paper'):
                    self._place_pending_limit(pair, trade_record, details)
                else:
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
                        # Record decision to audit trail
                        try:
                            writer = _get_risk_gate_writer()
                            if writer:
                                writer.record_decision(
                                    signal, risk_decision, pair,
                                    strategy_name or 'unknown',
                                    source=self.mode,
                                    run_id=self.run_id,
                                )
                        except Exception:
                            pass
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
                            # Attach entry_context to trade_record for PG persistence (#175)
                            _atc = getattr(strategy, '_active_trade_context', {}).get(pair, {})
                            if _atc.get('entry_context'):
                                trade_record.entry_context = _atc['entry_context']

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

        # Capture entry context before sell clears open_trade
        entry_price = float(getattr(tsm.open_trade, 'entry_price', 0) or 0)
        entry_datetime = getattr(tsm.open_trade, 'entry_datetime', None)

        for attr in _ENTRY_COPY_ATTRS:
            setattr(trade_record, attr, getattr(tsm.open_trade, attr, None))
        process_trade_signals(
            "sell", trade_record, self.mode, tsm,
            order_executor=self.order_executor, logger=self._cb_logger,
        )

        # Notify strategy of exit
        exit_price = float(getattr(trade_record, 'exit_price', 0) or 0)
        exit_reason = getattr(trade_record, 'exit_reason', '') or ''
        exit_dt = getattr(trade_record, 'exit_datetime', None)
        net_pnl_pct = getattr(trade_record, 'pnl_pct', None)  # From executor (net, after fees)
        strategy = self.strategies.get(pair)
        if strategy and hasattr(strategy, 'on_exit_completed'):
            try:
                strategy.on_exit_completed(pair, exit_reason, exit_price, entry_price,
                                           exit_dt=exit_dt, net_pnl_pct=net_pnl_pct)
            except Exception as e:
                self._cb_logger.debug(f"[{pair}:{strategy_name}] on_exit_completed error: {e}")

        # Record outcome to risk gate audit trail
        try:
            writer = _get_risk_gate_writer()
            if writer and entry_price > 0 and entry_datetime:
                pnl_pct = net_pnl_pct if net_pnl_pct is not None else ((exit_price - entry_price) / entry_price * 100.0)
                writer.update_outcome(
                    pair=pair,
                    entry_timestamp=str(entry_datetime),
                    strategy_name=strategy_name or 'unknown',
                    is_win=pnl_pct > 0,
                    pnl_pct=round(pnl_pct, 4),
                    exit_reason=exit_reason,
                    run_id=self.run_id,
                )
        except Exception:
            pass

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
        # --- Monitoring warmup gate (#48) ---
        if self.data_source and hasattr(self.data_source, 'is_monitoring_warm'):
            if not self.data_source.is_monitoring_warm(pair, interval_val):
                self._cb_logger.debug(
                    f"[{pair}:{strategy_name}][MONITORING] Skipping — {interval_val}m buffer not warm"
                )
                return

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
        """Route monitoring-interval candles to exit monitoring, pending limit check,
        or entry scanning.

        Three-state routing:
        1. Trade open     -> exit monitoring (unchanged)
        2. Pending limit  -> _check_pending_limit() (check fill/expiry)
        3. No trade       -> entry scan -> _process_entry() (may place limit)
        """
        # --- Monitoring warmup gate (#48) ---
        # Skip monitoring until the buffer for this interval has warmed up
        # (200+ candles), preventing stale/partial prices from triggering
        # bogus ATR stops on the first cycle after restart.
        if self.data_source and hasattr(self.data_source, 'is_monitoring_warm'):
            if not self.data_source.is_monitoring_warm(pair, interval_val):
                if not getattr(self, '_warmup_warned', None):
                    self._warmup_warned = set()
                key = (pair, int(interval_val))
                if key not in self._warmup_warned:
                    self._cb_logger.warning(
                        f"[{pair}][MONITORING] Skipping — {interval_val}m buffer not warm yet"
                    )
                    self._warmup_warned.add(key)
                else:
                    self._cb_logger.debug(
                        f"[{pair}][MONITORING] Still waiting for {interval_val}m buffer warmup"
                    )
                return
        else:
            # Log once when warmup gate clears for this (pair, interval)
            if getattr(self, '_warmup_warned', None):
                key = (pair, int(interval_val))
                if key in self._warmup_warned:
                    self._cb_logger.info(
                        f"[{pair}][MONITORING] {interval_val}m buffer warm — monitoring active"
                    )
                    self._warmup_warned.discard(key)

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
                # State 1: exit monitoring (unchanged)
                result = strategy.on_monitoring_tick(
                    pair, int(interval_val), candle_dict, tsm.open_trade
                )
                if result and result.get('exit'):
                    details = result.get('details', {})
                    trade_record = self._build_trade_record(details)
                    if trade_record is not None and tsm.open_trade is not None:
                        self._process_exit(pair, trade_record)
            elif self.pending_orders.has_pending(pair):
                # State 2: check pending limit order
                self._check_pending_limit(pair, candle_dict)
            else:
                # State 3: entry scan on sub-signal ticks
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
        """Save state for all pairs and clean up. Cancel all pending limit orders."""
        # Cancel all pending limit orders before shutdown
        for pair, pending in list(self.pending_orders.all_pending().items()):
            self._cancel_pending_limit(pair, pending, reason='shutdown')

        # Stop pipeline event bus
        if self.event_bus is not None:
            try:
                self.event_bus.stop()
                self.logger.info("Pipeline event bus stopped")
            except Exception:
                pass

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
                # Record decision to audit trail
                try:
                    writer = _get_risk_gate_writer()
                    if writer:
                        writer.record_decision(
                            signal, risk_decision, pair,
                            getattr(signal, 'strategy_name', None) or 'zone_bounce',
                            source=self.mode,
                            run_id=self.run_id,
                        )
                except Exception:
                    pass
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
                    # Attach entry_context to trade_record for PG persistence (#175)
                    _atc = getattr(strategy, '_active_trade_context', {}).get(pair, {})
                    if _atc.get('entry_context'):
                        trade_record.entry_context = _atc['entry_context']

        if entry_flag and trade_record is not None:
            # Branch on order type: limit orders get placed as pending,
            # market orders execute immediately (existing path)
            order_type = getattr(trade_record, 'order_type', None) or 'market'
            if order_type == 'limit' and self.mode in ('live', 'paper'):
                self._place_pending_limit(pair, trade_record, details)
            else:
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
        strategy = self.strategies.get(pair)

        # Capture entry context before sell clears open_trade
        entry_price = float(getattr(tsm.open_trade, 'entry_price', 0) or 0)
        entry_datetime = getattr(tsm.open_trade, 'entry_datetime', None)
        entry_strategy_name = getattr(tsm.open_trade, 'strategy_name', None) or 'zone_bounce'

        # Copy entry context to exit record
        for attr in _ENTRY_COPY_ATTRS:
            setattr(trade_record, attr, getattr(tsm.open_trade, attr, None))
        process_trade_signals(
            "sell", trade_record, self.mode, tsm,
            order_executor=self.order_executor, logger=self._cb_logger,
        )

        # Notify strategy of exit (for cooldown tracking, state cleanup)
        exit_price = float(getattr(trade_record, 'exit_price', 0) or 0)
        exit_reason = getattr(trade_record, 'exit_reason', '') or ''
        exit_dt = getattr(trade_record, 'exit_datetime', None)
        net_pnl_pct = getattr(trade_record, 'pnl_pct', None)  # From executor (net, after fees)
        if strategy and hasattr(strategy, 'on_exit_completed'):
            try:
                strategy.on_exit_completed(pair, exit_reason, exit_price, entry_price,
                                           exit_dt=exit_dt, net_pnl_pct=net_pnl_pct)
            except Exception as e:
                self._cb_logger.debug(f"[{pair}] on_exit_completed error: {e}")

        # Record outcome to risk gate audit trail
        try:
            writer = _get_risk_gate_writer()
            if writer and entry_price > 0 and entry_datetime:
                pnl_pct = net_pnl_pct if net_pnl_pct is not None else ((exit_price - entry_price) / entry_price * 100.0)
                writer.update_outcome(
                    pair=pair,
                    entry_timestamp=str(entry_datetime),
                    strategy_name=entry_strategy_name,
                    is_win=pnl_pct > 0,
                    pnl_pct=round(pnl_pct, 4),
                    exit_reason=exit_reason,
                    run_id=self.run_id,
                )
        except Exception:
            pass

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

    def _place_pending_limit(self, pair, trade_record, details):
        """Place a limit order on the exchange and register as pending.

        Called from _process_entry() when order_type == 'limit'.
        Sets predictive-specific TTL (bar-based) when entry_path is 'predictive'.
        """
        if self.order_executor is None:
            self._cb_logger.warning(f"[{pair}] Cannot place limit: no order executor")
            return

        limit_price = getattr(trade_record, 'limit_price', None)
        if limit_price is None:
            self._cb_logger.warning(f"[{pair}] Cannot place limit: no limit_price")
            return

        try:
            result = self.order_executor.place_limit_order(trade_record, limit_price)
            txids = result.get('txid') or []
            if not txids:
                self._cb_logger.error(f"[{pair}] Limit order returned no txid")
                return

            txid = txids[0]

            # Build signal context for fill handoff
            signal_context = {
                'signal': details.get('signal'),
                'trade_record_dict': trade_record.__dict__.copy(),
            }

            # Determine TTL and source based on entry path
            signal = details.get('signal')
            entry_path = (signal.row_data or {}).get('entry_path', '') if signal else ''
            is_predictive = entry_path == 'predictive'

            if is_predictive:
                from spirit.config import PREDICTIVE_TTL_BARS
                ttl_minutes = PREDICTIVE_TTL_BARS * 60  # bars at 60m = hours -> minutes
                ttl_bars = PREDICTIVE_TTL_BARS * 60     # bar counter in 1m ticks
                source = 'predictive'
            else:
                ttl_minutes = self.limit_order_ttl
                ttl_bars = ttl_minutes  # 1 bar = 1 minute; ensures expiry works in replay mode
                source = 'confirmed'

            pending = PendingLimitOrder(
                pair=pair,
                txid=txid,
                limit_price=limit_price,
                zone_id=signal and getattr(signal, 'zone_id', None),
                ttl_minutes=ttl_minutes,
                ttl_bars=ttl_bars,
                buy_amount_usd=getattr(trade_record, 'buy_amount', 0) or 0,
                volume=getattr(trade_record, 'buy_amount', 0) or 0,
                source=source,
                signal_context=signal_context,
            )
            self.pending_orders.place(pending)
            self._cb_logger.info(
                f"[{pair}][LIMIT_PLACED] txid={txid} limit={limit_price:.4f} "
                f"signal_price={getattr(trade_record, 'entry_price', 'N/A')} "
                f"ttl={ttl_minutes}m source={source} entry_path={entry_path}"
            )

        except Exception as e:
            self._cb_logger.error(f"[{pair}] Failed to place limit order: {e}")

    def _check_pending_limit(self, pair, candle):
        """Check a pending limit order for fill or expiry.

        Called every 1m tick when a limit order is pending for this pair.
        Increments bar counter for bar-based TTL expiry.

        IMPORTANT: Fill check runs BEFORE expiry check (#181) so that a candle
        arriving on the same tick as TTL expiry still gets a chance to fill.
        """
        pending = self.pending_orders.get_pending(pair)
        if pending is None:
            return

        # Increment bar counter (tracks 1m ticks — used for TTL, thesis checks, missed bounce)
        pending.tick_bar()

        # --- Fill check FIRST (before expiry) ---
        # A candle that arrives on the same tick as TTL should still fill (#181)
        candle_low = float(candle.get('low', 0)) if candle else 0
        if self.mode == 'paper':
            fill_status = self.order_executor.check_limit_fill(pending.txid, candle)
        else:
            fill_status = self.order_executor.check_order_status(pending.txid)

        status = fill_status.get('status', 'unknown')

        if status == 'closed':
            # Filled — transition to open trade
            self._cb_logger.info(
                f"[{pair}][LIMIT_FILL_CHECK] FILLED age={pending.age_minutes:.1f}m "
                f"candle_low={candle_low} limit={pending.limit_price}"
            )
            self._on_limit_filled(pair, pending, fill_status)
            return
        elif status in ('canceled', 'expired'):
            # Exchange cancelled the order
            self.pending_orders.remove(pair)
            self._cb_logger.info(
                f"[{pair}][LIMIT_CANCEL] Exchange {status}: txid={pending.txid}"
            )
            return

        # --- Missed bounce cancel (price moved too far above zone) ---
        candle_close = float(candle.get('close', 0)) if candle else 0
        missed_bounce_pct = float(get_config('PREDICTIVE_MISSED_BOUNCE_PCT', '2.0'))
        if candle_close > 0 and pending.limit_price > 0:
            gap_pct = (candle_close - pending.limit_price) / pending.limit_price * 100
            if gap_pct > missed_bounce_pct:
                self._cb_logger.info(
                    f"[{pair}][LIMIT_MISSED_BOUNCE] close={candle_close:.2f} "
                    f"limit={pending.limit_price:.2f} gap={gap_pct:.1f}% > {missed_bounce_pct}%"
                )
                strategy = self.strategies.get(pair)
                if strategy and hasattr(strategy, 'on_limit_expired'):
                    try:
                        strategy.on_limit_expired(pair, pending.zone_id)
                    except Exception as e:
                        self._cb_logger.debug(f"[{pair}] on_limit_expired error: {e}")
                self._cancel_pending_limit(pair, pending, reason='missed_bounce')
                return

        # --- Thesis health check (every 15 ticks while pending) ---
        if pending.bars_elapsed > 0 and pending.bars_elapsed % 15 == 0:
            strategy = self.strategies.get(pair)
            if strategy and hasattr(strategy, 'check_pending_thesis_health'):
                try:
                    health = strategy.check_pending_thesis_health(
                        pair, candle, pending.bars_elapsed)
                    if health and health.get('action') == 'EXIT_EARLY':
                        self._cb_logger.info(
                            f"[{pair}][LIMIT_THESIS_DEGRADED] {health.get('reason', '')} "
                            f"health={health.get('health_score', '?')} bar={pending.bars_elapsed}"
                        )
                        if hasattr(strategy, 'on_limit_expired'):
                            try:
                                strategy.on_limit_expired(pair, pending.zone_id)
                            except Exception:
                                pass
                        self._cancel_pending_limit(pair, pending, reason='thesis_degraded')
                        return
                except Exception as e:
                    self._cb_logger.debug(f"[{pair}] check_pending_thesis_health error: {e}")

        # --- Expiry check (after fill check) ---
        if pending.is_expired:
            self._cb_logger.info(
                f"[{pair}][LIMIT_EXPIRE] age={pending.age_minutes:.1f}m "
                f"ttl={pending.ttl_minutes}m candle_low={candle_low} "
                f"limit={pending.limit_price} gap={candle_low - pending.limit_price:.4f}"
            )
            # Notify strategy for cooldown tracking (predictive entries)
            strategy = self.strategies.get(pair)
            if strategy and hasattr(strategy, 'on_limit_expired'):
                try:
                    strategy.on_limit_expired(pair, pending.zone_id)
                except Exception as e:
                    self._cb_logger.debug(f"[{pair}] on_limit_expired error: {e}")
            self._cancel_pending_limit(pair, pending, reason='expired')
            return

        # Periodic diagnostic: log every 10 minutes while pending
        if int(pending.age_minutes) % 10 == 0 and int(pending.age_minutes) > 0:
            self._cb_logger.debug(
                f"[{pair}][LIMIT_PENDING] age={pending.age_minutes:.0f}m "
                f"candle_low={candle_low} limit={pending.limit_price} "
                f"gap={candle_low - pending.limit_price:.4f}"
            )

    def _on_limit_filled(self, pair, pending, fill_status):
        """Handle a filled limit order — transition to open trade.

        Rebuilds TradeRecord from pending signal_context, finalizes fill
        with order executor, and opens trade in TSM.
        """
        ctx = self.context_manager.get(pair)
        tsm = self.trade_state_manager.get(pair)
        strategy = self.strategies.get(pair)

        # Rebuild TradeRecord from signal context
        tr_dict = pending.signal_context.get('trade_record_dict', {})
        trade_record = self._build_trade_record(tr_dict)
        pre_finalize_price = getattr(trade_record, 'entry_price', None) if trade_record else None
        if trade_record is None:
            self._cb_logger.warning(
                f"[{pair}][LIMIT_FILLED] _build_trade_record returned None, "
                f"using fallback with limit_price={pending.limit_price}"
            )
            trade_record = TradeRecord(
                entry_price=pending.limit_price,
                symbol=pair,
                strategy_name='zone_bounce',
                mode=self.mode,
            )

        # Finalize fill with order executor (updates equity, records to PG)
        self.order_executor.finalize_limit_fill(pending.txid, trade_record)

        self._cb_logger.info(
            f"[{pair}][LIMIT_FILLED] txid={pending.txid} "
            f"pre_finalize={pre_finalize_price} post_finalize={trade_record.entry_price} "
            f"limit={pending.limit_price} zone_id={pending.zone_id} "
            f"age={pending.age_minutes:.1f}m"
        )

        # Open trade in TSM (same as market order path)
        tsm.open(trade_record)
        if tsm.open_trade is not None:
            ctx.set_open_trade(tsm.open_trade)

        # Capture entry context for dynamic exit
        signal = pending.signal_context.get('signal')
        if strategy and hasattr(strategy, 'on_entry_confirmed') and signal:
            # Build a minimal RiskDecision from the signal context
            try:
                from spirit.trade_signal import RiskDecision
                risk_decision = RiskDecision(
                    trade=True,
                    position_size_usd=getattr(trade_record, 'buy_amount', 0) or 0,
                )
                strategy.on_entry_confirmed(pair, signal, risk_decision)
                # Attach entry_context to open trade for PG persistence (#175)
                _atc = getattr(strategy, '_active_trade_context', {}).get(pair, {})
                if _atc.get('entry_context') and tsm.open_trade is not None:
                    tsm.open_trade.entry_context = _atc['entry_context']
            except Exception as e:
                self._cb_logger.debug(f"[{pair}] on_entry_confirmed after limit fill: {e}")

        if self._update_state and tsm.open_trade is not None:
            ot = tsm.open_trade
            self._update_state(open_trade={
                'pair': pair,
                'entry_price': getattr(ot, 'entry_price', None),
                'entry_datetime': str(getattr(ot, 'entry_datetime', '')),
                'buy_amount': getattr(ot, 'buy_amount', None),
            })

        # Clear pending state
        self.pending_orders.remove(pair)

    def _cancel_pending_limit(self, pair, pending, reason='manual'):
        """Cancel a pending limit order on the exchange and clean up state."""
        if self.order_executor is not None:
            self.order_executor.cancel_order(pending.txid)

        self.pending_orders.remove(pair)
        self._cb_logger.info(
            f"[{pair}][LIMIT_CANCEL] reason={reason} txid={pending.txid} "
            f"age={pending.age_minutes:.1f}m"
        )

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

    # Pre-flight validation (skip Kraken keys for replay mode — no API needed)
    import sys
    is_replay_mode = '--replay' in sys.argv
    from spirit.utils.preflight import run_preflight
    preflight = run_preflight(skip_kraken=is_replay_mode)
    if not preflight.passed:
        logger.error("Pre-flight checks FAILED. Spirit cannot start.")
        for f in preflight.fatal_failures:
            logger.error(f"  {f.name}: {f.message}")
        raise SystemExit(1)
    logger.info("Pre-flight checks passed.")

    # Log version and active configuration for prod traceability (#25, #249)
    from spirit import __version__
    import subprocess
    try:
        git_hash = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        git_hash = 'unknown'
    instance = get_config('SPIRIT_INSTANCE', 'prod')
    strategy_name = get_config('SPIRIT_STRATEGY', 'none')
    mode_label = 'replay' if '--replay' in sys.argv else get_config('SPIRIT_MODE', 'paper')
    logger.info(
        f"Spirit v={__version__} ({git_hash}) instance={instance} strategy={strategy_name} mode={mode_label}"
    )

    # Stamp version into spirit_state for PG-queryable deployment verification (#249)
    # Keys are instance-scoped to avoid collisions in multi-instance deployments (#225)
    if mode_label != 'replay':
        try:
            from spirit.utils.db_connection import execute_query
            from datetime import datetime, timezone
            started_at = datetime.now(timezone.utc).isoformat()
            for key, value in [
                (f'version:{instance}:arch', __version__),
                (f'version:{instance}:git_sha', git_hash),
                (f'version:{instance}:started_at', started_at),
            ]:
                execute_query("""
                    INSERT INTO spirit_state (key, value, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """, (key, json.dumps(value)), fetch='none')
            logger.info(f"[VERSION] Stamped v={__version__} sha={git_hash} instance={instance} to spirit_state")
        except Exception as e:
            logger.warning(f"[VERSION] Failed to stamp version to spirit_state: {e}")

    # Record startup heartbeat for daemon health monitoring (#225)
    if mode_label != 'replay':
        try:
            from spirit.pipeline.daemon_health import record_heartbeat
            ok = record_heartbeat(f'spirit:{instance}', status='starting', metadata={
                'version': __version__,
                'git_sha': git_hash,
                'strategy': strategy_name,
                'mode': mode_label,
            })
            if ok:
                logger.info(f"[HEARTBEAT] Registered spirit:{instance} (starting)")
        except Exception as e:
            logger.debug(f"[HEARTBEAT] Startup heartbeat failed: {e}")

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
    parser.add_argument('--replay', action='store_true', help='PG replay backtest mode')
    parser.add_argument('--start', type=str, default=None, help='Replay start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, default=None, help='Replay end date (YYYY-MM-DD)')
    parser.add_argument('--with-monitoring', action='store_true',
                        help='Include monitoring intervals (1m) in replay for dynamic exit')
    parser.add_argument('--run-tag', type=str, default=None,
                        help='Human-readable label for this replay run (e.g. "baseline")')
    parser.add_argument('--list-runs', action='store_true',
                        help='Print replay run table and exit')
    parser.add_argument('--delete-run', type=str, default=None, metavar='RUN_ID',
                        help='Delete all data for a specific run ID and exit')
    args = parser.parse_args()

    # --list-runs: print run table and exit
    if args.list_runs:
        from spirit.utils.run_manager import list_runs
        runs = list_runs(limit=30)
        if not runs:
            print("No replay runs found.")
        else:
            print(f"{'ID':>8}  {'Tag':<15}  {'Strategy':<15}  {'Pairs':<30}  "
                  f"{'Range':<25}  {'Status':<10}  {'Trades':>6}  {'WR':>6}  {'PF':>6}  {'Net%':>8}")
            print("-" * 160)
            for r in runs:
                rid = r['id'][:8] + '...'
                tag = (r.get('tag') or '-')[:15]
                strat = (r.get('strategy_name') or '-')[:15]
                pairs_str = (r.get('pairs') or '-')[:30]
                date_range = f"{r.get('start_date', '?')} to {r.get('end_date', '?')}"
                status = r.get('status', '?')
                trades = r.get('total_trades') or 0
                wr = f"{float(r['win_rate']):.1%}" if r.get('win_rate') is not None else '-'
                pf = f"{float(r['profit_factor']):.2f}" if r.get('profit_factor') is not None else '-'
                net = f"{float(r['net_pnl_pct']):.2f}" if r.get('net_pnl_pct') is not None else '-'
                print(f"{rid:>8}  {tag:<15}  {strat:<15}  {pairs_str:<30}  "
                      f"{date_range:<25}  {status:<10}  {trades:>6}  {wr:>6}  {pf:>6}  {net:>8}")
        return

    # --delete-run: delete run data and exit
    if args.delete_run:
        from spirit.utils.run_manager import delete_run
        try:
            counts = delete_run(args.delete_run)
            print(f"Deleted run {args.delete_run[:8]}...:")
            for table, n in counts.items():
                print(f"  {table}: {n} rows")
        except ValueError as e:
            print(f"Error: {e}")
        return

    # --replay implies --csv-style flow (non-live) with paper mode
    if args.replay:
        if not args.start or not args.end:
            parser.error("--replay requires --start and --end dates")
        args.data_source = 'replay'
        if args.mode == 'test':
            args.mode = 'paper'
        # Expose replay start for PIT-safe calibration in scorer/regime engine
        os.environ['SPIRIT_REPLAY_START'] = args.start

    # Generate run_id for replay runs (live/paper use 'live')
    from spirit.utils.run_manager import LIVE_RUN_ID
    if args.replay:
        from spirit.utils.run_manager import generate_run_id, register_run
        run_id = generate_run_id()
    else:
        run_id = LIVE_RUN_ID

    # ---------------------------------------------------------------
    # Determine pairs and create per-pair strategy instances
    # ---------------------------------------------------------------

    # Env var override for dev/replay (e.g. SPIRIT_PAIRS="XBTUSD,ETHUSD")
    env_pairs_str = (os.environ.get('SPIRIT_PAIRS', '') or '').strip()
    if env_pairs_str:
        pairs = [p.strip() for p in env_pairs_str.split(',') if p.strip()]
        logger.info(f"Pairs from env override: {pairs}")
    else:
        from spirit.utils.pair_registry import get_active_pairs
        pairs = get_active_pairs(instance=instance)
        logger.info(f"Pairs from registry (instance={instance}): {pairs}")

    # Create a probe strategy to read DataRequirements (cheap — no cache loading)
    probe_strategy = get_strategy()
    trading_enabled = probe_strategy is not None

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
    # Fetch per-pair lot sizes: live from Kraken API, paper/replay from defaults
    pair_info = None
    order_executor = None
    try:
        if trading_enabled and args.mode == 'live':
            from spirit.utils.kraken_api_client import get_asset_pairs, KRAKEN_PAIR_DEFAULTS
            try:
                pair_info = get_asset_pairs()
                logger.info(f"[MAIN] Fetched lot sizes from Kraken for {len(pair_info)} pairs")
            except Exception as e:
                logger.warning(f"[MAIN] Failed to fetch Kraken pair info, using defaults: {e}")
                pair_info = dict(KRAKEN_PAIR_DEFAULTS)

            from spirit.utils.order_executor import KrakenOrderExecutor
            order_executor = KrakenOrderExecutor(
                starting_equity=float(os.getenv('ACCOUNT_EQUITY', '10000')),
                pair_info=pair_info,
                run_id=run_id,
            )
        elif trading_enabled and args.mode == 'paper':
            from spirit.utils.kraken_api_client import KRAKEN_PAIR_DEFAULTS
            pair_info = dict(KRAKEN_PAIR_DEFAULTS)

            from spirit.utils.paper_order_executor import PaperOrderExecutor
            order_executor = PaperOrderExecutor(
                starting_equity=float(get_config('PAPER_STARTING_EQUITY', '1000')),
                max_trade_usd=float(get_config('PAPER_MAX_TRADE_USD', '250')),
                fee_pct=float(get_config('PAPER_FEE_PCT', '0.40')),
                pair_info=pair_info,
                replay_mode=(args.data_source == 'replay'),
                run_id=run_id,
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

    # Crash recovery: sync restored state per pair (skip in replay mode — clean slate)
    if args.data_source == 'replay':
        logger.info("[REPLAY] Skipping state restoration — starting with clean slate")
        for pair in pairs:
            ctx = context_manager.get(pair)
            ctx.clear_open_trade()
    else:
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
                if args.mode == 'paper':
                    logger.info(
                        f"[{pair}] Skipping equity restore for paper mode "
                        f"(stale=${ctx.equity:.2f}, using starting=${order_executor.equity:.2f})"
                    )
                else:
                    order_executor.equity = ctx.equity
                    logger.info(f"[{pair}] Restored equity: ${ctx.equity:.2f}")
                    if risk_gate:
                        risk_gate.update_equity(ctx.equity)

    # Startup reconciliation: cancel orphaned limit orders from previous session
    if trading_enabled and args.mode == 'live' and order_executor is not None:
        try:
            from utils.kraken_api_client import get_open_orders
            open_orders = get_open_orders()
            orphaned = open_orders.get('open', {})
            if orphaned:
                from utils.kraken_api_client import close_order as cancel_kraken_order
                for txid, order_data in orphaned.items():
                    descr = order_data.get('descr', {})
                    if descr.get('ordertype') == 'limit' and descr.get('type') == 'buy':
                        try:
                            cancel_kraken_order(txid)
                            logger.info(f"[STARTUP] Cancelled orphaned limit order: {txid}")
                        except Exception as e:
                            logger.warning(f"[STARTUP] Failed to cancel {txid}: {e}")
        except Exception as e:
            logger.debug(f"[STARTUP] Open orders check skipped: {e}")

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
    # Pipeline event bus (live mode only)
    # ---------------------------------------------------------------

    event_bus = None
    readiness_gate = None
    pipeline_bus_mode = get_config('PIPELINE_EVENT_BUS', 'none')
    is_live_mode = args.data_source == 'kraken'

    spirit_data_mode = get_config('SPIRIT_DATA_MODE', 'kraken_api')

    if pipeline_bus_mode == 'pg' and is_live_mode:
        try:
            from spirit.pipeline.pg_event_bus import PgEventBus
            from spirit.pipeline.readiness_gate import DataReadinessGate
            from spirit.utils.db_connection import get_listen_connection, get_connection

            readiness_timeout = float(get_config('PIPELINE_READINESS_TIMEOUT', '45'))
            event_bus = PgEventBus(
                listen_conn_factory=get_listen_connection,
                publish_conn_factory=get_connection,
            )

            # In pipeline data mode, the D-Limit event IS the readiness signal —
            # no gate needed. In kraken_api mode, keep the gate for backward compat.
            if spirit_data_mode == 'pipeline':
                readiness_gate = None
                logger.info("[Pipeline Mode] Readiness gate skipped (event-driven)")
            else:
                readiness_gate = DataReadinessGate(
                    event_bus=event_bus,
                    timeout=readiness_timeout,
                )

            # Wire pipeline events to strategy cache invalidation
            def _on_dlimit_event(event):
                strategy = strategies.get(event.pair)
                if strategy and hasattr(strategy, 'on_pipeline_event'):
                    try:
                        strategy.on_pipeline_event(event)
                    except Exception as e:
                        logger.debug(f"[PIPELINE] on_pipeline_event error: {e}")

            # Wire pipeline_bounce_physics NOTIFY → hot-reload all live
            # BounceReferenceStore instances. Full mode does reload_all(),
            # incremental mode does reload_incremental(watermark) using the
            # watermark carried in event.metadata. The calibrator emits this
            # event after every successful run (including no-op heartbeats).
            def _on_bounce_physics_event(event):
                try:
                    from spirit.tumblers.bounce_reference import get_active_stores
                except Exception as e:
                    logger.warning(
                        f"[BOUNCE_REF_RELOAD] Failed to import get_active_stores: {e}"
                    )
                    return

                stores = get_active_stores()
                metadata = getattr(event, 'metadata', {}) or {}
                mode = metadata.get('mode', 'full')
                watermark_iso = metadata.get('watermark')
                n_new = metadata.get('n_new', 0)

                logger.info(
                    f"[BOUNCE_REF_RELOAD] event received: mode={mode} "
                    f"n_new={n_new} watermark={watermark_iso} "
                    f"n_stores={len(stores)}"
                )

                if not stores:
                    logger.info(
                        "[BOUNCE_REF_RELOAD] no live stores in this process — "
                        "nothing to reload"
                    )
                    return

                # Heartbeat emission (n_new=0 incremental) — log and skip
                # the actual reload to avoid pointless PG queries.
                if mode == 'incremental' and n_new == 0:
                    logger.info(
                        "[BOUNCE_REF_RELOAD] heartbeat (no new bounces) — "
                        "skipping reload"
                    )
                    return

                # Parse watermark for incremental, fall back to full if missing
                watermark = None
                if mode == 'incremental' and watermark_iso:
                    try:
                        from datetime import datetime as _dt
                        watermark = _dt.fromisoformat(watermark_iso)
                    except Exception as e:
                        logger.warning(
                            f"[BOUNCE_REF_RELOAD] bad watermark {watermark_iso!r}, "
                            f"falling back to reload_all: {e}"
                        )
                        mode = 'full'

                for store in stores:
                    try:
                        if mode == 'incremental' and watermark is not None:
                            n_added = store.reload_incremental(watermark)
                            logger.info(
                                f"[BOUNCE_REF_RELOAD] incremental: +{n_added} refs"
                            )
                        else:
                            store.reload_all()
                            logger.info(
                                f"[BOUNCE_REF_RELOAD] full reload: "
                                f"{store.total_refs} refs total"
                            )
                    except Exception as e:
                        logger.warning(
                            f"[BOUNCE_REF_RELOAD] reload failed: {e}"
                        )

            event_bus.subscribe('pipeline_dlimit_60m', _on_dlimit_event)
            event_bus.subscribe('pipeline_dlimit_15m', _on_dlimit_event)
            event_bus.subscribe('pipeline_bounce_physics', _on_bounce_physics_event)
            event_bus.start()
            logger.info(
                f"[PIPELINE] Event bus active: mode=pg, "
                f"readiness_timeout={readiness_timeout}s"
            )
        except Exception as e:
            logger.warning(f"[PIPELINE] Event bus init failed (degrading gracefully): {e}")
            event_bus = None
            readiness_gate = None
    else:
        if is_live_mode and pipeline_bus_mode != 'pg':
            logger.info(f"[PIPELINE] Event bus disabled (PIPELINE_EVENT_BUS={pipeline_bus_mode})")

    # ---------------------------------------------------------------
    # Create orchestrator
    # ---------------------------------------------------------------

    # Set run_id on strategy instances (for scorer outcome recording via on_exit_completed)
    for pair_key, s in strategies.items():
        s.run_id = run_id

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
        event_bus=event_bus,
        readiness_gate=readiness_gate,
        run_id=run_id,
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

    if args.data_source == 'replay':
        from spirit.utils.replay_data_source import ReplayDataSource
        if getattr(args, 'with_monitoring', False) and monitoring_intervals:
            replay_intervals = sorted({interval} | monitoring_intervals)
        else:
            replay_intervals = [interval]
        logger.info(
            f"[PG Replay] pairs={pairs} intervals={replay_intervals} "
            f"primary={interval} range={args.start} to {args.end}"
        )
        data_source = ReplayDataSource(
            pairs=pairs,
            primary_interval=interval,
            start_date=args.start,
            end_date=args.end,
            intervals=replay_intervals,
            window_size=args.buffer_size,
        )
        multi_pair = True  # ReplayDataSource is always multi-pair style
    elif args.data_source == 'kraken':
        if spirit_data_mode == 'pipeline' and event_bus is not None:
            # Event-driven: pipeline events trigger eval (no Kraken API polling)
            from spirit.utils.pipeline_data_source import MultiPairPipelineDataSource
            from spirit.config import PIPELINE_FALLBACK_TIMEOUT
            logger.info(
                f"[Pipeline Mode] pairs={pairs} intervals={intervals_list} "
                f"primary={interval} fallback={PIPELINE_FALLBACK_TIMEOUT}s"
            )
            data_source = MultiPairPipelineDataSource(
                pairs=pairs,
                intervals=intervals_list,
                primary_interval=interval,
                event_bus=event_bus,
                buffer_size=args.buffer_size,
                fallback_timeout=PIPELINE_FALLBACK_TIMEOUT,
            )
            multi_pair = True
        elif multi_pair:
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

    is_csv = (args.data_source in ('csv', 'replay'))
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
    # CSV / PG replay loop
    # ---------------------------------------------------------------

    # Register replay run in the registry (after pairs/strategy are known)
    if args.replay and run_id != LIVE_RUN_ID:
        try:
            register_run(
                run_id=run_id,
                strategy_name=strategy_name,
                pairs=pairs,
                start_date=args.start,
                end_date=args.end,
                tag=args.run_tag,
                config={
                    'mode': args.mode,
                    'with_monitoring': getattr(args, 'with_monitoring', False),
                    'buffer_size': args.buffer_size,
                },
                git_hash=git_hash,
            )
        except Exception as e:
            logger.warning(f"[RUN] Failed to register run: {e}")

    if is_csv:
        import threading
        replay_label = "PG Replay" if args.data_source == 'replay' else "CSV Replay"

        def _csv_replay_loop():
            try:
                steps = 0
                for _ in iter(data_source):
                    steps += 1
                    if steps % 1000 == 0:
                        logger.info(f"[{replay_label}] progress steps={steps}")
            except Exception as e:
                import traceback
                logger.error(f"[{replay_label}] error: {e}\n{traceback.format_exc()}")

        csv_thread = threading.Thread(target=_csv_replay_loop, name="ReplayThread", daemon=True)
        orch.csv_thread = csv_thread
        csv_thread.start()
        logger.info(f"[MAIN] Waiting for {replay_label} to finish...")
        try:
            csv_thread.join()
        except Exception:
            pass
        logger.info(f"[MAIN] {replay_label} finished; initiating graceful shutdown...")

        # Run shadow outcome calculation for blocked decisions (replay only)
        if args.data_source == 'replay':
            try:
                from spirit.indicators.decision_engine.engine.shadow_outcome_calculator import (
                    calculate_shadow_outcomes,
                )
                shadow_count = calculate_shadow_outcomes(limit=5000)
                logger.info(f"[SHADOW] Post-replay shadow calc: {shadow_count} decisions processed")
            except Exception as e:
                logger.debug(f"[SHADOW] Shadow calc skipped: {e}")

        # Finalize replay run (compute summary stats)
        if args.replay and run_id != LIVE_RUN_ID:
            try:
                from spirit.utils.run_manager import finalize_run
                finalize_run(run_id, status='completed')
            except Exception as e:
                logger.warning(f"[RUN] Failed to finalize run: {e}")

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
