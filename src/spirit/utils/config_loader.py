"""
Config loader with resolution order: env var -> per-instance YAML -> default.

Separates operational config (per-instance YAML) from secrets (env-only).


Resolution contract (see docs/reference/MODULE_CONTRACTS.md)
============================================================

YAML is resolved from **exactly one** location:

    ~/.spirit/$SPIRIT_INSTANCE/spirit.yaml

If `SPIRIT_INSTANCE` is not in the environment, no YAML is loaded —
`get_config()` falls through to the hardcoded default. There is **no**
filesystem-search fallback, no "walk up from this file's location"
heuristic, and no system-wide config path.

This is intentional. Pre-#733 the loader walked up parent directories
from `__file__` looking for a `config/spirit.yaml`, which:

  1. Silently picked up `<repo>/config/spirit.yaml` in dev (correct), but
  2. Silently picked up `<pipx-venv>/lib/python3.12/config/spirit.yaml` on
     pipx installs — a file written by an earlier buggy spirit-setup —
     so every freshly-installed Spirit on every user's box pre-loaded
     someone else's defaults and reported "configured" from `spirit-preflight`.

The new contract eliminates the search. The only way for `get_config()`
to return a YAML value is for the user (or `spirit-setup`) to have
explicitly written that key under `~/.spirit/$SPIRIT_INSTANCE/spirit.yaml`,
with `SPIRIT_INSTANCE` set in the environment. No exceptions.

For source-repo development (where the legacy `<repo>/config/spirit.yaml`
flow was useful) set `SPIRIT_INSTANCE=dev` and write the file at
`~/.spirit/dev/spirit.yaml`. Or set the env vars directly — they always win.
"""

import os
from typing import Optional

import yaml

_config_cache: Optional[dict] = None
_cache_key: Optional[str] = None   # the resolved YAML path the cache was built from
_stale_check_done = False          # one-shot guard for the legacy-file warning


def resolve_active_instance() -> Optional[str]:
    """Return the active instance name, with single-instance autodetect.

    Resolution order:

      1. `SPIRIT_INSTANCE` env var (any value, even one that doesn't
         match an existing directory — explicit user intent).
      2. If env is unset and **exactly one** non-hidden directory lives
         under `~/.spirit/`, return that directory's name (autodetect
         for the common single-instance case).
      3. Otherwise None (no instance, or ambiguous multi-instance state).

    Used by `_resolve_yaml_path()` here and by `spirit.logger` for its
    display prefix, so a freshly-set-up single-instance box "just works"
    without the user having to source a `.env` first.
    """
    explicit = os.environ.get("SPIRIT_INSTANCE", "").strip()
    if explicit:
        return explicit
    spirit_root = os.path.join(os.path.expanduser("~"), ".spirit")
    try:
        candidates = [
            d for d in os.listdir(spirit_root)
            if not d.startswith(".") and os.path.isdir(os.path.join(spirit_root, d))
        ]
    except (FileNotFoundError, NotADirectoryError):
        return None
    if len(candidates) == 1:
        return candidates[0]
    # 0 or 2+ → no single answer, caller falls through to defaults.
    return None


def _resolve_yaml_path() -> Optional[str]:
    """Return the per-instance spirit.yaml path, or None when no instance.

    Resolves the active instance via `resolve_active_instance()` (env var
    or single-directory autodetect), then maps it to
    `~/.spirit/<instance>/spirit.yaml`.
    """
    instance = resolve_active_instance()
    if not instance:
        return None
    return os.path.join(os.path.expanduser("~"), ".spirit", instance, "spirit.yaml")


def _resolve_env_path() -> Optional[str]:
    """Return the per-instance .env path, or None when no instance."""
    instance = resolve_active_instance()
    if not instance:
        return None
    return os.path.join(os.path.expanduser("~"), ".spirit", instance, ".env")


_env_file_loaded = False


def _load_env_file() -> None:
    """Read `~/.spirit/<instance>/.env` once per process into `os.environ`.

    The setup wizard writes secrets (SPIRIT_API_KEY, EXCHANGE_API_KEY, etc.)
    here on the assumption that spirit's diagnostic and runtime CLIs pick
    them up automatically — which they didn't pre-rc4. Tim's rc3 install
    test reported this as Bug A: "edit ~/.spirit/test/.env, re-run
    spirit-preflight" silently failed because preflight never read the
    file. Same flaw bit `spirit --mode paper` on paid tier.

    Format: KEY=value lines, one per row. Matches what setup.py writes.
    No quoting / escaping — caller is responsible for keeping values
    one-line and shell-safe.

    Precedence: an env var ALREADY set in the process environment wins
    over the .env value. That preserves the override convention every
    dotenv library uses; explicit-shell-export still trumps file.
    """
    global _env_file_loaded
    if _env_file_loaded:
        return
    _env_file_loaded = True

    env_path = _resolve_env_path()
    if env_path is None or not os.path.exists(env_path):
        return

    try:
        with open(env_path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key or key in os.environ:
                    # Existing env var wins (standard dotenv convention).
                    continue
                # Strip optional surrounding quotes — the wizard doesn't
                # quote, but users editing by hand sometimes do.
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                os.environ[key] = value
    except OSError:
        # Don't crash if the .env exists but is unreadable — log a hint
        # and proceed with whatever's already in env.
        import sys
        sys.stderr.write(
            f"spirit: warning — could not read {env_path}; continuing with current environment\n"
        )


def _detect_stale_yaml() -> Optional[str]:
    """Return the path of a stale pre-#733 spirit.yaml if one is found.

    Users who ran spirit-setup before #733 may have a spirit.yaml at
    `<pipx-venv>/lib/python3.X/config/spirit.yaml` — the broken location
    setup.py used to write to. This file is ignored under the new
    resolution contract but worth flagging because it'll otherwise sit
    on disk forever, confusing anyone who finds it.

    Only fires when running from an installed package (i.e. `__file__`
    is inside a `site-packages` tree). In source-repo dev the same
    `<repo>/config/spirit.yaml` path is a legitimate file, not stale.

    Returns the path so the caller can log it once.
    """
    if "site-packages" not in os.path.normpath(__file__):
        # Dev / source-repo install — the path math points at the real
        # `<repo>/config/spirit.yaml`, which is the legitimate dev file.
        return None
    legacy = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "spirit.yaml")
    )
    if os.path.exists(legacy):
        return legacy
    return None


_stale_warning_emitted = False


def _maybe_warn_stale(path: Optional[str]) -> None:
    """Warn once per process about a stale legacy spirit.yaml."""
    global _stale_warning_emitted
    if path is None or _stale_warning_emitted:
        return
    _stale_warning_emitted = True
    # Use sys.stderr directly — the logger may not be configured yet
    # when config_loader is imported (e.g. during spirit.config import).
    import sys
    sys.stderr.write(
        f"\nspirit: warning — found a stale config at {path}\n"
        f"  This file was written by an older spirit-setup (#733); "
        f"it is no longer read.\n"
        f"  You can safely delete it. Your active config lives at "
        f"~/.spirit/<instance>/spirit.yaml.\n\n"
    )


def _load_yaml() -> dict:
    """Load the per-instance YAML config. Returns {} when unset or missing.

    The cache is keyed on the resolved path so a SPIRIT_INSTANCE change
    inside a long-running process (rare, but legitimate in tests)
    triggers a fresh read.

    First call in the process also:
      - probes for and warns about pre-#733 stale config
      - loads `~/.spirit/<instance>/.env` into `os.environ` (Bug A,
        documented Pro-flip path required this)
    """
    global _config_cache, _cache_key, _stale_check_done

    # One-shot stale-config probe — runs once per process, regardless of
    # whether SPIRIT_INSTANCE is set. The whole point is to catch the
    # "SPIRIT_INSTANCE unset, stale yaml inside venv" case where the
    # old loader would have silently pre-loaded values.
    if not _stale_check_done:
        _stale_check_done = True
        _maybe_warn_stale(_detect_stale_yaml())

    # One-shot .env load (rc4 Bug A). Must run BEFORE any caller
    # observes env-derived values so that env vars written by setup
    # propagate into get_config()'s env-first resolution.
    _load_env_file()

    path = _resolve_yaml_path()
    if path != _cache_key:
        # Path changed (or first call) — invalidate cache.
        _config_cache = None
        _cache_key = path

    if _config_cache is None:
        if path is None:
            _config_cache = {}
        else:
            try:
                with open(path) as f:
                    _config_cache = yaml.safe_load(f) or {}
            except FileNotFoundError:
                _config_cache = {}
    return _config_cache


def load_yaml_config() -> dict:
    """Return the full parsed YAML config dict (cached per-instance)."""
    return _load_yaml()


def get_config(key: str, default=None):
    """Resolution: env var -> per-instance YAML -> default.

    Never reads outside ~/.spirit/<instance>/spirit.yaml — see module
    docstring. Returns `default` when the key is absent from both env
    and (the relevant per-instance) YAML.
    """
    val = os.environ.get(key)
    if val is not None:
        return val
    yaml_conf = _load_yaml()
    val = yaml_conf.get(key)
    if val is not None:
        return str(val)
    return default
