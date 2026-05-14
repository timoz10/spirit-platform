"""
Spirit First-Run Setup

Interactive CLI to configure a new Spirit instance. Writes spirit.yaml
and .env with the minimum config needed to run Spirit.

Uses `questionary` for arrow-key menus + masked password entry when run
on a TTY. Falls back to plain `input()` when stdin/stdout aren't tty —
this preserves the ability to drive the wizard non-interactively from a
shell pipeline (`printf '1\\n\\n…' | python3 -m spirit.setup`), which
the install/smoke-test scripts depend on.

Usage:
    python3 -m spirit.setup
"""

from __future__ import annotations

import os
import sys
from typing import Sequence


# ---------------------------------------------------------------------------
# TTY detection + prompt helpers
# ---------------------------------------------------------------------------

# `questionary` requires a TTY for its arrow-key UI. When stdin is piped
# (CI, smoke scripts, automated provisioning) we fall back to plain
# `input()` so the wizard stays scriptable. Detection is once-at-import
# so prompts behave consistently within a single run.
_INTERACTIVE = sys.stdin.isatty() and sys.stdout.isatty()


def _ask_text(label: str, default: str = "") -> str:
    """Prompt for a text answer with optional default."""
    if _INTERACTIVE:
        import questionary
        ans = questionary.text(
            label,
            default=default or "",
        ).ask()
        # `ask()` returns None on Ctrl-C; treat as user abort.
        if ans is None:
            sys.exit(1)
        return ans.strip() or default
    # Fallback — plain input(), default-on-blank.
    display = f"{label} [{default}]: " if default else f"{label}: "
    return (input(display).strip()) or default


def _ask_password(label: str) -> str:
    """Prompt for a secret with masked input. Empty answer is allowed
    (caller decides whether the value is required)."""
    if _INTERACTIVE:
        import questionary
        ans = questionary.password(label).ask()
        if ans is None:
            sys.exit(1)
        return ans.strip()
    # Fallback — getpass when stdin is a real tty (rare in non-interactive
    # mode but possible in some CI shells); plain input() otherwise.
    import getpass
    display = f"{label}: "
    try:
        return getpass.getpass(display + "(input hidden) ").strip()
    except (EOFError, OSError):
        return input(display).strip()


def _ask_select(label: str, choices: Sequence[tuple[str, str]],
                default_value: str | None = None) -> str:
    """Prompt for a single choice from `choices` (list of (value, display)).

    Returns the chosen `value`. In non-interactive mode the user types the
    value directly (or hits enter for the default) — same protocol the
    old setup wizard used, so existing scripted-stdin flows still work.
    """
    if _INTERACTIVE:
        import questionary
        # questionary.Choice carries title (shown) + value (returned).
        q_choices = [
            questionary.Choice(title=display, value=value)
            for value, display in choices
        ]
        # questionary 2.x matches `default` against each Choice's
        # `value`, NOT its `title`. Passing the title raises ValueError
        # at runtime — and unit tests miss it because the questionary
        # path only fires on a TTY. Pass the value directly.
        valid_values = {value for value, _ in choices}
        default = default_value if default_value in valid_values else None
        ans = questionary.select(
            label,
            choices=q_choices,
            default=default,
        ).ask()
        if ans is None:
            sys.exit(1)
        return ans
    # Fallback — show numbered list, ask for the number, default on blank.
    print()
    print(label)
    valid_values: list[str] = []
    default_idx_display = ""
    for i, (value, display) in enumerate(choices, 1):
        marker = ""
        if value == default_value:
            marker = "  (default)"
            default_idx_display = str(i)
        print(f"  {i}. {display}{marker}")
        valid_values.append(value)
    while True:
        prompt = (f"Choice [1-{len(choices)}]"
                  + (f" [{default_idx_display}]" if default_idx_display else "")
                  + ": ")
        raw = input(prompt).strip()
        if not raw and default_idx_display:
            return valid_values[int(default_idx_display) - 1]
        try:
            idx = int(raw)
            if 1 <= idx <= len(choices):
                return valid_values[idx - 1]
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(choices)}.")


# ---------------------------------------------------------------------------
# Config writers (unchanged from previous setup.py — still produce the same
# spirit.yaml + .env shape consumers expect)
# ---------------------------------------------------------------------------


def _write_env(env_path: str, values: dict):
    """Write or update .env file with key=value pairs."""
    existing = {}
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()

    existing.update(values)

    with open(env_path, "w") as f:
        for k, v in existing.items():
            f.write(f"{k}={v}\n")


def _remove_yaml_keys(yaml_path: str, keys: Sequence[str]) -> None:
    """Strip keys from spirit.yaml in place. Used by the Free path to
    suppress Plus/Pro-only keys (e.g. SPIRIT_API_URL) inherited from a
    committed default template that doesn't apply to a Free install."""
    if not os.path.exists(yaml_path):
        return
    target = set(keys)
    with open(yaml_path, "r") as f:
        lines = f.readlines()
    kept = []
    for line in lines:
        stripped = line.strip()
        if ":" in stripped and not stripped.startswith("#"):
            key = stripped.split(":", 1)[0].strip()
            if key in target:
                continue
        kept.append(line)
    with open(yaml_path, "w") as f:
        f.writelines(kept)


def _load_existing_yaml(yaml_path: str) -> dict:
    """Return current values from an existing spirit.yaml as a dict.

    Used by the wizard to pre-populate prompts on a re-run — without
    this, every prompt's default reverts to the hardcoded value, and
    a user who hits Enter expecting to keep their previous choice
    silently overwrites it. Trap caught in 2026-05-14 VM testing
    when a user re-ran the wizard 3x and saw their instance name
    flip from `test-vm` back to `local`.

    Line-based parser (no yaml dependency) to match `_write_yaml`'s
    style and to tolerate user-added comments. Returns {} if the file
    doesn't exist (first-run).
    """
    if not os.path.exists(yaml_path):
        return {}
    values: dict = {}
    try:
        with open(yaml_path, "r") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if ":" not in stripped:
                    continue
                key, _, val = stripped.partition(":")
                key = key.strip()
                val = val.strip()
                # Strip surrounding double or single quotes — the writer
                # adds them for values with commas (e.g. SPIRIT_PAIRS).
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                values[key] = val
    except OSError:
        return {}
    return values


def _write_yaml(yaml_path: str, values: dict):
    """Write spirit.yaml config file."""
    lines = []
    if os.path.exists(yaml_path):
        with open(yaml_path, "r") as f:
            lines = f.readlines()

    existing_keys = set()
    updated_lines = []
    for line in lines:
        stripped = line.strip()
        if ":" in stripped and not stripped.startswith("#"):
            key = stripped.split(":")[0].strip()
            if key in values:
                updated_lines.append(f"{key}: {values[key]}\n")
                existing_keys.add(key)
                continue
        updated_lines.append(line)

    for k, v in values.items():
        if k not in existing_keys:
            updated_lines.append(f"{k}: {v}\n")

    with open(yaml_path, "w") as f:
        f.writelines(updated_lines)


# ---------------------------------------------------------------------------
# Free tier path
# ---------------------------------------------------------------------------


def _setup_free_tier(project_root, env_path, yaml_path, yaml_dir):
    """Free-tier branch — local SQLite + direct exchange OHLC.

    Writes:
      SPIRIT_TIER=free, SPIRIT_INSTANCE, SPIRIT_SQLITE_PATH (optional),
      SPIRIT_PAIRS, SPIRIT_STRATEGY, SPIRIT_MODE.
      EXCHANGE_API_KEY/SECRET if user provides them.
      SPIRIT_API_KEY if user provides one (optional warm-on-ramp; unused
      at v2.2.x but lights up upgrade-path features automatically when
      the same key gets a Plus/Pro tier server-side).
    """
    env_values: dict = {}
    yaml_values: dict = {"SPIRIT_TIER": "free"}

    # Load existing yaml so re-runs offer the user's previous answers
    # as defaults. Without this, every prompt reverts to the hardcoded
    # default, and a user who hits Enter expecting to keep their last
    # choice silently overwrites it. See #700 for the migration UX
    # followup; this is the minimum-viable fix.
    existing = _load_existing_yaml(yaml_path)
    is_rerun = bool(existing)

    print()
    print("--- Free tier ---")
    if is_rerun:
        print(f"  Re-run detected — values from {os.path.basename(yaml_path)}")
        print("  are offered as the default at each prompt. Hit Enter to keep,")
        print("  or type a new value to change.")
        print()
    print("Free-tier Spirit runs entirely on your machine:")
    print("  - Reads OHLC directly from the exchange (Kraken at launch)")
    print("  - Stores trade outcomes + state in a local SQLite file")
    print("  - No telemetry, no phone-home, no Spirit-gateway calls")
    print()
    print("Bring your own strategy or use the bundled SMA example.")
    print("D-Limit zones, scorer, and risk-gate calibration are NOT")
    print("included on Free — those are Plus / Pro features.")
    print()

    # --- Instance name + SQLite path ---
    instance = _ask_text(
        "Instance name (e.g. local, alpha)",
        existing.get("SPIRIT_INSTANCE", "local"),
    )
    yaml_values["SPIRIT_INSTANCE"] = instance

    # Default DB path follows the instance name unless the user previously
    # set an explicit SPIRIT_SQLITE_PATH override.
    default_db = existing.get("SPIRIT_SQLITE_PATH") or os.path.expanduser(
        f"~/.spirit/{instance}/spirit.db"
    )
    sqlite_path = _ask_text("SQLite database path", default_db)
    # Compare against the *instance-derived* default — explicit overrides
    # land in SPIRIT_SQLITE_PATH, instance-derived defaults don't.
    instance_default = os.path.expanduser(f"~/.spirit/{instance}/spirit.db")
    if sqlite_path != instance_default:
        yaml_values["SPIRIT_SQLITE_PATH"] = sqlite_path

    # --- Optional TradeBOT API key (warm-on-ramp to paid) ---
    print()
    print("Optional: TradeBOT API key")
    print("  Sign up at https://tradebot.live for a free account + key.")
    print("  Stored locally now, unused until you upgrade. Adding one")
    print("  means a future Plus / Pro upgrade lights up automatically")
    print("  with the same key — no re-onboarding.")
    print("  Leave blank to skip — Free works fully without one.")
    api_key = _ask_password("TradeBOT API key (optional)")
    if api_key:
        env_values["SPIRIT_API_KEY"] = api_key

    # --- Exchange + credentials ---
    print()
    exchange = _ask_select(
        "Which exchange do you trade on?",
        choices=[
            ("kraken", "Kraken — bundled, only option at v2.2.x"),
        ],
        default_value=existing.get("SPIRIT_EXCHANGE", "kraken"),
    )
    yaml_values["SPIRIT_EXCHANGE"] = exchange

    print()
    print("Exchange API credentials:")
    print("  - Required for live trading.")
    print("  - Leave blank for paper-mode-only — Spirit reads public")
    print("    OHLC and runs paper strategies without holding a key.")
    ex_key = _ask_password("Exchange API key (optional for paper)")
    ex_secret = _ask_password("Exchange API secret (optional for paper)")
    if ex_key:
        env_values["EXCHANGE_API_KEY"] = ex_key
    if ex_secret:
        env_values["EXCHANGE_API_SECRET"] = ex_secret

    # --- Pairs ---
    print()
    print("Trading pairs — which markets should Spirit watch?")
    print("  Bundled: XBTUSD, ETHUSD, SOLUSD, ATOMUSD")
    pairs_in = _ask_text(
        "Pairs (comma-separated)",
        existing.get("SPIRIT_PAIRS", "XBTUSD,ETHUSD"),
    )
    yaml_values["SPIRIT_PAIRS"] = f'"{pairs_in}"'

    # --- Strategy ---
    print()
    # Map existing yaml value to a menu choice. Custom strategies (anything
    # not in the built-in set) land on "custom" — the prompt will then ask
    # for the module name, defaulting to the existing value.
    prior_strategy = existing.get("SPIRIT_STRATEGY", "sma_crossover")
    if prior_strategy in ("sma_crossover", "macd_demo"):
        strategy_default = prior_strategy
    elif prior_strategy:
        strategy_default = "custom"
    else:
        strategy_default = "sma_crossover"
    strat_choice = _ask_select(
        "Strategy",
        choices=[
            ("sma_crossover",
             "sma_crossover — minimal example, paper-mode-by-default"),
            ("macd_demo",
             "macd_demo — full-stack example (MACD cross + RSI/trend filters + ATR stop)"),
            ("custom",
             "Custom — drop your own under ~/.spirit/strategies/"),
        ],
        default_value=strategy_default,
    )
    if strat_choice == "custom":
        # If the prior yaml had a custom name (anything not in the built-in
        # set), offer it as the default so re-runs don't lose the value.
        custom_default = prior_strategy if prior_strategy not in (
            "sma_crossover", "macd_demo", ""
        ) else ""
        strat_name = _ask_text("Strategy module name (without .py)",
                               custom_default)
        # Strip a trailing `.py` if the user typed one — strategy_config
        # matches by module name, not filename. Also strip any path
        # components ("strategies/foo.py" → "foo").
        strat_name = os.path.basename(strat_name).rsplit(".py", 1)[0].strip()
        yaml_values["SPIRIT_STRATEGY"] = strat_name
        # Sanity warn — registry alone won't have it, and an empty user
        # dir would cause a startup MONITOR-ONLY mode rather than a
        # crash. Just point the user at next steps.
        user_dir = os.path.expanduser("~/.spirit/strategies")
        user_file = os.path.join(user_dir, f"{strat_name}.py")
        if not os.path.exists(user_file):
            print()
            print(f"  Note: {user_file} does not exist yet.")
            print(f"  Create the file before starting Spirit, or pick a")
            print(f"  built-in strategy by re-running setup.")
    else:
        # Built-in selected — write whatever the user picked verbatim.
        yaml_values["SPIRIT_STRATEGY"] = strat_choice

    yaml_values["SPIRIT_MODE"] = "paper"

    # --- Write config ---
    print()
    print("--- Writing Configuration ---")
    os.makedirs(yaml_dir, exist_ok=True)
    _write_yaml(yaml_path, yaml_values)
    # Strip Plus/Pro-only keys inherited from the committed default
    # spirit.yaml (e.g. SPIRIT_API_URL pointing at the internal gateway).
    # Free doesn't talk to the gateway; leaving these in is noise at
    # best and confusing-prod-IP-leak at worst.
    _remove_yaml_keys(yaml_path, ["SPIRIT_API_URL"])
    print(f"  Written: {yaml_path}")
    if env_values:
        _write_env(env_path, env_values)
        print(f"  Written: {env_path}")

    # --- Done ---
    print()
    print("=" * 60)
    print("  Free-tier setup complete!")
    print("=" * 60)
    print()
    print("Defaults applied:")
    print(f"  Tier:     free")
    print(f"  Pairs:    {pairs_in}")
    print(f"  Strategy: {yaml_values['SPIRIT_STRATEGY']}")
    print("  Mode:     paper  (use --mode live when ready, after testing)")
    print()
    print("Local data:")
    print(f"  SQLite:   {sqlite_path}")
    print()
    print("Upgrade path:")
    print("  Plus / Pro tiers add D-Limit zones, scorer, and risk-gate")
    print("  calibration via the API gateway. See https://tradebot.live/upgrade.")
    print()


# ---------------------------------------------------------------------------
# BYOD OHLC backfill helper (#666 sub-task 9b)
# ---------------------------------------------------------------------------


def _offer_csv_backfill(api_url: str, api_key: str, pairs_csv: str) -> None:
    """Prompt the user for an optional Kraken CSV backfill and run it.

    Pulled out of `_setup_paid_tier` so it can be invoked standalone
    (e.g. from a `spirit backfill` follow-up subcommand later) and
    unit-tested without driving the whole wizard.

    Behaviour:
      - Asks: "Do you have a Kraken historical CSV you'd like to import?"
      - If yes: prompts for CSV path + pair + interval, then streams the
        file in 50k-row chunks via ApiDataProvider.upload_user_ohlc.
        Per-chunk progress is printed.
      - If the user enters an invalid path or empty input, we print a
        gentle skip message — never raise.
    """
    print()
    print("--- BYOD OHLC backfill ---")
    print("Spirit can bulk-import historical OHLC from a Kraken CSV export")
    print("so trajectory recovery + backtests have data on day one.")
    print()
    print("Get a CSV from: https://support.kraken.com/hc/en-us/articles/")
    print("                  360047124832")
    print()
    print("Skip this step if you don't have a CSV yet — Spirit will still")
    print("run, and your cloud OHLC will accumulate from the live exchange")
    print("via incremental pushes.")
    print()

    if _INTERACTIVE:
        import questionary
        do_import = questionary.confirm(
            "Import a Kraken CSV now?", default=False,
        ).ask()
    else:
        raw = input("Import a Kraken CSV now? [y/N]: ").strip().lower()
        do_import = raw in ("y", "yes")
    if not do_import:
        print("  Skipped. Re-run later without restarting Spirit:")
        print("    python3 -m spirit.backfill /path/to/XBTUSDT_60.csv")
        return

    csv_path = _ask_text("Path to Kraken CSV (e.g. ~/Downloads/XBTUSD_1.csv)")
    if not csv_path:
        print("  No path given — skipping import.")
        print("  Run later: python3 -m spirit.backfill /path/to/file.csv")
        return

    csv_path = os.path.expanduser(csv_path)
    if not os.path.exists(csv_path):
        print(f"  File not found: {csv_path}")
        print("  Run later when the file is in place:")
        print("    python3 -m spirit.backfill /path/to/file.csv")
        return

    # Default pair = first one from SPIRIT_PAIRS; user can override.
    default_pair = (pairs_csv or "XBTUSD").split(",")[0].strip().upper()
    pair = _ask_text("Pair this CSV covers", default_pair).strip().upper()
    interval = _ask_select(
        "Interval (minutes)",
        choices=[
            ("1", "1 — minute candles"),
            ("5", "5 — 5-minute"),
            ("15", "15 — 15-minute"),
            ("60", "60 — hourly"),
        ],
        default_value="1",
    )

    # Lazy imports — keep module-load cost flat for users who never run setup.
    from spirit.utils.api_data_provider import ApiDataProvider
    from spirit.utils.kraken_csv import iter_kraken_csv_chunks

    provider = ApiDataProvider(base_url=api_url, api_key=api_key)
    chunks_uploaded = 0
    total_inserted = 0
    total_skipped = 0
    print()
    print(f"  Streaming {csv_path} as ({pair}, {interval}m) ...")
    try:
        for chunk, stats in iter_kraken_csv_chunks(csv_path, chunk_size=50_000):
            if not chunk:
                continue
            resp = provider.upload_user_ohlc(pair, int(interval), chunk)
            chunks_uploaded += 1
            total_inserted += resp.get("rows_inserted", 0)
            total_skipped += resp.get("rows_skipped", 0)
            print(
                f"    chunk {chunks_uploaded}: "
                f"sent={len(chunk)} "
                f"inserted={resp.get('rows_inserted', 0)} "
                f"skipped={resp.get('rows_skipped', 0)} "
                f"(running total: parsed={stats.rows_parsed})"
            )
        print()
        print(f"  Upload complete: {chunks_uploaded} chunks, "
              f"{total_inserted} new rows, {total_skipped} duplicates.")
        if stats.rows_skipped:
            print(f"  ({stats.rows_skipped} malformed rows skipped — "
                  f"reasons: {dict(stats.skip_reasons)})")
    except Exception as e:
        print(f"  Upload failed mid-stream: {e}")
        print(f"  Partial: {chunks_uploaded} chunks landed before the error.")
        print("  Re-run setup to retry; already-uploaded candles dedupe.")


# ---------------------------------------------------------------------------
# Plus / Pro path
# ---------------------------------------------------------------------------


def _setup_paid_tier(project_root, env_path, yaml_path, yaml_dir):
    """Plus / Pro branch — gateway-backed, requires a TradeBOT API key."""
    env_values: dict = {}
    yaml_values: dict = {}

    existing = _load_existing_yaml(yaml_path)
    is_rerun = bool(existing)

    print()
    print("--- Plus / Pro tier ---")
    if is_rerun:
        print(f"  Re-run detected — values from {os.path.basename(yaml_path)}")
        print("  are offered as the default at each prompt. Hit Enter to keep,")
        print("  or type a new value to change.")
        print()
    print("Spirit connects to api.tradebot.live for D-Limit zones,")
    print("scorer outputs, and risk-gate calibration. You need a key.")
    print()

    api_key = _ask_password("Spirit API key")

    print()
    prior_api_url = existing.get("SPIRIT_API_URL", "")
    if prior_api_url == "https://api.tradebot.live/v1" or not prior_api_url:
        api_url_default = "https://api.tradebot.live/v1"
    else:
        api_url_default = "custom"
    api_url_choice = _ask_select(
        "API gateway URL",
        choices=[
            ("https://api.tradebot.live/v1", "api.tradebot.live (default)"),
            ("custom", "Custom URL"),
        ],
        default_value=api_url_default,
    )
    if api_url_choice == "custom":
        api_url = _ask_text("Custom gateway URL",
                            prior_api_url if api_url_default == "custom" else "")
    else:
        api_url = api_url_choice

    # Auto-detect instance name from API key via /whoami
    instance = None
    capabilities: list[str] = []
    if api_key:
        try:
            import json
            import urllib.request
            req = urllib.request.Request(
                f"{api_url}/whoami",
                headers={
                    "X-API-Key": api_key,
                    "User-Agent": "Spirit-Setup/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                instance = data.get("instance")
                key_name = data.get("name", "")
                capabilities = list(data.get("capabilities", []))
                print(f"  Authenticated: {key_name} (instance: {instance})")
        except Exception as e:
            print(f"  Could not reach gateway: {e}")

    if not instance:
        instance = _ask_text(
            "Instance name (e.g. prod, canary, davy)",
            existing.get("SPIRIT_INSTANCE", "prod"),
        )

    yaml_values["SPIRIT_API_URL"] = api_url
    yaml_values["SPIRIT_INSTANCE"] = instance
    env_values["SPIRIT_API_KEY"] = api_key

    # --- Exchange ---
    print()
    exchange = _ask_select(
        "Which exchange does this instance trade on?",
        choices=[
            ("kraken", "Kraken"),
            ("none", "None — paper mode only, no exchange keys needed"),
        ],
        default_value=existing.get("SPIRIT_EXCHANGE", "kraken"),
    )

    if exchange == "kraken":
        yaml_values["SPIRIT_EXCHANGE"] = "kraken"
        print()
        print("Exchange API credentials (leave blank to skip for paper mode):")
        ex_key = _ask_password("Exchange API key (EXCHANGE_API_KEY)")
        ex_secret = _ask_password("Exchange API secret (EXCHANGE_API_SECRET)")
        if ex_key:
            env_values["EXCHANGE_API_KEY"] = ex_key
        if ex_secret:
            env_values["EXCHANGE_API_SECRET"] = ex_secret
    else:
        yaml_values["SPIRIT_EXCHANGE"] = "none"

    # --- Pairs ---
    print()
    print("Trading pairs — which markets should Spirit watch?")
    print("  Available: XBTUSD, ETHUSD, SOLUSD, ATOMUSD, INJUSD, FETUSD, JUPUSD")
    print("  Suggested: XBTUSD,ETHUSD,SOLUSD  (3 pairs is a sensible start)")
    pairs_in = _ask_text(
        "Pairs (comma-separated)",
        existing.get("SPIRIT_PAIRS", "XBTUSD,ETHUSD,SOLUSD"),
    )
    yaml_values["SPIRIT_PAIRS"] = f'"{pairs_in}"'

    yaml_values["SPIRIT_STRATEGY"] = "zone_bounce"
    yaml_values["SPIRIT_MODE"] = "paper"

    # --- BYOD OHLC backfill (#666 sub-task 9b) ---
    # Only offer when the key actually has write:ohlc_user. Plus / Pro
    # post-#665 always do; internal_canary / admin do too. Skipping
    # cleanly when the cap is absent means a free key that's already
    # past the tier picker (rare but possible via custom config) doesn't
    # see a confusing dead-end prompt.
    if "write:ohlc_user" in capabilities:
        _offer_csv_backfill(api_url, api_key, pairs_in)
    else:
        print()
        print("--- BYOD OHLC backfill ---")
        print("  Skipped — your key doesn't carry write:ohlc_user.")
        print("  (Plus, Pro, and internal_canary keys offer historical")
        print("   import here; free + admin-only keys don't.)")

    # --- Write config ---
    print()
    print("--- Writing Configuration ---")

    os.makedirs(yaml_dir, exist_ok=True)
    _write_yaml(yaml_path, yaml_values)
    print(f"  Written: {yaml_path}")

    _write_env(env_path, env_values)
    print(f"  Written: {env_path}")

    print()
    print("=" * 60)
    print("  Setup complete!")
    print("=" * 60)
    print()
    print("Defaults applied:")
    print(f"  Pairs:    {pairs_in}")
    print("  Strategy: zone_bounce   (or write your own — see")
    print("            docs/reference/platform/WRITING_A_STRATEGY.md)")
    print("  Mode:     paper          (use --mode live when ready)")
    print()
    print("To start Spirit:")
    print(f"  cd {project_root}")
    print(f"  [ -f .env ] && set -a && source .env && set +a")
    print(f"  PYTHONPATH=src python3 -m spirit.main --mode paper --no-pause")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    print()
    print("=" * 60)
    print("  Spirit First-Run Setup")
    print("=" * 60)
    print()
    print("This wizard configures Spirit for first use.")
    print("It writes config/spirit.yaml and .env in the project root.")
    print()

    # Find project root (where config/ and .env live)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(script_dir, "..", ".."))
    env_path = os.path.join(project_root, ".env")
    yaml_dir = os.path.join(project_root, "config")
    yaml_path = os.path.join(yaml_dir, "spirit.yaml")

    print(f"Project root: {project_root}")
    print()

    # --- Tier selection ---
    tier = _ask_select(
        "Which Spirit tier are you setting up?",
        choices=[
            ("free",
             "Free       — local SQLite, no API key required, BYO strategy"),
            ("paid",
             "Plus / Pro — gateway-backed, D-Limit + scorer + risk-gate"),
        ],
        default_value="paid",
    )
    if tier == "free":
        return _setup_free_tier(project_root, env_path, yaml_path, yaml_dir)
    return _setup_paid_tier(project_root, env_path, yaml_path, yaml_dir)


if __name__ == "__main__":
    main()
