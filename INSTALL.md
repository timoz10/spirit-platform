# Installing Spirit

Spirit is published on PyPI as `spirit-platform`. The recommended install uses **pipx**, which keeps Spirit's dependencies isolated from your system Python.

> **Supported platforms:** Tested on Ubuntu 22.04 / 24.04 and Debian 12. macOS and Windows installs are unverified at this stage — patches and testers welcome. Open an issue if you try Spirit on another platform.

## Ubuntu / Debian (most common)

```bash
sudo apt install -y pipx
pipx ensurepath
pipx install spirit-platform
```

Open a new shell (or `exec bash`) so the updated PATH takes effect. `spirit`, `spirit-health`, `spirit-preflight`, and `spirit-uninstall` will then be on your PATH.

> **Why pipx and not `pip install`?**
> Modern Ubuntu and Debian protect the system Python from package installs (PEP 668). `pip install spirit-platform` will fail with `externally-managed-environment`. `pipx` builds an isolated venv per package and is the canonical way to install Python CLI apps on these distros.

## Other Linux / no pipx available

Use a venv directly:

```bash
python3 -m venv ~/.spirit-venv
~/.spirit-venv/bin/pip install spirit-platform
~/.spirit-venv/bin/spirit --help
```

Symlink the entry points onto your PATH if you don't want to type the full path every time:

```bash
ln -s ~/.spirit-venv/bin/spirit ~/.local/bin/spirit
```

## Verify the install

```bash
spirit-health
```

You should see `Process: NOT running` and `Local DB: not found` — that's expected before you run the setup wizard.

## First run

### 1. Run the setup wizard

```bash
spirit-setup
```

The wizard walks you through:

- **Tier** — `Free` (local SQLite, no API key required, BYO strategy) or `Plus/Pro` (gateway-backed, custom indicators + scorer + risk-gate; get an API key at [portal.tradebot.live](https://portal.tradebot.live))
- **Instance name** — short kebab-case label (e.g. `test`, `prod`, `alice`). Becomes the path under `~/.spirit/<instance>/` for that instance's state.
- **Strategy** — pick `macd_demo` or `sma_crossover` from the bundled examples, or **Custom** to point at your own file under `~/.spirit/strategies/`.
- **Kraken API keys** — optional, only needed for live trading. Skip for paper mode.

The wizard writes `spirit.yaml` and `.env` files; their paths are printed at the end.

### 2. Sanity-check

```bash
spirit-preflight
```

All checks should PASS for Free tier (env vars + disk space). Plus/Pro adds gateway connectivity + capability checks.

### 3. Start trading

```bash
spirit --mode paper        # paper trading (recommended for first run)
spirit --mode test         # dry-run, no orders placed
spirit --mode live         # real money — only after paper validation
```

On a successful start you'll see a `[GREEN LIGHT]` line for each pair when the warmup buffer is ready (~30s on a fresh start), then `[SPIRIT] alive` periodic heartbeats every 30 minutes, plus strategy log lines when signals fire.

Stop with `Ctrl-C` for a graceful shutdown.

## Troubleshooting

**`error: externally-managed-environment`**
You ran `pip install` on Ubuntu 23.04+ / Debian 12+. Use pipx as shown above. Do **not** use `--break-system-packages` — it can collide with apt-managed Python libraries.

**`spirit: command not found` after install**
Run `pipx ensurepath`, then open a new terminal (or `exec bash`). If you used a venv directly, the binary is at `~/.spirit-venv/bin/spirit`.

**`pipx: command not found` on older Ubuntu (20.04 / 22.04)**
`pipx` isn't in those apt repos. Install with pip into the user site instead:
```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```
Then continue with `pipx install spirit-platform`.

**`No module named venv`**
On stripped-down Debian/Ubuntu images: `sudo apt install -y python3-venv`.

## Upgrading

```bash
pipx upgrade spirit-platform
```

## Uninstalling

```bash
pipx uninstall spirit-platform
```

This removes the `spirit*` commands and the isolated venv. Your config (`~/.spirit/`) and local data are kept. For a deeper clean — including config, local DB, and the systemd unit if you installed one — run `spirit-uninstall` **before** `pipx uninstall`.

---

Found a problem with these instructions? Open an issue at [github.com/timoz10/spirit-platform/issues](https://github.com/timoz10/spirit-platform/issues).
