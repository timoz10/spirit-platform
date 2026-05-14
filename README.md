# Spirit

A programmable cryptocurrency trading platform. Run it on your own hardware, write your own strategies, keep your decisions on your own box.

Spirit is the framework — the strategy is yours. There's no "buy this, sell that" service. There's a Python plugin loader, a market-data feed (free, direct from Kraken — or paid, with our hosted indicators on top), and an exchange adapter.

---

## Quick start

```bash
pip install spirit-trading
python3 -m spirit.setup     # interactive setup wizard
python3 -m spirit.main --mode paper
```

Run the wizard once (it asks your tier, instance name, and Kraken API keys), drop a strategy file in `~/.spirit/strategies/` (or use the bundled `sma_crossover` example), and Spirit starts paper-trading.

To go live, swap `--mode paper` for `--mode live` once you've sanity-checked the paper P&L.

---

## Tiers

| Tier | Price | What you get |
|------|-------|--------------|
| **Free** | £0 | Local SQLite storage. OHLC data direct from your exchange (Kraken module included). Run Spirit in paper mode and learn the framework. No live trading, no D-Limit access. |
| **Plus** | £15/mo | Full D-Limit indicators (zones, trend states, swing detection), cloud-backed trade history with crash recovery, live trading enabled, push-based WebSocket events. The default for serious paper-trading or running real money. |

A **Pro** tier is on the roadmap once the V3 confidence scorer, full orderbook intelligence, and our hosted OHLC feed are all production-ready. Until then, Plus is the top of the stack.

Get a key at [portal.tradebot.live](https://portal.tradebot.live).

---

## Writing a strategy

Strategies are plain Python classes with three methods:

```python
class MyStrategy(SpiritStrategy):
    def evaluate_trade(self, ctx): ...
    def on_entry_confirmed(self, fill): ...
    def on_monitoring_tick(self, ctx): ...
```

Drop the file in `~/.spirit/strategies/` and Spirit picks it up at next startup. The bundled `sma_crossover` example is the simplest possible reference; it's runnable as-is.

For the full plugin guide and an LLM-ready companion (point Cursor or Claude at it and it can write a strategy from spec), see `docs/reference/platform/`.

---

## Project structure

```
src/spirit/
  main.py                  - entrypoint
  setup.py                 - first-run wizard
  config.py                - .env / yaml loader
  trade_signal.py          - signal dataclass
  trade_status.py          - status dataclass
  exchange/                - exchange adapter protocol + Kraken impl
  pipeline/                - WebSocket event bus, freshness cache, daemon health
  storage/                 - local SQLite schema (Free tier)
  strategies/
    base.py                - SpiritStrategy ABC
    examples/sma_crossover - reference strategy
  utils/                   - data providers, OHLC buffer, paper executor, etc.
```

Implementation details for the bundled D-Limit indicators, V3 scorer, and risk-gate calibrators live behind the gateway API — they're the IP that Plus / Pro subscriptions pay for. The framework you see here can run any strategy you write, against any data source you plug in.

---

## Bring your own exchange

Spirit ships with a Kraken adapter. To target a different exchange, implement the `ExchangeProvider` protocol in `src/spirit/exchange/protocol.py` and register it. See `docs/reference/EXCHANGE_PLUGIN_GUIDE.md` for the full guide.

---

## Status

- **v2.2.1** — first public release. Free + Plus tiers, single-machine deploy, paper or live mode.
- Production canary on Hetzner runs the same code, paper-mode, 24/7 — sees the same data and runs the same strategies you do.

---

## License

Apache-2.0. See [LICENSE](LICENSE).

The framework is free to use, modify, and redistribute. Plus / Pro subscriptions cover the hosted data and indicator infrastructure — the framework code itself doesn't depend on a subscription to run.

---

## Support

- Portal + key management: [portal.tradebot.live](https://portal.tradebot.live)
- Issue tracker: GitHub Issues on this repo
- For commercial / integration questions: tim@tradebot.live
