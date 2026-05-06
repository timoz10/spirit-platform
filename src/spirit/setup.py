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

    print()
    print("--- Free tier ---")
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
    instance = _ask_text("Instance name (e.g. local, alpha)", "local")
    yaml_values["SPIRIT_INSTANCE"] = instance

    default_db = os.path.expanduser(f"~/.spirit/{instance}/spirit.db")
    sqlite_path = _ask_text("SQLite database path", default_db)
    if sqlite_path != default_db:
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
        default_value="kraken",
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
    pairs_in = _ask_text("Pairs (comma-separated)", "XBTUSD,ETHUSD")
    yaml_values["SPIRIT_PAIRS"] = f'"{pairs_in}"'

    # --- Strategy ---
    print()
    strat_choice = _ask_select(
        "Strategy",
        choices=[
            ("sma_crossover",
             "sma_crossover — bundled example, paper-mode-by-default"),
            ("custom",
             "Custom — drop your own under ~/.spirit/strategies/"),
        ],
        default_value="sma_crossover",
    )
    if strat_choice == "custom":
        strat_name = _ask_text("Strategy module name (without .py)")
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
        yaml_values["SPIRIT_STRATEGY"] = "sma_crossover"

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
# Plus / Pro path
# ---------------------------------------------------------------------------


def _setup_paid_tier(project_root, env_path, yaml_path, yaml_dir):
    """Plus / Pro branch — gateway-backed, requires a TradeBOT API key."""
    env_values: dict = {}
    yaml_values: dict = {}

    print()
    print("--- Plus / Pro tier ---")
    print("Spirit connects to api.tradebot.live for D-Limit zones,")
    print("scorer outputs, and risk-gate calibration. You need a key.")
    print()

    api_key = _ask_password("Spirit API key")

    print()
    api_url_choice = _ask_select(
        "API gateway URL",
        choices=[
            ("https://api.tradebot.live/v1", "api.tradebot.live (default)"),
            ("custom", "Custom URL"),
        ],
        default_value="https://api.tradebot.live/v1",
    )
    if api_url_choice == "custom":
        api_url = _ask_text("Custom gateway URL")
    else:
        api_url = api_url_choice

    # Auto-detect instance name from API key via /whoami
    instance = None
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
                print(f"  Authenticated: {key_name} (instance: {instance})")
        except Exception as e:
            print(f"  Could not reach gateway: {e}")

    if not instance:
        instance = _ask_text("Instance name (e.g. prod, canary, davy)", "prod")

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
        default_value="kraken",
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
    pairs_in = _ask_text("Pairs (comma-separated)", "XBTUSD,ETHUSD,SOLUSD")
    yaml_values["SPIRIT_PAIRS"] = f'"{pairs_in}"'

    yaml_values["SPIRIT_STRATEGY"] = "zone_bounce"
    yaml_values["SPIRIT_MODE"] = "paper"

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
