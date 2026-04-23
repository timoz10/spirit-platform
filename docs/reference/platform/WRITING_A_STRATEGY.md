# Writing a Spirit Strategy

How to build your own trading algorithm without modifying the orchestrator. The strategy you write lives in your own directory — Spirit loads it dynamically at startup. Throw it away, replace it, version it independently — the orchestrator never changes.

---

## TL;DR

1. Subclass `BaseStrategy` in a `.py` file
2. Drop the file in `~/.spirit/strategies/<your_name>.py`
3. Set `SPIRIT_STRATEGY=<your_name>` in your `.env`
4. Run Spirit — your strategy is loaded automatically

No orchestrator code changes. No PR to the Spirit repo. No package install.

---

## The minimum viable strategy

```python
# ~/.spirit/strategies/my_first_algo.py

from spirit.strategies.base import BaseStrategy, DataRequirements


class MyFirstAlgo(BaseStrategy):
    """The simplest strategy that does nothing — confirms loading works."""

    def evaluate_trade(self, pair: str, mode: str = "test", **kwargs):
        return {"entry": False, "exit": False, "details": {}}

    def get_data_requirements(self) -> DataRequirements:
        return DataRequirements(
            pairs=["XBTUSD"],
            signal_interval=60,
        )
```

Then in your `.env`:

```bash
SPIRIT_STRATEGY=my_first_algo
SPIRIT_STRATEGIES_DIR=~/.spirit/strategies   # optional; this is the default
```

Start Spirit. You'll see:
```
[INFO] strategy_config: User strategy 'my_first_algo' resolved to MyFirstAlgo in ~/.spirit/strategies/my_first_algo.py
[INFO] strategy_config: Strategy loaded: my_first_algo (MyFirstAlgo)
```

That's the contract. Everything else is *what* your strategy does inside `evaluate_trade()`.

---

## A real strategy — using gateway data

Your strategy can fetch market data, zones, calibrations, etc. via the gateway. Use the `ApiDataProvider` (already wired up by the orchestrator).

```python
# ~/.spirit/strategies/momentum_breakout.py

from spirit.strategies.base import BaseStrategy, DataRequirements
from spirit.data.api_provider import ApiDataProvider


class MomentumBreakout(BaseStrategy):
    """Enter long when price breaks above the prior 60m high with volume."""

    def __init__(self, filter_pair: str = None, filter_interval: int = 60, **kwargs):
        self.filter_pair = filter_pair or "XBTUSD"
        self.filter_interval = filter_interval
        self.data = ApiDataProvider()  # talks to wss://api.tradebot.live

    def get_data_requirements(self) -> DataRequirements:
        return DataRequirements(
            pairs=[self.filter_pair],
            signal_interval=self.filter_interval,
            warmup_candles=200,
        )

    def evaluate_trade(self, pair: str, mode: str = "test", **kwargs):
        # Fetch last 60 candles
        candles = self.data.get_ohlc(pair, interval=60, count=60)
        if len(candles) < 20:
            return {"entry": False, "exit": False, "details": {}}

        prior_high = max(c["high"] for c in candles[-20:-1])
        last = candles[-1]

        if last["close"] > prior_high and last["volume"] > 1.5 * (
            sum(c["volume"] for c in candles[-20:-1]) / 19
        ):
            return {
                "entry": True,
                "exit": False,
                "details": {
                    "datetime": last["datetime"],
                    "entry_price": last["close"],
                    "symbol": pair,
                    "trigger": "momentum_breakout",
                },
            }
        return {"entry": False, "exit": False, "details": {}}
```

---

## What's in `BaseStrategy`

| Method | Required? | When called |
|--------|-----------|-------------|
| `evaluate_trade(pair, mode, **kwargs)` | **Yes** | Every signal-interval candle close |
| `get_data_requirements()` | Recommended | At startup. Declares pairs, intervals, warmup |
| `on_monitoring_tick(pair, interval, candle, open_trade)` | No | Each monitoring-interval candle while a trade is open |
| `on_entry_scan_tick(pair, interval, candle)` | No | Each monitoring-interval candle when no trade is open |
| `on_entry_confirmed(pair, signal, risk_decision)` | No | After RiskGate approves entry |
| `on_exit_completed(pair, exit_reason, exit_price, entry_price, ...)` | No | After exit is processed |
| `on_pipeline_event(event)` | No | When upstream stage completes (e.g. D-Limit zone update) |
| `validate_readiness()` | No | After warmup. Return `(ok: bool, issues: list[str])` |
| `uses_risk_gate` (property) | No | Whether entries route through RiskGate for sizing |

See [`PLATFORM_API.md`](PLATFORM_API.md) for full method signatures.

---

## Return shape from `evaluate_trade`

```python
{
    "entry": bool,        # True to open a position
    "exit": bool,         # True to close existing position
    "details": {
        "datetime": ...,            # candle timestamp (datetime)
        "entry_price": float,       # required if entry=True
        "symbol": str,              # the pair
        # ... any strategy-specific fields you want logged
    },
}
```

---

## Lifecycle hooks for stateful strategies

If your strategy holds state across candles (e.g. tracking an open position's behaviour), use the lifecycle hooks:

```python
class StatefulStrategy(BaseStrategy):
    def __init__(self, **kwargs):
        self.entries = {}  # pair -> entry context

    def on_entry_confirmed(self, pair, signal, risk_decision):
        self.entries[pair] = {"entry_price": signal["entry_price"], ...}

    def on_exit_completed(self, pair, exit_reason, exit_price, entry_price, **kwargs):
        self.entries.pop(pair, None)

    def on_monitoring_tick(self, pair, interval, candle, open_trade):
        # Custom stop-loss, trailing stop, etc.
        if pair in self.entries:
            entry_price = self.entries[pair]["entry_price"]
            if candle["close"] < 0.97 * entry_price:
                return {"exit": True, "details": {"exit_reason": "my_stop"}}
        return None
```

---

## Where strategies are loaded from

Resolution order (first match wins):

1. **Built-in strategies** in `src/spirit/strategies/` (only `zone_bounce` is production)
2. **Built-in experimental** in `src/spirit/strategies/experimental/` (macd_cross, rsi_reversion, spine, regime_engine, test) — if `SPIRIT_STRATEGY=spine` etc.
3. **User strategies** in `$SPIRIT_STRATEGIES_DIR` (default `~/.spirit/strategies/`) — if `SPIRIT_STRATEGY=<filename-without-.py>`

The user dir is where your work belongs. The orchestrator never sees your file in `git diff`. You can:
- Version your strategies independently in your own git repo
- Share strategies as standalone files
- Try-and-throw-away without polluting commit history

---

## Example: setting up Davy's first strategy

```bash
# 1. Spirit is installed at /opt/spirit/kraken-bot/ (read-only as far as Davy's
#    workflow is concerned). His personal strategies live elsewhere.
mkdir -p ~/.spirit/strategies

# 2. Drop your strategy file
cp davy_exit_v1.py ~/.spirit/strategies/

# 3. Configure
echo "SPIRIT_STRATEGY=davy_exit_v1" >> /opt/spirit/kraken-bot/.env

# 4. Restart Spirit
sudo systemctl restart spirit.service

# 5. Verify
sudo journalctl -u spirit.service --since "1 min ago" | grep "Strategy loaded"
# → Strategy loaded: davy_exit_v1 (DavyExitV1)
```

To iterate: edit your file, restart the service. No git operations involved.

---

## Conventions

- **One strategy per file.** The loader picks the first concrete `BaseStrategy` subclass; multiple classes in one file is technically allowed but confusing.
- **Class name is up to you.** Snake-case the filename, PascalCase the class. The loader doesn't enforce this — but humans reading logs will appreciate consistency.
- **Constructor takes `**kwargs`.** The orchestrator may pass `filter_pair`, `filter_interval`, and other context. Accept and ignore unknowns gracefully.
- **`get_data_requirements()` is your contract.** If your strategy needs 1-minute candles for monitoring, declare it — the orchestrator subscribes accordingly. If you skip this, you get default behaviour (XBTUSD / 60m / 720 warmup).

---

## Testing your strategy locally

Before pointing your live Spirit at it:

```bash
# Unit-test in isolation
python3 -c "
import sys
sys.path.insert(0, '/home/davy/.spirit/strategies')
from davy_exit_v1 import DavyExitV1

s = DavyExitV1()
print(s.get_data_requirements())
print(s.evaluate_trade('XBTUSD', mode='test'))
"
```

For full backtest support, see Spirit's replay mode (`docs/reference/BACKFILL_GUIDE.md`).

---

## See also

- [`PLATFORM_API.md`](PLATFORM_API.md) — full method signatures + return types
- [`PERMISSIONS.md`](PERMISSIONS.md) — what your API key grants
- [`USER_LIFECYCLE.md`](USER_LIFECYCLE.md) — how your key + instance work
- [`../EXCHANGE_PLUGIN_GUIDE.md`](../EXCHANGE_PLUGIN_GUIDE.md) — same pattern, but for adding a new exchange
