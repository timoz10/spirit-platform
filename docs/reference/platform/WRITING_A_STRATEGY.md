---
status: v1 (#484 Part B)
target: v2.2.0
audience: someone writing their first Spirit strategy
---

# Writing a Spirit Strategy

This doc walks you through writing, loading, and running your own trading strategy on Spirit. By the end you'll have a working strategy file in `~/.spirit/strategies/`, registered, configured, and producing decisions on live market data.

If you're reading this for the second time and just want a code skeleton, jump to [§2 — Your first strategy](#2-your-first-strategy).

---

## 1. The 30-second mental model

Spirit is an event-driven trading framework. Once a candle interval (default 60m) ticks over, the orchestrator calls **your strategy's `evaluate_trade(pair, mode)` method**. You return a dict saying "enter," "exit," or "do nothing." Spirit handles everything else — order placement, exit monitoring, paper-equity bookkeeping, audit logging.

```
Market data ──► OHLC ──► D-Limit indicators ──► your strategy ──► entry/exit signal
                                                      │
                                       (optional) RiskGate sizes it
                                                      │
                                                  Order executor
                                                      │
                                                exit monitoring (1m ticks)
```

Three things you need to know:

- **You implement `BaseStrategy`**, a small abstract base class. One required method (`evaluate_trade`), several optional hooks.
- **Your strategy file lives in `~/.spirit/strategies/`** — Spirit loads it at startup. No PR, no merge, no rebuild.
- **Your strategy never talks to PostgreSQL directly.** All data flows through the gateway HTTP+WS API at `https://api.tradebot.live`.

---

## 2. Your first strategy

The minimum-viable strategy that confirms loading works:

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

In your `.env`:

```bash
SPIRIT_STRATEGY=my_first_algo
SPIRIT_STRATEGIES_DIR=~/.spirit/strategies   # optional; this is the default
```

Restart Spirit. You'll see:

```
[INFO] strategy_config: User strategy 'my_first_algo' resolved to MyFirstAlgo in ~/.spirit/strategies/my_first_algo.py
[INFO] strategy_config: Strategy loaded: my_first_algo (MyFirstAlgo)
```

That's the contract. Everything else is *what* your strategy does inside `evaluate_trade()`.

### A real strategy using gateway data

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
        return DataRequirements(
            pairs=["XBTUSD"],
            signal_interval=60,
            warmup_candles=24,
        )

    def evaluate_trade(self, pair: str, mode: str = "test", **kwargs):
        dp = get_data_provider()
        candles = dp.get_ohlc(pair=pair, interval=60, limit=24, order="desc")

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

Configure with optional params:

```bash
SPIRIT_STRATEGY=buy_the_dip
SPIRIT_STRATEGY_PARAMS='{"dip_pct": 3.0}'
```

---

## 3. The `BaseStrategy` contract

Path: `src/spirit/strategies/base.py` — read it; it's 135 lines and clear.

### Required

| Method | What it does |
|--------|--------------|
| `evaluate_trade(pair, mode, **kwargs)` | Called once per signal-interval close. Returns `{"entry", "exit", "details"}` |

The `details` dict needs at minimum `datetime`, `entry_price`, `symbol`. Add whatever else your strategy wants for downstream context — those extra fields end up in `entry_context` on the persisted trade row, queryable later.

### Return shape

```python
{
    "entry": bool,        # True to open a position
    "exit": bool,         # True to close existing position
    "details": {
        "datetime": ...,            # candle timestamp
        "entry_price": float,       # required if entry=True
        "symbol": str,              # the pair
        # ... any strategy-specific fields you want logged
    },
}
```

### Optional lifecycle hooks

| Hook | When |
|------|------|
| `get_data_requirements()` | At startup. Tells Spirit which pairs/intervals/warmup you need |
| `on_monitoring_tick(pair, interval, candle, open_trade)` | Each monitoring-interval tick (e.g. 1m) **while a trade is open** — return an exit dict to close |
| `on_entry_scan_tick(pair, interval, candle)` | Each monitoring-interval tick **with no open trade** — return an entry dict to open early |
| `on_pipeline_event(event)` | When upstream pipeline stages complete (e.g. D-Limit writes a new indicator row). Use this to refresh in-memory caches |
| `on_entry_confirmed(pair, signal, risk_decision)` | After RiskGate approves your entry — capture context for exit logic |
| `on_exit_completed(pair, exit_reason, ...)` | After the exit fires — update cooldowns, clear state |
| `validate_readiness()` | After warmup — return `(ready: bool, issues: list[str])` for the GREEN/YELLOW LIGHT log |

The defaults are no-ops, so override only what you need.

### `uses_risk_gate` property

Set this to `True` to opt into RiskGate sizing (see §7). Default is `False` — your strategy fully owns position sizing.

```python
class MyStrategy(BaseStrategy):
    uses_risk_gate = True
```

---

## 4. Pair routing — one strategy instance per pair

Spirit instantiates **one strategy object per pair** at startup. Each pair runs in its own thread; per-pair locks serialise eval + monitoring + on_pipeline_event so they don't interleave.

What this means for you:
- Use `self.filter_pair` to know which pair this instance owns.
- Don't share mutable state across pair instances unless it's intentional and locked.
- Inside `evaluate_trade`, `pair` is always your pair — but if you call `get_spirit_temp_ti()` (legacy helper), set the active pair first via `set_active_pair(pair)`.

```python
from spirit.utils.db_utils import set_active_pair, get_spirit_temp_ti

def evaluate_trade(self, pair, mode="test", **kwargs):
    set_active_pair(pair)
    df = get_spirit_temp_ti()    # routes to this thread's pair
    ...
```

Most modern strategies skip this and call `get_data_provider()` directly with explicit pair args — cleaner and thread-safe by construction.

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

On Free, `get_data_provider()` returns a `CompositeDataProvider` that splits the work:

- **Reads** (`get_ohlc`, `get_pairs`) come from an `ExchangeBackedDataProvider` that wraps your configured `ExchangeProvider` (Kraken at v2.3.0). No gateway calls, no API key. Bulk historical backfills are limited to ~720 candles per request — enough for live evaluation, not for deep backtesting.
- **Writes** (`put_state`, `write_performance`, `write_heartbeat`) go to a local SQLite file at `~/.spirit/<instance>/spirit.db` (override with `SPIRIT_SQLITE_PATH`).
- **IP methods** (`get_zones`, `get_dlimit`, `get_*_calibration`, …) raise `NotImplementedError` with an upgrade message. There is no silent fallback — if your strategy calls an IP method on Free it will crash, by design.

A working starting point is `src/spirit/strategies/examples/sma_crossover.py` — paper-mode-by-default, ~120 lines, uses only `FrameworkDataProvider`. Copy it, adapt it, and drop your version in `~/.spirit/strategies/` (see §6).

### Reading from event payloads (no fetch race)

When a D-Limit row finishes computing, the gateway pushes a WS event with the canonical row attached. Spirit's `on_pipeline_event(event)` hook gives it to you — read `event.row` directly instead of issuing a fresh fetch:

```python
def on_pipeline_event(self, event):
    if event.stage.startswith("dlimit_60m") and event.row:
        # event.row has slope_angle, capture_rate, trend_state, ...
        self._update_my_cache(event.candle_dt, event.row)
```

Why this matters: until 2026-04-29 strategies did a separate PG fetch after the event arrived, racing against commit visibility. Push-through-event eliminates the race. The short version is *don't fetch what's already in `event.row`*.

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
| `SPIRIT_INSTANCE` | Your instance label (e.g. `davy`, `customer-47`) |

```bash
SPIRIT_STRATEGY=buy_the_dip
SPIRIT_STRATEGY_PARAMS='{"dip_pct": 3.0, "max_position_usd": 500}'
SPIRIT_INSTANCE=davy
```

The loader silently drops kwargs your `__init__` doesn't accept, so older params can stay in JSON during refactors.

### End-to-end onboarding example

```bash
# 1. Spirit is installed at /opt/spirit/kraken-bot/. Your personal strategies
#    live elsewhere — they don't touch the install tree.
mkdir -p ~/.spirit/strategies

# 2. Drop your strategy file
cp my_strategy.py ~/.spirit/strategies/

# 3. Configure
echo "SPIRIT_STRATEGY=my_strategy" >> /opt/spirit/kraken-bot/.env

# 4. Restart Spirit
sudo systemctl restart spirit.service

# 5. Verify
sudo journalctl -u spirit.service --since "1 min ago" | grep "Strategy loaded"
# → Strategy loaded: my_strategy (MyStrategy)
```

To iterate: edit your file, restart the service. No git operations involved.

### Built-ins for reference

The `_STRATEGY_REGISTRY` dict in `strategy_config.py` lists in-tree strategies. **These are not templates you should fork into the user dir** unless you understand them — they're shipped for our internal use and future versions may break user copies. The skeleton in §8 is a safer starting point.

---

## 7. RiskGate — opt-in position sizing

RiskGate is a sizing helper that ships with Spirit. Lives at `src/spirit/indicators/decision_engine/engine/risk_gate.py`. It takes a signal, profiles its R:R, applies regime-adaptive floors, and returns a sized position.

You don't call RiskGate from your strategy — the orchestrator does. To opt in, set:

```python
class MyStrategy(BaseStrategy):
    uses_risk_gate = True
```

When `True`, after your `evaluate_trade` returns `entry=True`, the orchestrator:

1. Pulls a `signal` object out of `details["signal"]`
2. Calls `risk_gate.evaluate(signal)` → `RiskDecision`
3. If `risk_decision.trade is False`, the entry is dropped (logged with skip reason)
4. If `True`, sets `trade_record.buy_amount = risk_decision.position_size_usd` and proceeds

For RiskGate to make a sensible decision, your `details["signal"]` should expose:

- `confidence_score` — 0-100, your model's score for this entry
- `suggested_stop` — stop price
- `suggested_target` — take-profit price
- `regime`, `slope_angle`, `capture_rate` — D-Limit context (or whatever regime label your strategy uses)
- `pair`, `datetime`, `price`

If you set `uses_risk_gate = False` (the default), the orchestrator skips RiskGate entirely. You're then responsible for setting `trade_record.buy_amount` to a sane size before returning.

**Configuration** (`config/spirit.yaml`, all optional):

```yaml
RISK_GATE_CALIBRATION_ENABLED: true        # regime-adaptive R:R floors
RISK_GATE_RECALIBRATE_HOURS: 24
TRADE_USD_AMOUNT: 100                      # base size when calibrator is off
```

Tune to your risk appetite. A future setup guide will go deeper into RiskGate calibration.

---

## 8. A copy-paste skeleton (longer than §2)

This shows the most-common shape including monitoring exit, on_entry_confirmed context capture, and on_pipeline_event cache refresh.

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

In a future Spirit release we'll publish this as a pullable template module — same way `Kraken` is the reference exchange and `zone_bounce` is the reference strategy. For now, copy the block above.

---

## 9. Testing and tuning — best practices

We don't gate this. You own your risk appetite. But here's the order of operations that's saved us from real losses.

### 1. Smoke-test in isolation

Before pointing your live Spirit at the file, confirm it imports and runs:

```bash
python3 -c "
import sys
sys.path.insert(0, '/home/you/.spirit/strategies')
from my_strategy import MyStrategy

s = MyStrategy()
print(s.get_data_requirements())
print(s.evaluate_trade('XBTUSD', mode='test'))
"
```

Catches typos, missing imports, and obvious type errors before you waste a Spirit restart.

### 2. Local backtest (cheap, lossy)

Backtest on historical data via `CsvDataProvider` or replay mode (`docs/reference/BACKFILL_GUIDE.md`). Confirms your strategy runs end-to-end, doesn't crash on edge cases, and produces signals.

**A backtest is NOT a performance estimate.** Slippage, fee structure, and microstructure effects diverge enough that a profitable backtest can lose live money. Use backtests for *correctness*, not *expected returns*.

See [§Backtesting before #500 lands](#backtesting-before-500-lands) below for the temporary writes pattern.

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
| **Static market thresholds** (e.g. "ATR > 0.5") | Derive from data — percentiles, neighbours, historical regime distributions. See CLAUDE.md Rule 9. |
| **Fetch-after-event race** — your strategy fires a fetch in `on_pipeline_event` to get the just-written indicator row | Use `event.row` directly; the gateway pushed it through the event. CLAUDE.md Rule 6 |
| **Look-ahead bias** in backtests — accumulated values like zone strength queried as "current" instead of "as of this candle" | Reconstruct point-in-time using event/touch history. See `docs/reference/TESTING_STANDARDS.md` |
| **Type/timezone divergence** — `Decimal` vs `float`, naive vs tz-aware datetime | Rule 11 in CLAUDE.md. Always normalise at the boundary. |
| **Signature missing critical fields** producing `regime=UNKNOWN` trades | Check for `[FIELD-COVERAGE]` warnings; they fire when a critical D-Limit field is None at decision time |
| **Single-pair assumptions** in shared state | Each pair is a separate thread + strategy instance. Don't share mutable dicts without locks |

### Logging conventions

- One logger per module: `self.logger = get_logger("my_strategy")`. Keeps logs grep-friendly.
- Use `[TAG]` prefixes for greppable categories: `[ENTRY]`, `[EXIT]`, `[SKIP]`, `[CACHE]`, `[ERROR]`.
- `INFO` for state transitions and decisions. `DEBUG` for per-tick chatter. `WARNING` for unusual-but-recoverable. `ERROR` for things that need human attention.
- Always include the pair: `f"[{pair}][ENTRY] score={score:.2f}"`. When seven pairs are interleaved in one log file, the pair tag is everything.

---

## 10. Where to ask for help

The project is in transition — the public-facing repo for user strategies is being separated from the internal dev repo. For the v2.2.0 timeframe:

- **Internal users (Tim, Davy):** GitHub Issues on `timoz10/Bot`. Tag with `strategy-help`.
- **External users (when paid tiers open):** A new public `spirit-platform` repo will host issues, examples, and a knowledge base. The exact URL ships with the public launch.

For AI-assisted authoring, point your coding LLM (Cursor, Claude Code, Copilot) at [`STRATEGY_LLM_CONTEXT.md`](STRATEGY_LLM_CONTEXT.md) — a dense, machine-readable version of this contract designed for that workflow. We're also building a searchable knowledge base on `tradebot.live` with how-tos, planned post-v2.2.0.

For now, if you're stuck: open an issue with the strategy file attached, the log lines around the failure, and what you expected to happen.

---

## Conventions cheatsheet

- **One strategy per file.** The loader picks the first concrete `BaseStrategy` subclass; multiple classes in one file is technically allowed but confusing.
- **Class name is up to you.** Snake-case the filename, PascalCase the class. The loader doesn't enforce this — but humans reading logs will appreciate consistency.
- **Constructor takes `**_kwargs`.** The orchestrator may pass `filter_pair`, `filter_interval`, and other context. Accept and ignore unknowns gracefully.
- **`get_data_requirements()` is your contract.** If your strategy needs 1-minute candles for monitoring, declare it — the orchestrator subscribes accordingly. If you skip this, you get default behaviour (XBTUSD / 60m / 720 warmup).

---

## Backtesting before #500 lands

Spirit's per-user dev/prod data isolation (#500) will give you a clean separate endpoint and table set for backtests. **Until that ships (target v2.3.0)**, you have two options for backtesting on the platform:

### Option A — backtest locally (recommended)

Run backtests directly on your box without touching central PG. Output goes to a local SQLite database. This is fully isolated from everyone else's data and from your own production trades.

**When to pick this**: any iterative strategy development, parameter sweeps, multi-run comparisons.

**Limitations**: results stay local; you can't share them via the platform; no cloud-side storage.

### Option B — write to production tables with your own `instance` slug

Use the existing `POST /v1/performance` endpoint with `instance='<your-slug>'`. Your rows are RLS-isolated from every other user — they can't see your data, you can't see theirs. The only "contamination" is into your own production-trade history.

**When to pick this**: a single backtest where you want central storage and don't mind it living alongside your live trades temporarily. You can prune them by `run_id` once #500 lands.

**Limitations**:
- Your backtest rows show up in your own `instance`'s production trade history. If you query "all my trades" you'll see backtests mixed in until you filter by `run_id != 'live'`.
- No per-run quota or retention — long-running iterative backtests will accumulate.
- When #500 lands, you'll need to migrate these rows to the dev tables (one-shot script, not painful).

**Pattern**:
```python
# In your strategy/backtest code, set a distinctive run_id per backtest session
import uuid
run_id = f"backtest-{uuid.uuid4()}"

# Then write trades via the existing endpoint
POST /v1/performance
{
  "strategy_name": "my_strategy",
  "instance": "<your-slug>",
  "run_id": run_id,            # not 'live' or 'paper'
  "source": "backtest",
  ...
}
```

The `run_id` discipline matters: even within Option B, **always use a fresh UUID per backtest run**. This prevents your runs from corrupting each other (Tim hit this exact bug; it's why #500's dev tables enforce per-run uniqueness).

### Migration to #500 when it lands

Once #500 ships, dev backtest output moves to `/v1/dev/performance` and `public.dev_strategy_performance`. We'll provide a one-shot migration: rows in your production tables with `run_id LIKE 'backtest-%'` will move to the dev tables, leaving your live/paper rows in place. No data loss.

---

## See also

- [`STRATEGY_LLM_CONTEXT.md`](STRATEGY_LLM_CONTEXT.md) — dense LLM-targeted companion for AI-assisted strategy authoring
- [`PLATFORM_API.md`](PLATFORM_API.md) — full HTTP+WS API contract + return types
- [`PERMISSIONS.md`](PERMISSIONS.md) — tier matrix, instance scoping, RLS
- [`USER_LIFECYCLE.md`](USER_LIFECYCLE.md) — how your key + instance work
- [`../EXCHANGE_PLUGIN_GUIDE.md`](../EXCHANGE_PLUGIN_GUIDE.md) — same pattern, but for adding a new exchange
- [`../TESTING_STANDARDS.md`](../TESTING_STANDARDS.md) — backtest rigour, look-ahead bias
- `src/spirit/strategies/base.py` — the `BaseStrategy` ABC
- `src/spirit/strategy_config.py` — registry + user-strategy loader
- `src/spirit/strategies/zone_bounce.py` — the production reference strategy
- `CLAUDE.md` — module rules (Rules 1-11), data-flow architecture
- #500 — Per-user dev/prod data isolation (full architecture)
- #501 — Tiered retention + storage caps
- `docs/features/platform/DEV_PROD_DATA_ISOLATION.md` — design doc
