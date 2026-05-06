"""Minimal SMA-crossover example for Free-tier Spirit (#561).

This is a *teaching example* — the smallest possible illustration of how
to write a strategy against `FrameworkDataProvider` alone, with no
D-Limit, scorer, regime, or zone dependencies.

DO NOT run this with real money. The crossover signal is well known to
underperform buy-and-hold on most crypto pairs across most regimes; ship
it only as a starting template that a Free-tier user copies, edits, and
ideally replaces. The runtime guard below makes paper mode the default.

What it shows
-------------
1. Subclass `BaseStrategy`, implement `evaluate_trade(pair, mode, **kw)`.
2. Pull OHLC via `get_data_provider().get_ohlc(...)` — the only call that
   matters at the data layer; SQLite/Kraken/gateway are all hidden.
3. Compute features in pandas. Free tier ships no engineered-feature
   pipeline, so your strategy owns its own indicators.
4. Return the standard `{entry, exit, details}` dict.
"""

from __future__ import annotations

import os
from typing import Optional

import pandas as pd

from spirit.logger import get_logger
from spirit.strategies.base import BaseStrategy, DataRequirements
from spirit.trade_types import TradeRecord

logger = get_logger("strategies.examples.sma_crossover")


class SmaCrossoverStrategy(BaseStrategy):
    """Long when fast SMA crosses above slow SMA; exit on the reverse.

    Args:
        filter_pair:   Pair to trade (e.g. 'XBTUSD').
        filter_interval: Signal interval in minutes (e.g. 60).
        fast:          Fast SMA window (default 20 candles).
        slow:          Slow SMA window (default 50 candles).
        allow_live:    Must be True to permit `mode='live'`. Default
                       False — a guard against accidental real-money
                       runs. Override with `SMA_EXAMPLE_ALLOW_LIVE=1`
                       only after acknowledging the risks.
    """

    def __init__(
        self,
        filter_pair: str = "XBTUSD",
        filter_interval: int = 60,
        fast: int = 20,
        slow: int = 50,
        allow_live: bool = False,
    ) -> None:
        if fast >= slow:
            raise ValueError(
                f"fast ({fast}) must be < slow ({slow}) for a crossover."
            )
        self.filter_pair = filter_pair
        self.filter_interval = int(filter_interval)
        self.fast = int(fast)
        self.slow = int(slow)
        # Env override lets users opt into live without editing code.
        env_allow = os.environ.get("SMA_EXAMPLE_ALLOW_LIVE", "0").strip()
        self._allow_live = bool(allow_live) or env_allow in ("1", "true", "yes")

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def get_data_requirements(self) -> DataRequirements:
        return DataRequirements(
            pairs=[self.filter_pair],
            signal_interval=self.filter_interval,
            warmup_candles=max(self.slow * 4, 200),
        )

    def evaluate_trade(self, pair: str, mode: str = "test", **kwargs):
        if mode == "live" and not self._allow_live:
            raise RuntimeError(
                "SmaCrossoverStrategy refuses mode='live' by default. "
                "This is an educational example, not a profitable strategy. "
                "Set allow_live=True or SMA_EXAMPLE_ALLOW_LIVE=1 to proceed."
            )

        from spirit.utils.data_provider import get_data_provider
        rows = get_data_provider().get_ohlc(
            pair, self.filter_interval, limit=max(self.slow * 4, 200),
        )
        if len(rows) < self.slow + 2:
            return {"entry": False, "exit": False, "details": {}}

        df = pd.DataFrame(rows)
        df["sma_fast"] = df["close"].rolling(self.fast).mean()
        df["sma_slow"] = df["close"].rolling(self.slow).mean()

        # Last two closed candles — we evaluate the just-closed crossover.
        prev = df.iloc[-2]
        curr = df.iloc[-1]
        if pd.isna(prev["sma_fast"]) or pd.isna(prev["sma_slow"]):
            return {"entry": False, "exit": False, "details": {}}

        fast_above_now = curr["sma_fast"] > curr["sma_slow"]
        fast_above_prev = prev["sma_fast"] > prev["sma_slow"]

        open_trade = kwargs.get("open_trade")

        # Bullish cross — fast crossed above slow this candle.
        if fast_above_now and not fast_above_prev and open_trade is None:
            tr = TradeRecord(
                entry_datetime=str(curr["datetime"]),
                entry_price=float(curr["close"]),
                strategy_name="sma_crossover",
                interval=str(self.filter_interval),
                mode=mode,
                symbol=pair,
            )
            return {"entry": True, "exit": False, "details": tr.__dict__}

        # Bearish cross — fast crossed below slow this candle.
        if not fast_above_now and fast_above_prev and open_trade is not None:
            tr = TradeRecord(
                exit_datetime=str(curr["datetime"]),
                exit_price=float(curr["close"]),
                strategy_name="sma_crossover",
                interval=str(self.filter_interval),
                mode=mode,
                symbol=pair,
                exit_reason="sma_cross_down",
            )
            return {"entry": False, "exit": True, "details": tr.__dict__}

        return {"entry": False, "exit": False, "details": {}}
