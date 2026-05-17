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

```bash
python3 -m spirit.setup        # interactive wizard
spirit --mode paper            # start paper trading
```

The wizard asks your tier (Free / Plus / Pro), your instance name, and an optional Kraken API key for live trading.

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
