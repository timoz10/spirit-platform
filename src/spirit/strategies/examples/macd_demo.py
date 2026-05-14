"""MACD demo strategy — full-stack tour of Spirit's strategy surface.

This is the second Free-tier example after `sma_crossover.py`. Where SMA is
the absolute-minimum "hello world", this file walks the rest of the
`BaseStrategy` lifecycle:

  * `get_data_requirements()` with a `monitoring_intervals` for sub-signal
    ticks (1m stop checks while the 60m signal candle hasn't closed yet).
  * `validate_readiness()` — post-warmup green/yellow light.
  * `evaluate_trade()` — entry/exit on the closed signal-interval candle.
  * `on_entry_confirmed()` — stash entry context for the exit loop.
  * `on_monitoring_tick()` — sub-signal exit check (fixed ATR stop).
  * `on_exit_completed()` — clean up stashed state.
  * `required_capabilities` + `uses_risk_gate` property overrides, with
    inline comments showing how to opt into Plus/Pro features.

Signal logic
------------
Long-only swing entries on a bullish MACD cross, layered with two filters:
  1. RSI(14) < 70  — don't chase into overbought conditions.
  2. close > SMA(200) — only take longs above the long trend.

Exits come from two paths:
  * Signal-interval (60m): bearish MACD cross — `evaluate_trade()`.
  * Sub-signal (1m): close <= entry - atr_stop_multiplier * ATR(14) at entry
    — `on_monitoring_tick()`. ATR is computed once at entry and stashed in
    `self._open_state`; the 1m tick path does no further indicator math.

All indicators are computed in pandas from the OHLC `dp.get_ohlc()` returns
— there is no dependency on `spirit_temp_ti`, the internal pipeline, or any
IP indicator (D-Limit, scorer, regime, etc.). This makes it the right
shape for Free-tier users who get a `CompositeDataProvider` wired as
`reads=ExchangeBackedDataProvider, writes=SqliteDataProvider`.

DO NOT run this with real money. Classic MACD-cross is well-studied and
loses to buy-and-hold across most crypto regimes; this is a *teaching*
example. The paper-by-default guard below requires explicit opt-in for
`mode='live'`.
"""

from __future__ import annotations

import os
from typing import Optional

import numpy as np
import pandas as pd

from spirit.logger import get_logger
from spirit.strategies.base import BaseStrategy, DataRequirements
from spirit.trade_types import TradeRecord

logger = get_logger("strategies.examples.macd_demo")


_LIVE_ENV = "MACD_DEMO_ALLOW_LIVE"


# ---------------------------------------------------------------------------
# Indicator helpers — all pure pandas, no upstream dependency.
# ---------------------------------------------------------------------------


def _compute_indicators(
    df: pd.DataFrame,
    *,
    fast_ema: int,
    slow_ema: int,
    signal_span: int,
    rsi_period: int,
    sma_trend_period: int,
    atr_period: int,
) -> pd.DataFrame:
    """Return a copy of `df` with `macd`, `macd_signal`, `rsi`, `sma_trend`,
    `atr` columns appended. Inputs must contain `close`, `high`, `low`.
    """
    out = df.copy()

    # MACD (Appel default 12/26/9): EMA(fast) - EMA(slow), signal = EMA of MACD.
    ema_fast = out["close"].ewm(span=fast_ema, adjust=False).mean()
    ema_slow = out["close"].ewm(span=slow_ema, adjust=False).mean()
    out["macd"] = ema_fast - ema_slow
    out["macd_signal"] = out["macd"].ewm(span=signal_span, adjust=False).mean()

    # RSI (Wilder's smoothing — EMA with alpha = 1/period).
    delta = out["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / rsi_period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / rsi_period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi"] = 100 - (100 / (1 + rs))

    # SMA trend filter.
    out["sma_trend"] = out["close"].rolling(sma_trend_period).mean()

    # ATR (Wilder). True Range = max(H-L, |H-prevC|, |L-prevC|).
    high_low = out["high"] - out["low"]
    high_close = (out["high"] - out["close"].shift()).abs()
    low_close = (out["low"] - out["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1 / atr_period, adjust=False).mean()

    return out


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class MacdDemoStrategy(BaseStrategy):
    """MACD cross + RSI/trend filters, with sub-signal ATR stop.

    Parameters
    ----------
    filter_pair : str
        Trading pair (e.g. 'XBTUSD').
    filter_interval : int
        Signal candle interval in minutes. Triggers `evaluate_trade`.
    fast_ema, slow_ema, signal_span : int
        MACD parameters (Appel default 12 / 26 / 9).
    use_rsi_filter, rsi_threshold : bool, float
        If True, reject entries when RSI(14) >= threshold (default 70).
    use_trend_filter, sma_trend_period : bool, int
        If True, reject entries when close <= SMA(200).
    atr_stop_multiplier : float
        Fixed stop = entry_price - mult * ATR(14) at entry.
    atr_stop_interval : int
        Monitoring interval for the stop check (default 1 minute).
    allow_live : bool
        Default False. `mode='live'` is rejected unless this is True or
        the env var `MACD_DEMO_ALLOW_LIVE=1` is set. This is a guard
        against accidental real-money runs — see the module docstring.
    """

    def __init__(
        self,
        filter_pair: str = "XBTUSD",
        filter_interval: int = 60,
        fast_ema: int = 12,
        slow_ema: int = 26,
        signal_span: int = 9,
        use_rsi_filter: bool = True,
        rsi_threshold: float = 70.0,
        use_trend_filter: bool = True,
        sma_trend_period: int = 200,
        atr_stop_multiplier: float = 2.0,
        atr_stop_interval: int = 1,
        allow_live: bool = False,
    ) -> None:
        if fast_ema >= slow_ema:
            raise ValueError(
                f"fast_ema ({fast_ema}) must be < slow_ema ({slow_ema})"
            )
        if sma_trend_period < 20:
            raise ValueError(
                f"sma_trend_period must be >= 20, got {sma_trend_period}"
            )

        self.filter_pair = filter_pair
        self.filter_interval = int(filter_interval)
        self.fast_ema = int(fast_ema)
        self.slow_ema = int(slow_ema)
        self.signal_span = int(signal_span)
        self.use_rsi_filter = bool(use_rsi_filter)
        self.rsi_threshold = float(rsi_threshold)
        self.use_trend_filter = bool(use_trend_filter)
        self.sma_trend_period = int(sma_trend_period)
        self.atr_stop_multiplier = float(atr_stop_multiplier)
        self.atr_stop_interval = int(atr_stop_interval)

        env_allow = os.environ.get(_LIVE_ENV, "0").strip()
        self._allow_live = bool(allow_live) or env_allow in ("1", "true", "yes")

        # In-memory entry context. Set by on_entry_confirmed, cleared by
        # on_exit_completed. For crash-recovery across restarts, mirror to
        # `dp.put_state("macd_demo:open_state", ...)` and rehydrate on the
        # first evaluate_trade call where `open_trade is not None`. Kept
        # out of this example to stay focused on the lifecycle hooks.
        self._open_state: Optional[dict] = None

    # ------------------------------------------------------------------
    # Lifecycle: data requirements + readiness
    # ------------------------------------------------------------------

    def get_data_requirements(self) -> DataRequirements:
        # Warmup = max(slow_ema*3, sma_trend_period + 50). The +50 buffer
        # lets ATR/RSI stabilise after Wilder smoothing converges.
        warmup = max(self.slow_ema * 3, self.sma_trend_period + 50)
        return DataRequirements(
            pairs=[self.filter_pair],
            signal_interval=self.filter_interval,
            monitoring_intervals=[self.atr_stop_interval],
            warmup_candles=warmup,
        )

    def validate_readiness(self) -> tuple[bool, list[str]]:
        # Called after warmup. We're always "ready" — but warn if warmup
        # is light, so the first few evaluations may see NaN indicators.
        issues: list[str] = []
        reqs = self.get_data_requirements()
        if reqs.warmup_candles < self.sma_trend_period + 50:
            issues.append(
                f"warmup_candles={reqs.warmup_candles} is tight for "
                f"SMA({self.sma_trend_period}) + ATR — first ~50 ticks "
                f"may return no-entry due to NaN indicators"
            )
        return (True, issues)

    @property
    def required_capabilities(self) -> frozenset[str]:
        """No platform capabilities required.

        Free tier reads OHLC directly from the exchange via
        `ExchangeBackedDataProvider`; this strategy computes every
        indicator it needs in pandas. A strategy that wanted Spirit's
        IP indicators (D-Limit zones, scorer, regime engine) would
        list them here, e.g.:

            return frozenset({"read:dlimit", "read:zones"})

        Spirit's preflight checks the gateway's `/v1/whoami` response
        and fails fast at startup if any required capability is missing,
        rather than crashing later on a 403 from a cold call site.
        """
        return frozenset()

    @property
    def uses_risk_gate(self) -> bool:
        """Free tier ships without RiskGate (it lives in
        `spirit.indicators.decision_engine`, which is not in the
        public mirror). To opt into portfolio-level sizing + exposure
        limits on Plus/Pro:

            1. Override this property to return True.
            2. Change `evaluate_trade` to populate
               `details["signal"]` with a TradeSignal:

                   from spirit.indicators.decision_engine.engine.risk_gate \\
                       import TradeSignal
                   signal = TradeSignal(
                       pair=pair, side="buy",
                       entry_price=float(curr["close"]),
                       confidence=0.5, atr=float(curr["atr"]),
                   )
                   return {"entry": True, "exit": False,
                           "details": {"signal": signal, ...}}

            3. Override `on_entry_confirmed(pair, signal, risk_decision)`
               to read sizing back from `risk_decision`.

        With `uses_risk_gate=False`, the orchestrator places the order
        directly using its default sizing and skips exposure/loss-limit
        checks. That's the right default for a teaching example; real
        capital should go through RiskGate.
        """
        return False

    # ------------------------------------------------------------------
    # Lifecycle: entry/exit on the signal-interval candle
    # ------------------------------------------------------------------

    def evaluate_trade(self, pair: str, mode: str = "test", **kwargs):
        if mode == "live" and not self._allow_live:
            raise RuntimeError(
                "MacdDemoStrategy refuses mode='live' by default. "
                "This is an educational example, not a profitable strategy. "
                f"Set allow_live=True or {_LIVE_ENV}=1 to proceed."
            )

        # Imported lazily to keep test fixtures from needing a full
        # provider chain at import time.
        from spirit.utils.data_provider import get_data_provider

        dp = get_data_provider()
        reqs = self.get_data_requirements()
        rows = dp.get_ohlc(
            pair, self.filter_interval, limit=reqs.warmup_candles
        )
        if len(rows) < self.sma_trend_period + 5:
            return {"entry": False, "exit": False, "details": {}}

        df = _compute_indicators(
            pd.DataFrame(rows),
            fast_ema=self.fast_ema,
            slow_ema=self.slow_ema,
            signal_span=self.signal_span,
            rsi_period=14,
            sma_trend_period=self.sma_trend_period,
            atr_period=14,
        )

        prev = df.iloc[-2]
        curr = df.iloc[-1]
        if pd.isna(prev["macd_signal"]) or pd.isna(curr["sma_trend"]):
            return {"entry": False, "exit": False, "details": {}}

        macd_above_now = curr["macd"] > curr["macd_signal"]
        macd_above_prev = prev["macd"] > prev["macd_signal"]
        bullish_cross = macd_above_now and not macd_above_prev
        bearish_cross = (not macd_above_now) and macd_above_prev

        open_trade = kwargs.get("open_trade")

        # ---- Exit path: in a trade, look for the reverse cross. ----
        if open_trade is not None:
            if bearish_cross:
                tr = TradeRecord(
                    exit_datetime=str(curr["datetime"]),
                    exit_price=float(curr["close"]),
                    exit_reason="macd_cross_down",
                    strategy_name="macd_demo",
                    interval=str(self.filter_interval),
                    mode=mode,
                    symbol=pair,
                )
                return {"entry": False, "exit": True, "details": tr.__dict__}
            return {"entry": False, "exit": False, "details": {}}

        # ---- Entry path: not in a trade, look for the bullish cross. ----
        if not bullish_cross:
            return {"entry": False, "exit": False, "details": {}}

        if self.use_rsi_filter and float(curr["rsi"]) >= self.rsi_threshold:
            logger.info(
                f"[{pair}] [MACD-DEMO] entry blocked by RSI filter "
                f"(rsi={curr['rsi']:.1f} >= {self.rsi_threshold})"
            )
            return {"entry": False, "exit": False, "details": {}}

        if self.use_trend_filter and float(curr["close"]) <= float(curr["sma_trend"]):
            logger.info(
                f"[{pair}] [MACD-DEMO] entry blocked by trend filter "
                f"(close={curr['close']:.4f} <= SMA={curr['sma_trend']:.4f})"
            )
            return {"entry": False, "exit": False, "details": {}}

        tr = TradeRecord(
            entry_datetime=str(curr["datetime"]),
            entry_price=float(curr["close"]),
            strategy_name="macd_demo",
            interval=str(self.filter_interval),
            mode=mode,
            symbol=pair,
        )
        # ATR is stashed alongside so on_entry_confirmed doesn't have to
        # re-fetch and recompute on a separate call. The orchestrator
        # echoes `details` back via on_entry_confirmed's `signal` arg.
        details = tr.__dict__.copy()
        details["_entry_atr"] = float(curr["atr"]) if pd.notna(curr["atr"]) else None
        return {"entry": True, "exit": False, "details": details}

    # ------------------------------------------------------------------
    # Lifecycle: stash + clear entry context for the exit loop
    # ------------------------------------------------------------------

    def on_entry_confirmed(self, pair: str, signal, risk_decision) -> None:
        """Called by the orchestrator after the order is placed (and
        RiskGate, if used, has approved). We stash the entry price + the
        ATR-derived stop so `on_monitoring_tick` can act every minute
        without re-fetching OHLC.
        """
        entry_price = None
        atr = None
        if isinstance(signal, dict):
            entry_price = signal.get("entry_price")
            atr = signal.get("_entry_atr")
        else:
            entry_price = getattr(signal, "entry_price", None)
            atr = getattr(signal, "_entry_atr", None)

        if entry_price is None or atr is None:
            logger.warning(
                f"[{pair}] [MACD-DEMO] on_entry_confirmed missing "
                f"entry_price ({entry_price}) or atr ({atr}); "
                f"ATR stop will be inactive for this trade"
            )
            self._open_state = None
            return

        stop_price = float(entry_price) - self.atr_stop_multiplier * float(atr)
        self._open_state = {
            "entry_price": float(entry_price),
            "atr": float(atr),
            "stop_price": stop_price,
        }
        logger.info(
            f"[{pair}] [MACD-DEMO] entry confirmed: "
            f"price={entry_price:.4f} atr={atr:.4f} "
            f"stop={stop_price:.4f} ({self.atr_stop_multiplier}xATR)"
        )

    def on_monitoring_tick(
        self, pair: str, interval: int, candle: dict, open_trade
    ) -> Optional[dict]:
        """Sub-signal (1m) ATR stop check.

        Called by the orchestrator for every monitoring-interval candle
        while a trade is open. The 60m signal candle won't close for up
        to an hour after entry, so this tick is what stops a bad trade
        bleeding for 59 minutes.
        """
        if self._open_state is None:
            # Stop wasn't stashed (entry happened before on_entry_confirmed
            # fired, or ATR was NaN). Nothing to do.
            return None

        close = candle.get("close")
        if close is None:
            return None

        if float(close) <= self._open_state["stop_price"]:
            dt = candle.get("datetime")
            tr = TradeRecord(
                exit_datetime=str(dt) if dt is not None else None,
                exit_price=float(close),
                exit_reason="atr_stop",
                strategy_name="macd_demo",
                interval=str(interval),
                mode=getattr(open_trade, "mode", "paper") if open_trade else "paper",
                symbol=pair,
            )
            logger.info(
                f"[{pair}] [MACD-DEMO] ATR stop fired: "
                f"close={close:.4f} <= stop={self._open_state['stop_price']:.4f}"
            )
            return {"exit": True, "details": tr.__dict__}

        return None

    def on_exit_completed(
        self,
        pair: str,
        exit_reason: str,
        exit_price: float,
        entry_price: float,
        exit_dt=None,
        net_pnl_pct: float = None,
    ) -> None:
        """Clear stashed state after the exit is processed."""
        self._open_state = None
        if net_pnl_pct is not None:
            logger.info(
                f"[{pair}] [MACD-DEMO] trade closed reason={exit_reason} "
                f"entry={entry_price:.4f} exit={exit_price:.4f} "
                f"pnl={net_pnl_pct:+.2f}%"
            )
