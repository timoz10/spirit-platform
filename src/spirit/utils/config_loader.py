"""
Config loader with resolution order: env var -> YAML -> hardcoded default.

Separates operational config (git-tracked YAML) from secrets (env-only).
"""

import os

import yaml

_config_cache = None

# Candidate YAML paths (tried in order)
_YAML_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), '..', 'config', 'spirit.yaml'),     # src/spirit/config/
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'config', 'spirit.yaml'),  # project root config/
]


def _load_yaml():
    global _config_cache
    if _config_cache is None:
        for candidate in _YAML_CANDIDATES:
            yaml_path = os.path.normpath(candidate)
            try:
                with open(yaml_path) as f:
                    _config_cache = yaml.safe_load(f) or {}
                    return _config_cache
            except FileNotFoundError:
                continue
        _config_cache = {}
    return _config_cache


def load_yaml_config() -> dict:
    """Return the full parsed YAML config dict (cached)."""
    return _load_yaml()


def get_config(key: str, default=None):
    """Resolution: env var -> YAML -> default."""
    val = os.environ.get(key)
    if val is not None:
        return val
    yaml_conf = _load_yaml()
    val = yaml_conf.get(key)
    if val is not None:
        return str(val)
    return default
