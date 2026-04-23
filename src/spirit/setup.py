"""
Spirit First-Run Setup

Interactive CLI to configure a new Spirit instance. Writes spirit.yaml
and .env with the minimum config needed to run Spirit.

Usage:
    python3 -m spirit.setup
"""

import os
import sys


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    """Prompt for input with optional default."""
    if default:
        display = f"{label} [{default}]: "
    else:
        display = f"{label}: "
    if secret:
        import getpass
        try:
            val = getpass.getpass(display + "(input hidden) ")
        except (EOFError, OSError):
            # Fallback if getpass can't access /dev/tty
            val = input(display)
    else:
        val = input(display)
    return val.strip() or default


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


def _write_yaml(yaml_path: str, values: dict):
    """Write spirit.yaml config file."""
    lines = []
    if os.path.exists(yaml_path):
        with open(yaml_path, "r") as f:
            lines = f.readlines()

    # Update existing keys or append new ones
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

    # --- Step 1: API gateway ---
    print("--- Step 1: API Gateway ---")
    print("Spirit connects to the trading data API. You need an API key.")
    print()
    env_values = {}
    yaml_values = {}

    api_key = _prompt("Spirit API key", secret=True)
    print()
    print("API gateway:")
    print("  1. api.tradebot.live (default)")
    print("  2. Custom URL")
    print()
    gw_choice = _prompt("Gateway [1/2]", "1")
    if gw_choice == "2":
        api_url = _prompt("Custom gateway URL")
    else:
        api_url = "https://api.tradebot.live/v1"

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
        instance = _prompt("Instance name (e.g. prod, canary, davy)", "prod")

    yaml_values["SPIRIT_API_URL"] = api_url
    yaml_values["SPIRIT_INSTANCE"] = instance
    env_values["SPIRIT_API_KEY"] = api_key

    print()

    # --- Step 2: Exchange ---
    print("--- Step 2: Exchange ---")
    print("Which exchange does this instance trade on?")
    print("  1. Kraken")
    print("  2. None (paper mode only, no exchange keys needed)")
    print()
    exchange_choice = _prompt("Exchange [1/2]", "1")

    if exchange_choice == "1":
        yaml_values["SPIRIT_EXCHANGE"] = "kraken"
        print()
        print("Exchange API credentials (leave blank to skip for paper mode):")
        ex_key = _prompt("Exchange API key (EXCHANGE_API_KEY)", secret=True)
        ex_secret = _prompt("Exchange API secret (EXCHANGE_API_SECRET)", secret=True)
        if ex_key:
            env_values["EXCHANGE_API_KEY"] = ex_key
        if ex_secret:
            env_values["EXCHANGE_API_SECRET"] = ex_secret
    else:
        yaml_values["SPIRIT_EXCHANGE"] = "none"

    # Pair selection — explicit prompt (don't silently default to 1 pair, see #396)
    print()
    print("Trading pairs (which markets should Spirit watch?):")
    print("  Available: XBTUSD, ETHUSD, SOLUSD, ATOMUSD, INJUSD, FETUSD, JUPUSD")
    print("  Suggested: XBTUSD,ETHUSD,SOLUSD  (3 pairs is a reasonable starting set)")
    pairs_in = _prompt("Pairs (comma-separated)", default="XBTUSD,ETHUSD,SOLUSD")
    yaml_values["SPIRIT_PAIRS"] = f'"{pairs_in}"'

    # Strategy + mode defaults — safe out of the box
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
    print("  Strategy: zone_bounce   (or write your own — see docs/reference/platform/WRITING_A_STRATEGY.md)")
    print("  Mode:     paper          (use --mode live when ready)")
    print()
    print("To start Spirit:")
    print(f"  cd {project_root}")
    print(f"  set -a && source .env && set +a")
    print(f"  PYTHONPATH=src python3 -m spirit.main --mode paper --no-pause")
    print()


if __name__ == "__main__":
    main()
