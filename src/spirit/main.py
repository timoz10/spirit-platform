"""
spirit_main.py

Main orchestration script for kraken-bot project.
Coordinates data sources (live/CSV, single/multi-interval), warmup/backfill,
feature engineering, strategy evaluation, and graceful shutdown/export.
"""

import faulthandler; faulthandler.enable()
import os
import sys
import time
import sqlite3
import argparse
import pandas as pd

from logger import get_logger
from system_config import KRAKEN_OHLC_COUNT, DB_PATH, KRAKEN_OHLC_INTERVAL
from utils.config_loader import get_config
from utils.data_source import LiveDataSource, CsvDataSource

# Optional multi-interval sources (may not exist on all branches)
try:
    from utils.multi_interval_data_source import MultiIntervalLiveDataSource
except Exception:
    MultiIntervalLiveDataSource = None
try:
    from utils.data_source import CsvMultiIntervalDataSource
except Exception:
    CsvMultiIntervalDataSource = None

from strategy_config import get_strategy
from temp_data import (
    create_spirit_temp_tables,
    drop_spirit_temp_tables,
    warmup_from_window,
    process_new_window,
)


def main():
    logger = get_logger("spirit_main")
    logger.info("---------- SPIRIT starting ----------")

    # Pre-flight validation: check environment, PG, Kraken keys, etc.
    from utils.preflight import run_preflight
    preflight = run_preflight()
    if not preflight.passed:
        logger.error("Pre-flight checks FAILED. Spirit cannot start.")
        for f in preflight.fatal_failures:
            logger.error(f"  {f.name}: {f.message}")
        raise SystemExit(1)
    logger.info("Pre-flight checks passed.")

    # Start web dashboard if enabled
    if get_config('SPIRIT_WEB', '').lower() in ('1', 'true', 'yes'):
        from spirit_web import start_web_server, update_state
        web_port = int(get_config('SPIRIT_WEB_PORT', '8377'))
        start_web_server(port=web_port)
    else:
        update_state = None

    # V2 context mode (feature flag: SPIRIT_V2_CONTEXT=1)
    use_v2_context = get_config('SPIRIT_V2_CONTEXT', '').lower() in ('1', 'true', 'yes')
    context = None

    if use_v2_context:
        from spirit_context import SpiritContext
        from utils.db_utils import set_spirit_context
        context = SpiritContext(persist_to_pg=True)
        set_spirit_context(context)
        logger.info("[V2] SpiritContext mode active (in-memory DataFrame, PG persistence)")
        context.restore_state()
    else:
        # Ensure SQLite temp tables exist (v1 path)
        create_spirit_temp_tables()

    # CLI
    parser = argparse.ArgumentParser(description="SPIRIT Main Orchestration")
    parser.add_argument('--csv', dest='data_source', action='store_const', const='csv', default='kraken', help='Use CSV file as data source (default: Kraken API)')
    parser.add_argument('--csv-path', type=str, default='test_data.csv', help='Path to CSV file when using --csv')
    parser.add_argument('--buffer-size', type=int, default=KRAKEN_OHLC_COUNT, help='Window/buffer size for initial warmup')
    parser.add_argument('--mode', type=str, choices=['test', 'paper', 'live'], default='test', help='Trading mode: test (simulation), paper (validated orders, simulated fills), or live (real orders)')
    parser.add_argument('--multi-interval', action='store_true', help='Enable multi-interval mode for live (uses OHLC_INTERVALS)')
    parser.add_argument('--duration', type=int, default=None, help='Optional duration (seconds) before graceful shutdown')
    parser.add_argument('--keep-temp', action='store_true', help='Keep temporary tables on exit')
    parser.add_argument('--no-pause', action='store_true', help='Skip interactive pause before cleanup')
    parser.add_argument('--exit-after-warmup', action='store_true', help='Exit after warmup/backfill (combine with --keep-temp)')
    args = parser.parse_args()

    # Strategy and trading deps
    strategy = get_strategy()
    trading_enabled = strategy is not None

    # Strategy data requirements (handshake)
    requirements = None
    if not trading_enabled:
        logger.warning("=" * 60)
        logger.warning("  NO TRADING ALGORITHM LOADED")
        logger.warning("  Spirit is running in MONITOR-ONLY mode.")
        logger.warning("  Candles will be processed but no trades will execute.")
        logger.warning("  Set SPIRIT_STRATEGY env var to enable trading.")
        logger.warning("=" * 60)
    else:
        requirements = strategy.get_data_requirements()
        logger.info(f"Trading algorithm: {type(strategy).__name__}")
        logger.info(
            f"[HANDSHAKE] pairs={requirements.pairs} "
            f"signal_interval={requirements.signal_interval}m "
            f"monitoring_intervals={requirements.monitoring_intervals} "
            f"warmup_candles={requirements.warmup_candles} "
            f"uses_risk_gate={strategy.uses_risk_gate}"
        )
        # Warn if KRAKEN_OHLC_INTERVAL disagrees with strategy's signal_interval
        env_interval = int(KRAKEN_OHLC_INTERVAL)
        if env_interval != requirements.signal_interval:
            logger.warning(
                f"[HANDSHAKE] KRAKEN_OHLC_INTERVAL={env_interval} but strategy declares "
                f"signal_interval={requirements.signal_interval}. "
                f"Strategy's signal_interval will be used."
            )

    from trade_types import TradeRecord
    from trade_logic import TradeStateManager, process_trade_signals
    trade_state_manager = TradeStateManager(use_sqlite=not use_v2_context)
    order_executor = None
    try:
        if trading_enabled and args.mode == 'live':
            from utils.order_executor import KrakenOrderExecutor
            order_executor = KrakenOrderExecutor(
                starting_equity=float(os.getenv('ACCOUNT_EQUITY', '10000')),
            )
        elif trading_enabled and args.mode == 'paper':
            from utils.paper_order_executor import PaperOrderExecutor
            order_executor = PaperOrderExecutor(
                starting_equity=float(get_config('PAPER_STARTING_EQUITY', '1000')),
                max_trade_usd=float(get_config('PAPER_MAX_TRADE_USD', '250')),
                fee_pct=float(get_config('PAPER_FEE_PCT', '0.40')),
            )
    except (ImportError, ValueError, OSError) as e:
        logger.exception(f"Order executor init failed: {e}")
        order_executor = None

    # Initialize RiskGate if strategy declares it needs one
    risk_gate = None
    if trading_enabled and strategy.uses_risk_gate:
        try:
            from indicators.decision_engine.engine.risk_gate import RiskGate
            if args.mode in ('paper', 'live') and order_executor is not None:
                equity = order_executor.equity
            else:
                equity = float(os.getenv('ACCOUNT_EQUITY', '10000'))
            risk_gate = RiskGate(account_equity=equity)
            logger.info(f"[RISK_GATE] Initialized with equity=${equity:.0f}")
        except (ImportError, ValueError, OSError) as e:
            logger.debug(f"RiskGate init skipped: {e}")

    # V2 crash recovery: sync restored state into TradeStateManager + executor
    if use_v2_context and context.open_trade is not None:
        trade_state_manager.open_trade = context.open_trade
        logger.info("[V2] Restored open trade into TradeStateManager")
    if use_v2_context and context.equity > 0 and order_executor and hasattr(order_executor, 'equity'):
        order_executor.equity = context.equity
        logger.info(f"[V2] Restored equity into PaperOrderExecutor: ${context.equity:.2f}")
        if risk_gate:
            risk_gate.update_equity(context.equity)

    # Update web dashboard state after init
    if update_state is not None:
        init_equity = order_executor.equity if (order_executor and hasattr(order_executor, 'equity')) else float(os.getenv('ACCOUNT_EQUITY', '10000'))
        update_state(mode=args.mode, equity=init_equity)

    # Web dashboard helpers (no-ops if web is disabled)
    if update_state is not None:
        from spirit_web import increment_candles as _web_inc, record_signal as _web_sig, record_decision as _web_dec
    else:
        _web_inc = _web_sig = _web_dec = None

    # Callback for single-interval
    def on_new_candle(window_df):
        if not getattr(data_source, 'warmup_complete', True):
            return
        try:
            if _web_inc:
                _web_inc()
            # Process new candle: v2 (in-memory) or v1 (SQLite)
            # In multi-interval mode, V2 append is handled by on_interval_window;
            # append_candle dedup prevents double-insert but we skip to avoid the overhead
            _has_multi_buffers = hasattr(data_source, 'buffers')
            if use_v2_context and not _has_multi_buffers:
                records = getattr(window_df, 'records', None)
                if records:
                    latest = records[-1]
                    candle_dict = latest.__dict__.copy() if hasattr(latest, '__dict__') else dict(latest)
                    context.append_candle(candle_dict, interval=interval)
            elif not use_v2_context:
                persist_raw = (args.data_source == 'kraken')
                process_new_window(window_df, db_path=DB_PATH, persist_raw=persist_raw, cleanup=False)

            # Skip trade evaluation if no strategy loaded (monitor-only mode)
            if not trading_enabled:
                return

            result = strategy.evaluate_trade(mode=args.mode, open_trade=trade_state_manager.open_trade)
            entry_flag = False
            exit_flag = False
            trade_record = None
            if isinstance(result, TradeRecord):
                trade_record = result
            elif isinstance(result, dict):
                entry_flag = bool(result.get('entry', False))
                exit_flag = bool(result.get('exit', False))
                details = result.get('details') or {}
                try:
                    from trade_types import TradeRecord as TR
                    fields = set(TR.__dataclass_fields__.keys())
                    kwargs = {k: v for k, v in details.items() if k in fields}
                    trade_record = TR(**kwargs)
                except Exception:
                    trade_record = None

                # Record signal to web dashboard
                if _web_sig and details.get('signal') is not None:
                    sig = details['signal']
                    _web_sig({
                        'datetime': getattr(sig, 'datetime', None),
                        'confidence_score': getattr(sig, 'confidence_score', None),
                        'zone_id': getattr(sig, 'zone_id', None),
                        'regime': getattr(sig, 'regime', None),
                        'price': getattr(sig, 'price', None),
                    })

                # RiskGate integration: if strategy returned a TradeSignal, route through RiskGate
                if risk_gate and entry_flag and trade_record is not None:
                    signal = details.get('signal')
                    if signal is not None:
                        risk_decision = risk_gate.evaluate(signal)

                        # Record decision to web dashboard
                        if _web_dec:
                            _web_dec(risk_decision.to_dict())

                        if not risk_decision.trade:
                            get_logger("spirit_callback").info(
                                f"[RISK_GATE] Skipped: {risk_decision.skip_reason}"
                            )
                            entry_flag = False  # Override — don't trade
                        else:
                            trade_record.buy_amount = risk_decision.position_size_usd
                            get_logger("spirit_callback").info(
                                f"[RISK_GATE] Sized: ${risk_decision.position_size_usd:.0f} "
                                f"({risk_decision.profile_tier}, R:R={risk_decision.rr_ratio:.1f})"
                            )

            if entry_flag and trade_record is not None:
                process_trade_signals("buy", trade_record, args.mode, trade_state_manager, order_executor=order_executor, logger=get_logger("spirit_callback"))
                # V2: persist open trade to PG
                if use_v2_context and trade_state_manager.open_trade is not None:
                    context.set_open_trade(trade_state_manager.open_trade)
                # Update web dashboard with open trade
                if update_state and trade_state_manager.open_trade is not None:
                    ot = trade_state_manager.open_trade
                    update_state(open_trade={
                        'entry_price': getattr(ot, 'entry_price', None),
                        'entry_datetime': str(getattr(ot, 'entry_datetime', '')),
                        'buy_amount': getattr(ot, 'buy_amount', None),
                    })
            if exit_flag and trade_record is not None and trade_state_manager.open_trade is not None:
                # copy entry context to exit record
                for attr in ['entry_index','entry_datetime','entry_price','macd_bullish_cross_entry','atr_entry','sma200_entry','rsi_entry','impulse_macd_entry','adx_entry','plus_di_entry','minus_di_entry','trend_direction_entry','buy_amount','symbol','interval','strategy_name','mode']:
                    setattr(trade_record, attr, getattr(trade_state_manager.open_trade, attr, None))
                process_trade_signals("sell", trade_record, args.mode, trade_state_manager, order_executor=order_executor, logger=get_logger("spirit_callback"))
                # Sync paper equity into RiskGate after trade closes
                if risk_gate and args.mode in ('paper', 'live') and order_executor is not None:
                    risk_gate.update_equity(order_executor.equity)
                # V2: persist state change to PG
                if use_v2_context:
                    context.clear_open_trade()
                    if order_executor and hasattr(order_executor, 'equity'):
                        context.set_equity(order_executor.equity)
                # Update web dashboard after trade close
                if update_state:
                    eq = order_executor.equity if (order_executor and hasattr(order_executor, 'equity')) else None
                    update_state(open_trade=None, **(({'equity': eq}) if eq is not None else {}))
            # V2: periodic state save (every 10 candles)
            if use_v2_context and context.health['candles_processed'] % 10 == 0:
                context.save_state()
        except Exception as e:
            get_logger("spirit_callback").exception(f"Exception in on_new_candle: {e}")
            if use_v2_context:
                context.health['errors'] += 1

    # Select data source
    # Strategy-declared interval takes precedence; fall back to config for monitor-only
    if requirements is not None:
        interval = requirements.signal_interval
    else:
        interval = int(KRAKEN_OHLC_INTERVAL)
    if args.data_source == 'kraken':
        logger.info(f"Starting live with primary interval={interval}")
        # Derive intervals: merge config + strategy requirements
        try:
            from system_config import OHLC_INTERVALS as _OHLC_INTERVALS
            cfg_intervals = {int(x) for x in _OHLC_INTERVALS}
        except Exception:
            cfg_intervals = set()
        cfg_intervals.add(interval)
        # Add strategy's monitoring intervals
        if requirements is not None:
            for mi in requirements.all_intervals:
                cfg_intervals.add(mi)
        force_single = os.environ.get('FORCE_SINGLE_INTERVAL', '').lower() in ['1','true','yes']
        auto_multi = (len(cfg_intervals) > 1)
        use_multi = (args.multi_interval or auto_multi) and not force_single and (MultiIntervalLiveDataSource is not None)
        if use_multi:
            intervals_list = sorted(cfg_intervals)
            logger.info(f"[MultiInterval Live] intervals={intervals_list} primary={interval}")
            data_source = MultiIntervalLiveDataSource(intervals=intervals_list, primary_interval=interval, buffer_size=args.buffer_size)
        else:
            data_source = LiveDataSource(buffer_size=args.buffer_size, interval=interval)
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
            data_source = CsvMultiIntervalDataSource(args.csv_path, primary_interval=interval, intervals=file_intervals, window_size=args.buffer_size)
        else:
            data_source = CsvDataSource(args.csv_path, interval=interval)

    # Warmup on primary interval to window size
    warmup_candles = requirements.warmup_candles if requirements is not None else 720
    target = max(warmup_candles, int(args.buffer_size))
    if args.data_source == 'kraken':
        logger.info(f"Waiting for live buffer to reach {target} on primary={interval}m...")
        if hasattr(data_source, 'buffer'):
            data_source.buffer.wait_for_buffer(min_size=target - 1)
            window_df = data_source.get_window(target)
        else:
            data_source.wait_for_data(interval, min_size=target - 1, timeout=180)
            window_df = data_source.get_window(interval, target)
    else:
        # CSV mode warmup
        if hasattr(data_source, 'df_by_interval'):
            # Multi-interval CSV: warm up using the FIRST `target` candles of the primary interval
            try:
                from data_types import OHLCRecord, OHLCData
                df_primary = data_source.df_by_interval.get(int(interval))
                if df_primary is None or df_primary.empty:
                    raise RuntimeError(f"No data for primary interval={interval} in CSV.")
                slice_df = df_primary.head(target)
                records = [
                    OHLCRecord.from_raw(
                        pair=row.get("pair"),
                        interval=int(interval),
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
                window_df = OHLCData(records=records)
            except Exception as e:
                logger.error(f"CSV warmup build failed: {e}; falling back to last window.")
                window_df = data_source.get_window(interval, target)
        elif hasattr(data_source, 'buffers'):
            # Multi-interval live-like interface; use generic API
            try:
                data_source.wait_for_data(interval, min_size=target - 1, timeout=1)
            except Exception:
                pass
            window_df = data_source.get_window(interval, target)
        else:
            # Single-interval CSV source uses its pointer-based window (first window)
            window_df = data_source.get_window(target)

    if use_v2_context:
        # V2: warmup into in-memory context
        context.warmup(window_df.records, interval=interval)
        if order_executor and hasattr(order_executor, 'equity'):
            context.set_equity(order_executor.equity)
    else:
        # V1: warmup into SQLite
        warmup_from_window(window_df, db_path=DB_PATH, logger=logger)
    data_source.warmup_complete = True
    logger.info("Warmup complete.")

    # Green light check: validate strategy readiness after warmup
    if trading_enabled and requirements is not None:
        ready, issues = strategy.validate_readiness()
        if ready:
            logger.info(
                f"[GREEN LIGHT] Strategy {type(strategy).__name__} ready. "
                f"signal={requirements.signal_interval}m "
                f"monitoring={requirements.monitoring_intervals} "
                f"risk_gate={'ON' if risk_gate else 'OFF'}"
            )
        else:
            logger.warning(
                f"[YELLOW LIGHT] Strategy {type(strategy).__name__} has issues "
                f"(proceeding anyway): {issues}"
            )

    # Backfill other intervals immediately if multi (v1 only — v2 handles via append_candle)
    if hasattr(data_source, 'buffers') and not use_v2_context:
        try:
            from temp_data import append_bulk_insert_spirit_temp_ti, engineer_full_for_interval
            intervals_present = sorted(getattr(data_source, 'buffers', {}).keys())
            others = [i for i in intervals_present if int(i) != int(interval)]
            for itvl in others:
                try:
                    # For CSV multi-interval, prefer first `target` candles for backfill
                    if hasattr(data_source, 'df_by_interval'):
                        from data_types import OHLCRecord, OHLCData
                        df_itvl = data_source.df_by_interval.get(int(itvl))
                        if df_itvl is None or df_itvl.empty:
                            continue
                        slice_df = df_itvl.head(target)
                        recs = [
                            OHLCRecord.from_raw(
                                pair=row.get("pair"),
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
                        win = OHLCData(records=recs)
                    else:
                        data_source.wait_for_data(int(itvl), min_size=300, timeout=180)
                        win = data_source.get_window(int(itvl), target)
                    append_bulk_insert_spirit_temp_ti(win.records, DB_PATH)
                    engineer_full_for_interval(int(itvl), DB_PATH, logger=logger)
                except Exception as _e:
                    logger.debug(f"Backfill failed for interval={itvl}: {_e}")
        except Exception as e:
            logger.debug(f"Backfill skipped: {e}")

    # Monitoring intervals from strategy requirements
    monitoring_intervals = set()
    if requirements is not None:
        monitoring_intervals = set(requirements.monitoring_intervals)

    # Monitoring tick handler: routes sub-signal candles to strategy
    def _handle_monitoring_tick(interval_val, window_df):
        """Route monitoring-interval candles to strategy.on_monitoring_tick()."""
        if not trading_enabled or trade_state_manager.open_trade is None:
            return
        try:
            records = getattr(window_df, 'records', None)
            if not records:
                return
            latest = records[-1]
            candle_dict = latest.__dict__.copy() if hasattr(latest, '__dict__') else dict(latest)
            result = strategy.on_monitoring_tick(int(interval_val), candle_dict, trade_state_manager.open_trade)
            if result and result.get('exit'):
                details = result.get('details', {})
                try:
                    from trade_types import TradeRecord as TR
                    fields = set(TR.__dataclass_fields__.keys())
                    kwargs = {k: v for k, v in details.items() if k in fields}
                    trade_record = TR(**kwargs)
                except Exception:
                    trade_record = None
                if trade_record is not None and trade_state_manager.open_trade is not None:
                    # Copy entry context to exit record
                    for attr in ['entry_index','entry_datetime','entry_price','macd_bullish_cross_entry','atr_entry','sma200_entry','rsi_entry','impulse_macd_entry','adx_entry','plus_di_entry','minus_di_entry','trend_direction_entry','buy_amount','symbol','interval','strategy_name','mode']:
                        setattr(trade_record, attr, getattr(trade_state_manager.open_trade, attr, None))
                    process_trade_signals("sell", trade_record, args.mode, trade_state_manager, order_executor=order_executor, logger=get_logger("spirit_callback"))
                    # Post-exit housekeeping (same as on_new_candle exit path)
                    if risk_gate and args.mode in ('paper', 'live') and order_executor is not None:
                        risk_gate.update_equity(order_executor.equity)
                    if use_v2_context:
                        context.clear_open_trade()
                        if order_executor and hasattr(order_executor, 'equity'):
                            context.set_equity(order_executor.equity)
                    if update_state:
                        eq = order_executor.equity if (order_executor and hasattr(order_executor, 'equity')) else None
                        update_state(open_trade=None, **(({'equity': eq}) if eq is not None else {}))
        except Exception as e:
            get_logger("spirit_callback").exception(f"Exception in _handle_monitoring_tick: {e}")

    # Register callbacks
    if hasattr(data_source, 'buffers'):
        def on_interval_window(interval_val, window_df):
            try:
                iv = int(interval_val)
                # For CSV: allow primary interval + monitoring intervals through
                if args.data_source == 'csv' and iv != int(interval) and iv not in monitoring_intervals:
                    return
                if use_v2_context:
                    # V2: append to in-memory context
                    records = getattr(window_df, 'records', None)
                    if records:
                        latest = records[-1]
                        candle_dict = latest.__dict__.copy() if hasattr(latest, '__dict__') else dict(latest)
                        context.append_candle(candle_dict, interval=iv)
                else:
                    # V1: persist to SQLite
                    persist_raw = (args.data_source == 'kraken')
                    process_new_window(window_df, db_path=DB_PATH, persist_raw=persist_raw, cleanup=False)
                # Signal interval → full evaluate_trade path
                if iv == int(interval):
                    on_new_candle(window_df)
                # Monitoring interval → lightweight stop check
                elif iv in monitoring_intervals:
                    _handle_monitoring_tick(iv, window_df)
            except Exception as e:
                logger.error(f"[MultiInterval] callback error: {e}")
        data_source.register_callback(on_interval_window)
    else:
        data_source.register_callback(on_new_candle)

    # CSV replay iterator in background
    csv_thread = None
    if args.data_source == 'csv':
        # Unified graceful shutdown routine (export trades, stop sources, drop temps)
        def graceful_shutdown():
            # V2: save final state to PG
            if use_v2_context:
                context.save_state()
                logger.info("[V2] Final state saved to PG")

            # Export trades to outputs/ (v1 SQLite path)
            if not use_v2_context:
                try:
                    logger.info(f"[EXPORT] CWD: {os.getcwd()}")
                except Exception:
                    pass
                conn = sqlite3.connect(DB_PATH)
                try:
                    trades_df = pd.read_sql_query("SELECT * FROM spirit_temp_trade ORDER BY trade_id ASC", conn)
                    try:
                        from system_config import BASE_DIR as EXPORT_BASE_DIR
                    except Exception:
                        EXPORT_BASE_DIR = os.getcwd()
                    out_dir = os.path.join(EXPORT_BASE_DIR, "outputs")
                    os.makedirs(out_dir, exist_ok=True)
                    out_path = os.path.join(out_dir, "trades_output.csv")
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    ts_out_path = os.path.join(out_dir, f"trades_output_{ts}.csv")
                    trades_df.to_csv(out_path, index=False)
                    trades_df.to_csv(ts_out_path, index=False)
                    logger.info(f"Exported {len(trades_df)} trades to {out_path} and {ts_out_path}")
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

            # Optional pause before cleanup (skip for CSV runs or when --no-pause)
            try:
                if not args.no_pause and args.data_source != 'csv' and sys.stdin.isatty():
                    input("\n[PAUSE] Temp tables are ready. Press Enter to drop temp tables and exit...\n")
            except Exception:
                pass

            # Graceful data source stop
            if hasattr(data_source, 'stop'):
                try:
                    data_source.stop()
                except Exception:
                    pass

            # Drop temp tables unless requested to keep (v1 only)
            if not use_v2_context:
                if args.keep_temp:
                    logger.info("--keep-temp set; preserving temp tables.")
                else:
                    drop_spirit_temp_tables()
                    logger.info("Temporary tables dropped.")

            # Join CSV replay thread if running
            try:
                if csv_thread is not None and csv_thread.is_alive():
                    csv_thread.join(timeout=2)
            except Exception:
                pass

        import threading
        def _csv_replay_loop():
            try:
                steps = 0
                primary = int(interval)
                total_primary = None
                if hasattr(data_source, 'df_by_interval'):
                    try:
                        total_primary = len(data_source.df_by_interval.get(primary, []))
                    except Exception:
                        total_primary = None
                for _ in iter(data_source):
                    steps += 1
                    if steps % 1000 == 0:
                        try:
                            cur = None
                            if hasattr(data_source, 'pointers'):
                                cur = int(data_source.pointers.get(primary, 0))
                            if total_primary is not None:
                                logger.info(f"[CSV Replay] progress primary={primary} {cur}/{total_primary}")
                            else:
                                logger.info(f"[CSV Replay] progress steps={steps}")
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"[CSV Replay] error: {e}")
        csv_thread = threading.Thread(target=_csv_replay_loop, name="CsvReplayThread", daemon=True)
        csv_thread.start()
        # For CSV mode, wait for replay to finish then initiate shutdown
        logger.info("[MAIN] Waiting for CSV replay to finish...")
        try:
            csv_thread.join()
        except Exception:
            pass
        logger.info("[MAIN] CSV replay finished; initiating graceful shutdown...")
        # Directly perform graceful shutdown/export for CSV runs
        graceful_shutdown()
        return

    # Early exit after warmup if requested
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
        # V2: save final state to PG
        if use_v2_context:
            context.save_state()
            logger.info("[V2] Final state saved to PG")

        # Export trades to outputs/ (v1 SQLite path)
        if not use_v2_context:
            try:
                logger.info(f"[EXPORT] CWD: {os.getcwd()}")
            except Exception:
                pass
            conn = sqlite3.connect(DB_PATH)
            try:
                trades_df = pd.read_sql_query("SELECT * FROM spirit_temp_trade ORDER BY trade_id ASC", conn)
                try:
                    from system_config import BASE_DIR as EXPORT_BASE_DIR
                except Exception:
                    EXPORT_BASE_DIR = os.getcwd()
                out_dir = os.path.join(EXPORT_BASE_DIR, "outputs")
                os.makedirs(out_dir, exist_ok=True)
                out_path = os.path.join(out_dir, "trades_output.csv")
                ts = time.strftime("%Y%m%d_%H%M%S")
                ts_out_path = os.path.join(out_dir, f"trades_output_{ts}.csv")
                trades_df.to_csv(out_path, index=False)
                trades_df.to_csv(ts_out_path, index=False)
                logger.info(f"Exported {len(trades_df)} trades to {out_path} and {ts_out_path}")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        # Optional pause before cleanup (skip for CSV runs or when --no-pause)
        try:
            if not args.no_pause and args.data_source != 'csv' and sys.stdin.isatty():
                input("\n[PAUSE] Temp tables are ready. Press Enter to drop temp tables and exit...\n")
        except Exception:
            pass

        # Graceful data source stop
        if hasattr(data_source, 'stop'):
            try:
                data_source.stop()
            except Exception:
                pass

        # Drop temp tables unless requested to keep (v1 only)
        if not use_v2_context:
            if args.keep_temp:
                logger.info("--keep-temp set; preserving temp tables.")
            else:
                drop_spirit_temp_tables()
                logger.info("Temporary tables dropped.")

        # Join CSV replay thread if running
        try:
            if csv_thread is not None and csv_thread.is_alive():
                csv_thread.join(timeout=2)
        except Exception:
            pass


if __name__ == "__main__":
    main()
