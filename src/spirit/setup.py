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
        val = getpass.getpass(display)
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

    # --- Step 1: Data source ---
    print("--- Step 1: Data Source ---")
    print("How does this Spirit instance get market data?")
    print("  1. API gateway (subscriber mode — needs SPIRIT_API_KEY)")
    print("  2. Direct PostgreSQL (developer mode — needs PG credentials)")
    print()
    data_mode = _prompt("Data source [1/2]", "1")
    is_api_mode = data_mode == "1"

    env_values = {}
    yaml_values = {}

    if is_api_mode:
        api_key = _prompt("Spirit API key", secret=True)
        api_url = _prompt("API gateway URL", "https://api.tradebot.live/v1")
        instance = _prompt("Instance name (e.g. prod, canary, davy)", "prod")

        yaml_values["SPIRIT_DATA_PROVIDER"] = "api"
        yaml_values["SPIRIT_API_URL"] = api_url
        yaml_values["SPIRIT_INSTANCE"] = instance
        env_values["SPIRIT_API_KEY"] = api_key
    else:
        pg_host = _prompt("PostgreSQL host")
        pg_user = _prompt("PostgreSQL user", "botuser")
        pg_pass = _prompt("PostgreSQL password", secret=True)
        pg_db = _prompt("PostgreSQL database", "trading_bot")
        instance = _prompt("Instance name", "dev")

        env_values["POSTGRES_HOST"] = pg_host
        env_values["POSTGRES_USER"] = pg_user
        env_values["POSTGRES_PASSWORD"] = pg_pass
        env_values["POSTGRES_DB"] = pg_db
        yaml_values["SPIRIT_DATA_PROVIDER"] = "pg"
        yaml_values["SPIRIT_INSTANCE"] = instance

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

    print()

    # --- Step 3: Trading pairs ---
    print("--- Step 3: Trading Pairs ---")
    pairs = _prompt("Trading pairs (comma-separated)", "XBTUSD")
    yaml_values["SPIRIT_PAIRS"] = f'"{pairs}"'

    # --- Step 4: Strategy ---
    yaml_values["SPIRIT_STRATEGY"] = "zone_bounce"

    print()

    # --- Step 5: Trading mode ---
    print("--- Step 4: Trading Mode ---")
    print("  1. Paper (simulated trading, no real orders)")
    print("  2. Live (real orders on exchange)")
    print()
    mode_choice = _prompt("Mode [1/2]", "1")
    yaml_values["SPIRIT_MODE"] = "live" if mode_choice == "2" else "paper"

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
    print("To start Spirit:")
    print(f"  cd {project_root}")
    print(f"  set -a && source .env && set +a")
    if is_api_mode:
        print(f"  PYTHONPATH=src python3 -m spirit.main --mode {yaml_values['SPIRIT_MODE']} --no-pause")
    else:
        print(f"  PYTHONPATH=src python3 -m spirit.main --mode {yaml_values['SPIRIT_MODE']}")
    print()


if __name__ == "__main__":
    main()
