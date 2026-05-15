---
status: v1
target: v2.2.0+
audience: someone writing their first Spirit strategy
---

# Writing a Spirit Strategy

This doc walks you through writing, loading, and running your own trading strategy on Spirit. By the end you'll have a working strategy file in `~/.spirit/strategies/`, registered, configured, and producing decisions on live market data.

If you're reading this for the second time and just want a code skeleton, jump to [§2 — Your first strategy](#2-your-first-strategy).

---

## 1. The 30-second mental model

Spirit is event-driven. Once a candle closes (default 60m), the orchestrator calls **your strategy's `evaluate_trade(pair, mode)`**. You return `{"entry", "exit", "details"}`. Spirit handles order placement, exit monitoring, paper-equity bookkeeping, and audit logging.

```
OHLC ─► (D-Limit indicators) ─► your strategy ─► entry/exit signal ─► (RiskGate) ─► order
```

Three things to know:

- **You subclass `BaseStrategy`.** One required method, several optional hooks.
- **Your file lives in `~/.spirit/strategies/`.** Spirit loads it at startup — no PR, no rebuild.
- **You never talk to PostgreSQL.** All data flows through the gateway at `https://api.tradebot.live`.

---

## 2. Your first strategy

A complete worked example — buys when the 1h candle closes a configurable % below the 24h high:

```python
# ~/.spirit/strategies/buy_the_dip.py
"""
buy_the_dip.py — buys when 1h candle closes 2% below the 24h high.
"""

from spirit.strategies.base import BaseStrategy, DataRequirements
from spirit.utils.data_provider import get_data_provider


class BuyTheDip(BaseStrategy):
    def __init__(self, dip_pct: float = 2.0, **_kwargs):
        self.dip_pct = float(dip_pct)
        self.filter_pair = "XBTUSD"
        self.filter_interval = 60

    def get_data_requirements(self) -> DataRequirements:
        return DataRequirements(pairs=["XBTUSD"], signal_interval=60, warmup_candles=24)

    def evaluate_trade(self, pair: str, mode: str = "test", **kwargs):
        candles = get_data_provider().get_ohlc(pair=pair, interval=60, limit=24, order="desc")
        if len(candles) < 24:
            return {"entry": False, "exit": False, "details": {}}

        latest = candles[0]
        high_24h = max(float(c["high"]) for c in candles)
        drop_pct = (high_24h - float(latest["close"])) / high_24h * 100

        return {
            "entry": drop_pct >= self.dip_pct,
            "exit": False,
            "details": {
                "datetime": latest["datetime"],
                "entry_price": float(latest["close"]),
                "symbol": pair,
                "drop_pct": round(drop_pct, 2),
            },
        }
```

Configure in your `.env`:

```bash
SPIRIT_STRATEGY=buy_the_dip
SPIRIT_STRATEGY_PARAMS='{"dip_pct": 3.0}'
```

Restart Spirit. You'll see `Strategy loaded: buy_the_dip (BuyTheDip)` in the log — that's it. Everything else is what your strategy does inside `evaluate_trade()`.

---

## 3. The `BaseStrategy` contract

Source: `src/spirit/strategies/base.py` (~135 lines, clear). For verbatim signatures, return-shape details, and the full lifecycle-hook table see [`STRATEGY_LLM_CONTEXT.md`](STRATEGY_LLM_CONTEXT.md) §2–§3. Conceptually:

- **One required method**: `evaluate_trade(pair, mode, **kwargs)` — called once per signal-interval close, returns `{"entry": bool, "exit": bool, "details": dict}`. `details` needs at minimum `datetime`, `entry_price`, `symbol`; extra fields persist into `entry_context` on the trade row.
- **Optional hooks** for what happens around the entry decision: `get_data_requirements()` (warmup + interval declaration), `on_monitoring_tick()` (1m exit checks while a trade is open), `on_entry_scan_tick()` (early entries between signal intervals), `on_pipeline_event()` (refresh caches when upstream stages complete), `on_entry_confirmed()`/`on_exit_completed()` (lifecycle callbacks), `validate_readiness()` (post-warmup sanity check).
- **`uses_risk_gate = True`** opts into RiskGate sizing (see §7). Default `False`, your strategy owns sizing.

Override only what you need — the defaults are no-ops.

---

## 4. Pair routing — one strategy instance per pair

Spirit creates **one strategy object per pair** at startup. Each pair runs in its own thread; per-pair locks serialise eval + monitoring + on_pipeline_event so they never interleave for the same pair.

What this means for you:
- Use `self.filter_pair` to know which pair this instance owns.
- Call `get_data_provider()` with explicit pair args — thread-safe by construction.
- Don't share mutable state across pair instances without an explicit `threading.Lock`.

---

## 5. Data sources

Spirit instances get data exclusively through the gateway. The `DataProvider` abstraction (`src/spirit/utils/data_provider.py`) gives you typed methods; under the hood it's `https://api.tradebot.live/v1/...`.

```python
from spirit.utils.data_provider import get_data_provider

dp = get_data_provider()

# Market data
candles = dp.get_ohlc(pair="XBTUSD", interval=60, limit=720)
zones = dp.get_zones(pair="XBTUSD", active=True)
touches = dp.get_zone_touches(pair="XBTUSD", limit=500)
indicators = dp.get_dlimit(pair="XBTUSD", interval=60)

# Calibrations (cross-instance, deterministic aggregates)
cooldown = dp.get_cooldown_calibration(pair="XBTUSD")
risk_floor = dp.get_risk_gate_calibration(pair="XBTUSD")

# Your own data (instance-scoped — RLS-isolated)
my_trades = dp.get_performance(strategy_name="my_strategy", limit=100)
```

### Tier gating

Different paid tiers see different endpoints. The full matrix is in [`PERMISSIONS.md`](PERMISSIONS.md); the short version:

| Tier | What you can pull |
|------|-------------------|
| `free` | OHLC (direct from exchange), pair registry, your own state. Local SQLite for trades. |
| `subscription` | Adds D-Limit indicators (60m + 15m), zones, zone touches, bounce events, bounce physics, cloud-side trade storage. |
| `pro` | Subscription + scorer outputs, full orderbook history, wall lifecycles, write access. |

If you call an endpoint your tier doesn't unlock, you get a `403`. Plan your data needs around your tier.

### Free-tier specifics

On Free, `get_data_provider()` returns a `CompositeDataProvider`:

- **Reads** come from your configured `ExchangeProvider` (Kraken by default) — no gateway calls, no API key. Bulk backfills cap at ~720 candles per request.
- **Writes** go to local SQLite at `~/.spirit/<instance>/spirit.db` (override with `SPIRIT_SQLITE_PATH`).
- **IP methods** (`get_zones`, `get_dlimit`, `get_*_calibration`, …) raise `NotImplementedError` with an upgrade message. No silent fallback.

A working starting point: `src/spirit/strategies/examples/sma_crossover.py` (~120 lines, Framework-only).

### Reading from event payloads (no fetch race)

When a D-Limit row finishes computing, the gateway pushes a WS event with the canonical row attached. Read `event.row` directly in `on_pipeline_event` — don't issue a fresh fetch, you'll race against commit visibility.

```python
def on_pipeline_event(self, event):
    if event.stage.startswith("dlimit_60m") and event.row:
        # event.row has slope_angle, capture_rate, trend_state, ...
        self._update_my_cache(event.candle_dt, event.row)
```

---

## 6. Loading and configuring

### File layout

```
~/.spirit/strategies/
  buy_the_dip.py            ← one class per file
  another_idea.py
```

Spirit scans this directory at startup. The loader (`src/spirit/strategy_config.py:_load_user_strategy`) finds the first **concrete** `BaseStrategy` subclass in the file and uses it. Convention: one strategy per file, file-name = registry name.

If you want to point at a different directory:

```bash
SPIRIT_STRATEGIES_DIR=/path/to/my/strategies
```

### Resolution order

When Spirit looks up `SPIRIT_STRATEGY=<name>`, it tries (first match wins):

1. **Built-in production** in `src/spirit/strategies/` (currently just `zone_bounce`)
2. **Built-in experimental** in `src/spirit/strategies/experimental/` (`macd_cross`, `rsi_reversion`, `spine`, `regime_engine`, `test`)
3. **User dir** at `$SPIRIT_STRATEGIES_DIR` (default `~/.spirit/strategies/`)

Your strategies belong in step 3. The user dir is where iteration happens — edit the file, restart the service, no git involved.

### Environment configuration

Spirit reads three env vars (typically from `.env` or `config/spirit.yaml`):

| Var | Purpose |
|-----|---------|
| `SPIRIT_STRATEGY` | Name (filename without `.py`, or alias for a built-in) |
| `SPIRIT_STRATEGY_PARAMS` | JSON dict, passed as kwargs to your `__init__` |
| `SPIRIT_INSTANCE` | Your instance label (e.g. `test`, `customer-47`) |

```bash
SPIRIT_STRATEGY=buy_the_dip
SPIRIT_STRATEGY_PARAMS='{"dip_pct": 3.0, "max_position_usd": 500}'
SPIRIT_INSTANCE=test
```

The loader silently drops kwargs your `__init__` doesn't accept, so older params can stay in JSON during refactors.

### End-to-end onboarding

```bash
mkdir -p ~/.spirit/strategies
cp my_strategy.py ~/.spirit/strategies/
echo "SPIRIT_STRATEGY=my_strategy" >> /opt/spirit/kraken-bot/.env
sudo systemctl restart spirit.service
sudo journalctl -u spirit.service --since "1 min ago" | grep "Strategy loaded"
# → Strategy loaded: my_strategy (MyStrategy)
```

Iterate by editing the file and restarting the service. No git involved.

---

## 7. RiskGate — opt-in position sizing

RiskGate (`src/spirit/indicators/decision_engine/engine/risk_gate.py`) profiles a signal's R:R, applies regime-adaptive floors, and returns a sized position. The orchestrator calls it, not you. To opt in:

```python
class MyStrategy(BaseStrategy):
    uses_risk_gate = True
```

When `True`, after `evaluate_trade` returns `entry=True`, the orchestrator pulls `signal = details["signal"]`, calls `risk_gate.evaluate(signal)`, and either drops the entry (logged with reason) or sizes it via `risk_decision.position_size_usd`.

`details["signal"]` should expose: `confidence_score` (0-100), `suggested_stop`, `suggested_target`, `regime`, `slope_angle`, `capture_rate`, `pair`, `datetime`, `price`.

With `uses_risk_gate = False` (default) the orchestrator skips RiskGate; you own sizing — set `trade_record.buy_amount` yourself or rely on `TRADE_USD_AMOUNT` from `config/spirit.yaml`.

```yaml
RISK_GATE_CALIBRATION_ENABLED: true     # regime-adaptive R:R floors
RISK_GATE_RECALIBRATE_HOURS: 24
TRADE_USD_AMOUNT: 100                   # base size when calibrator is off
```

---

## 8. A copy-paste skeleton

The production shape: signal eval + 1m exit monitoring + on_pipeline_event cache refresh.

```python
"""
my_strategy.py — copy this, rename, modify.
"""

from spirit.strategies.base import BaseStrategy, DataRequirements
from spirit.utils.data_provider import get_data_provider


class MyStrategy(BaseStrategy):
    uses_risk_gate = False    # set True to opt into RiskGate sizing

    def __init__(
        self,
        filter_pair: str = "XBTUSD",
        filter_interval: int = 60,
        my_threshold: float = 1.5,
        **_kwargs,
    ):
        self.filter_pair = filter_pair
        self.filter_interval = int(filter_interval)
        self.my_threshold = float(my_threshold)
        self._dlimit_cache: dict[str, dict] = {}

    def get_data_requirements(self) -> DataRequirements:
        return DataRequirements(
            pairs=[self.filter_pair],
            signal_interval=self.filter_interval,
            monitoring_intervals=[1],   # 1m exit checks while open
            warmup_candles=720,
        )

    # --- entry decision -----------------------------------------------------

    def evaluate_trade(self, pair, mode="test", **kwargs):
        open_trade = kwargs.get("open_trade")
        if open_trade is not None:
            return {"entry": False, "exit": False, "details": {}}

        score = self._score_entry(pair)
        if score < self.my_threshold:
            return {"entry": False, "exit": False, "details": {}}

        latest = self._latest_candle(pair)
        return {
            "entry": True,
            "exit": False,
            "details": {
                "datetime": latest["datetime"],
                "entry_price": float(latest["close"]),
                "symbol": pair,
                "score": score,
                # populate signal fields if uses_risk_gate=True
            },
        }

    # --- exit on 1m monitoring tick ----------------------------------------

    def on_monitoring_tick(self, pair, interval, candle, open_trade):
        if open_trade is None:
            return None
        entry = float(open_trade.entry_price)
        price = float(candle["close"])
        if price < entry * 0.97:
            return {
                "exit": True,
                "details": {
                    "datetime": candle["datetime"],
                    "exit_price": price,
                    "exit_reason": "fixed_stop_-3pct",
                    "symbol": pair,
                },
            }
        return None

    # --- pipeline event: refresh dlimit cache from event.row, no fetch -----

    def on_pipeline_event(self, event):
        if not event.stage.startswith("dlimit_60m"):
            return
        row = getattr(event, "row", None) or (event.metadata or {}).get("row")
        if isinstance(row, dict):
            key = (event.candle_dt or "")[:19]
            self._dlimit_cache[key] = row

    # --- helpers -----------------------------------------------------------

    def _latest_candle(self, pair):
        return get_data_provider().get_ohlc(pair=pair, interval=60, limit=1)[0]

    def _score_entry(self, pair) -> float:
        return 0.0
```

Copy the block above, rename, modify.

---

## 9. Testing and tuning — best practices

We don't gate this. You own your risk appetite. But here's the order of operations that's saved us from real losses.

### 1. Smoke-test in isolation

Before restarting Spirit, confirm your file imports and runs:

```bash
python3 -c "
import sys; sys.path.insert(0, '/home/you/.spirit/strategies')
from my_strategy import MyStrategy
s = MyStrategy()
print(s.get_data_requirements())
print(s.evaluate_trade('XBTUSD', mode='test'))
"
```

Catches typos, missing imports, and obvious type errors before you waste a restart.

### 2. Local backtest (cheap, lossy)

Backtest on historical data via `CsvDataProvider` or replay mode. Confirms end-to-end correctness, edge-case handling, and that your signal actually trips.

**A backtest is NOT a performance estimate.** Slippage, fees, and microstructure diverge enough that a profitable backtest can lose live money. Use backtests for *correctness*, not *expected returns*. See §Backtesting below for the writes pattern.

### 3. Paper soak (the real validation)

Set `SPIRIT_MODE=paper` and run for ≥24 hours. Watch the logs for:

- **Errors / tracebacks** — should be zero
- **Entries firing as expected** — does your signal actually trip?
- **Exits firing as expected** — both winning and losing exits, including stops
- **`[FIELD-COVERAGE]` / `[PAYLOAD-MISS]` warnings** — should be zero (if not, you're racing against indicator commits)
- **Regime classification** — do your trades have meaningful `regime` labels? `UNKNOWN` everywhere means data wiring is broken

A common mistake is calling 8h of paper "validated." Markets shift across sessions; soak through at least one full UTC-day cycle. Two if your strategy is regime-sensitive.

### 4. Live with small size

When you flip to `live`, start with `TRADE_USD_AMOUNT` an order of magnitude smaller than your eventual target. Every live trader who skips this regrets it eventually. Once you've got a week of clean live trades, scale up.

### Pitfalls that have bitten us

These are real failures from the project's history. Worth a re-read before going live.

| Pitfall | Fix |
|---------|-----|
| **Static market thresholds** (e.g. "ATR > 0.5") | Derive from data — percentiles, neighbours, historical regime distributions. |
| **Fetch-after-event race** — your strategy fires a fetch in `on_pipeline_event` to get the just-written indicator row | Use `event.row` directly; the gateway already pushed it through the event. |
| **Look-ahead bias** in backtests — accumulated values like zone strength queried as "current" instead of "as of this candle" | Reconstruct point-in-time using event/touch history; never query the current accumulated value at a past decision point. |
| **Type/timezone divergence** — `Decimal` vs `float`, naive vs tz-aware datetime | Always normalise at the boundary. DataProvider returns `float` and tz-aware UTC. |
| **Signature missing critical fields** producing `regime=UNKNOWN` trades | Check for `[FIELD-COVERAGE]` warnings; they fire when a critical D-Limit field is None at decision time |
| **Single-pair assumptions** in shared state | Each pair is a separate thread + strategy instance. Don't share mutable dicts without locks |

### Logging conventions

- One logger per module: `self.logger = get_logger("my_strategy")`. Keeps logs grep-friendly.
- Use `[TAG]` prefixes for greppable categories: `[ENTRY]`, `[EXIT]`, `[SKIP]`, `[CACHE]`, `[ERROR]`.
- `INFO` for state transitions and decisions. `DEBUG` for per-tick chatter. `WARNING` for unusual-but-recoverable. `ERROR` for things that need human attention.
- Always include the pair: `f"[{pair}][ENTRY] score={score:.2f}"`. When seven pairs are interleaved in one log file, the pair tag is everything.

---

## 10. Where to ask for help

- **GitHub Issues:** [`github.com/timoz10/spirit-platform`](https://github.com/timoz10/spirit-platform/issues) — bug reports, feature requests, strategy questions. Tag with `strategy-help`.
- **AI-assisted authoring:** point your coding LLM (Cursor, Claude Code, Copilot) at [`STRATEGY_LLM_CONTEXT.md`](STRATEGY_LLM_CONTEXT.md) — a dense, machine-readable version of this contract designed for that workflow.
- **Knowledge base:** a searchable how-to library on `tradebot.live` is in the works.

If you're stuck: open an issue with the strategy file attached, the log lines around the failure, and what you expected to happen.

---

## Conventions

- **One strategy per file.** The loader picks the first concrete `BaseStrategy` subclass; extras are silently ignored.
- **Snake-case the filename, PascalCase the class.** Not enforced, but consistent.
- **Constructor takes `**_kwargs`.** The orchestrator may pass `filter_pair`, `filter_interval`, etc. Accept and ignore unknowns.
- **`get_data_requirements()` is your contract.** Declare what you need or you get the default (XBTUSD / 60m / 720 warmup).

---

## Backtesting

Two options until per-user dev tables ship:

- **Local (recommended).** Run with a `CsvDataProvider` or replay mode; writes go to local SQLite. Fully isolated, no contamination of central data. Best for iteration, parameter sweeps, and multi-run comparisons.
- **Central tables with your own instance slug.** `POST /v1/performance` with your `instance` — rows are RLS-isolated from other users. Acceptable for a single backtest you want stored centrally; just filter by `run_id != 'live'` when reading.

**Discipline either way:** use a fresh UUID per backtest run (`run_id = f"backtest-{uuid.uuid4()}"`). Reused `run_id`s corrupt calibrator buffers.

---

## See also

- [`STRATEGY_LLM_CONTEXT.md`](STRATEGY_LLM_CONTEXT.md) — dense LLM-targeted companion for AI-assisted strategy authoring
- [`PLATFORM_API.md`](PLATFORM_API.md) — full HTTP+WS API contract + return types
- [`PERMISSIONS.md`](PERMISSIONS.md) — tier matrix, instance scoping, RLS
- [`USER_LIFECYCLE.md`](USER_LIFECYCLE.md) — how your key + instance work
- [`../EXCHANGE_PLUGIN_GUIDE.md`](../EXCHANGE_PLUGIN_GUIDE.md) — same pattern, but for adding a new exchange
- `src/spirit/strategies/base.py` — the `BaseStrategy` ABC
- `src/spirit/strategy_config.py` — registry + user-strategy loader
- `src/spirit/strategies/zone_bounce.py` — the production reference strategy
