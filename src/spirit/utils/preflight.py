"""
Pre-flight Validation for Spirit Trading System

Runs before any trading logic to validate environment and gateway
connectivity. Prevents silent startup failures.

Spirit is api-driven (#340): all data access goes through the API gateway.
Direct PostgreSQL checks were removed with the pg-mode strip.

Usage:
    from spirit.utils.preflight import run_preflight

    result = run_preflight()
    if not result.passed:
        sys.exit(1)
"""

import json
import logging
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from spirit.logger import get_logger
logger = get_logger("preflight")


# ---------------------------------------------------------------------------
# Session capability cache (populated by _check_gateway_capabilities)
# ---------------------------------------------------------------------------
# Cached after a successful /v1/whoami at startup. Subsequent modules
# (strategy preflight, data-provider factory, future BYOD wiring) read
# from here rather than re-hitting the gateway. None until preflight runs.
# Free tier never populates this — Spirit Free has no gateway interaction.

_session_whoami: dict | None = None


def get_session_whoami() -> dict | None:
    """Return the cached /v1/whoami response from the most recent preflight, or None.

    Available after `run_preflight()` completes successfully on a non-Free tier.
    Shape: {"instance", "name", "role", "tier_label", "permissions",
    "capabilities" (list[str]), "ip_history"}.
    """
    return _session_whoami


def get_session_capabilities() -> frozenset[str]:
    """Return the capability set granted to this Spirit instance, or empty if unknown.

    Empty frozenset is the safe default — code checking "do I have cap X?"
    against an empty set will correctly conclude "no".
    """
    if _session_whoami is None:
        return frozenset()
    return frozenset(_session_whoami.get("capabilities", []))


@dataclass
class CheckResult:
    """Result of a single pre-flight check."""
    name: str
    passed: bool
    severity: str  # 'FATAL' or 'WARN'
    message: str
    detail: Optional[str] = None


@dataclass
class PreflightResult:
    """Aggregate result of all pre-flight checks."""
    passed: bool  # True if no FATAL checks failed
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def fatal_failures(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.passed and c.severity == 'FATAL']

    @property
    def warnings(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.passed and c.severity == 'WARN']


def _check_api_gateway_connectivity() -> CheckResult:
    """Test API gateway reachability."""
    import urllib.error
    import urllib.request

    from spirit.utils.config_loader import get_config

    base_url = get_config("SPIRIT_API_URL", "")
    if not base_url:
        base_url = os.environ.get("SPIRIT_API_URL", "https://api.tradebot.live/v1")
    health_url = base_url.rstrip("/") + "/health"

    try:
        req = urllib.request.Request(health_url, method="GET",
                                     headers={"User-Agent": "Spirit/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return CheckResult(
                    name='api_gateway',
                    passed=True,
                    severity='FATAL',
                    message=f'API gateway reachable at {health_url}',
                )
            return CheckResult(
                name='api_gateway',
                passed=False,
                severity='FATAL',
                message=f'API gateway returned HTTP {resp.status}',
                detail=health_url,
            )
    except urllib.error.URLError as e:
        return CheckResult(
            name='api_gateway',
            passed=False,
            severity='FATAL',
            message='API gateway unreachable',
            detail=f'{health_url}: {e.reason}',
        )
    except Exception as e:
        return CheckResult(
            name='api_gateway',
            passed=False,
            severity='FATAL',
            message='API gateway connectivity check failed',
            detail=str(e),
        )


def _check_gateway_capabilities() -> CheckResult:
    """Resolve this Spirit instance's tier + capabilities by calling /v1/whoami.

    Populates the module-level `_session_whoami` cache used downstream by
    the strategy preflight, data-provider factory, and BYOD wiring (#666).

    Treated as FATAL: if Spirit can reach `/v1/health` (the connectivity
    check just passed) but cannot resolve its own key against `/v1/whoami`,
    something is meaningfully wrong (invalid key, expired, revoked) and we
    fail fast rather than discover it at the first 403 hours into a run.

    Free tier doesn't reach here — run_preflight skips this check when
    tier='free' because Free instances have no gateway API key.
    """
    global _session_whoami

    from spirit.utils.config_loader import get_config

    base_url = get_config("SPIRIT_API_URL", "") or os.environ.get(
        "SPIRIT_API_URL", "http://10.0.0.4:8000/v1"
    )
    api_key = get_config("SPIRIT_API_KEY", "") or os.environ.get("SPIRIT_API_KEY", "")

    if not api_key:
        # Defensive — env_vars check should have caught this already.
        return CheckResult(
            name='gateway_capabilities',
            passed=False,
            severity='FATAL',
            message='No SPIRIT_API_KEY set; cannot resolve capabilities',
        )

    whoami_url = base_url.rstrip("/") + "/whoami"
    try:
        req = urllib.request.Request(
            whoami_url, method="GET",
            headers={"User-Agent": "Spirit/1.0", "X-API-Key": api_key},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        status = e.code
        if status == 401:
            return CheckResult(
                name='gateway_capabilities',
                passed=False,
                severity='FATAL',
                message='Gateway rejected API key (401)',
                detail='Your SPIRIT_API_KEY is invalid or revoked. Reissue at portal.tradebot.live.',
            )
        if status == 403:
            return CheckResult(
                name='gateway_capabilities',
                passed=False,
                severity='FATAL',
                message='Gateway forbade /v1/whoami (403)',
                detail=str(e),
            )
        return CheckResult(
            name='gateway_capabilities',
            passed=False,
            severity='FATAL',
            message=f'/v1/whoami returned HTTP {status}',
            detail=str(e),
        )
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        return CheckResult(
            name='gateway_capabilities',
            passed=False,
            severity='FATAL',
            message='Failed to resolve capabilities via /v1/whoami',
            detail=str(e),
        )

    # Cache for downstream consumers.
    _session_whoami = body

    tier_label = body.get("tier_label", body.get("role", "unknown"))
    caps = sorted(body.get("capabilities", []))
    n_caps = len(caps)

    # Decide OHLC source signal for the startup line (cloud-first default,
    # see SPIRIT_OHLC_SOURCE config knob). Today's gating: do we have
    # read:ohlc? If yes → cloud; if no → local (BYOD, see #666 once wired).
    ohlc_source = "CLOUD (gateway)" if "read:ohlc" in caps else "LOCAL (BYOD via Kraken public REST)"

    logger.info(
        f"[STARTUP] Tier={tier_label} ({body.get('role','?')}) "
        f"instance={body.get('instance','?')} caps={n_caps} "
        f"OHLC source={ohlc_source}"
    )
    logger.info(f"[STARTUP] Granted capabilities: {', '.join(caps) if caps else '(none)'}")

    return CheckResult(
        name='gateway_capabilities',
        passed=True,
        severity='FATAL',
        message=f'Tier={tier_label}, {n_caps} capabilities granted',
        detail=f"OHLC source: {ohlc_source}",
    )


def _check_strategy_capabilities() -> CheckResult:
    """Check the active strategy's declared `required_capabilities` against the granted set.

    Loads the strategy named by `SPIRIT_STRATEGY` (env/config), reads its
    `required_capabilities` property, and verifies every capability is in
    the session-cached set from `_check_gateway_capabilities`.

    FATAL if anything is missing — the strategy will crash on its first
    data fetch otherwise. Better to fail at startup with a clear list.

    Skipped (returns PASS) when:
      - The session whoami wasn't resolved (e.g. preflight order changed)
      - The strategy hasn't declared any required capabilities
      - The strategy module can't be loaded (logged separately)
    """
    from spirit.utils.config_loader import get_config

    granted = get_session_capabilities()
    if not _session_whoami:
        return CheckResult(
            name='strategy_capabilities',
            passed=True,
            severity='WARN',
            message='Skipped — session whoami not resolved',
        )

    strategy_name = get_config('SPIRIT_STRATEGY', '') or os.environ.get(
        'SPIRIT_STRATEGY', ''
    )
    if not strategy_name:
        return CheckResult(
            name='strategy_capabilities',
            passed=True,
            severity='WARN',
            message='Skipped — SPIRIT_STRATEGY not set',
        )

    # Best-effort dynamic load: instantiate via the strategy_config factory
    # so brand-agnostic plumbing handles registry lookup + user-dir scan.
    # We read required_capabilities off the instance, not the class, to
    # accommodate strategies whose declared set depends on constructor params.
    # If load fails we WARN, not FATAL — the strategy_config layer surfaces
    # its own clearer "unknown strategy" error in main.
    try:
        from spirit.strategy_config import get_strategy
        instance = get_strategy()
        if instance is None:
            return CheckResult(
                name='strategy_capabilities',
                passed=True,
                severity='WARN',
                message=f"Couldn't load strategy '{strategy_name}' for cap check",
                detail='strategy_config.get_strategy() returned None',
            )
        required = frozenset(getattr(instance, 'required_capabilities', frozenset()))
    except Exception as e:
        return CheckResult(
            name='strategy_capabilities',
            passed=True,
            severity='WARN',
            message=f"Couldn't introspect strategy '{strategy_name}'",
            detail=f"{type(e).__name__}: {e}",
        )

    if not required:
        return CheckResult(
            name='strategy_capabilities',
            passed=True,
            severity='FATAL',
            message=f"Strategy '{strategy_name}' declares no required capabilities",
        )

    missing = required - granted
    if missing:
        return CheckResult(
            name='strategy_capabilities',
            passed=False,
            severity='FATAL',
            message=(
                f"Strategy '{strategy_name}' requires capabilities not granted by your tier: "
                f"{', '.join(sorted(missing))}"
            ),
            detail=(
                f"Granted: {', '.join(sorted(granted)) or '(none)'}. "
                f"Either upgrade your tier at portal.tradebot.live, "
                f"or switch to a strategy that fits your capability set."
            ),
        )

    return CheckResult(
        name='strategy_capabilities',
        passed=True,
        severity='FATAL',
        message=(
            f"Strategy '{strategy_name}' required capabilities all granted "
            f"({len(required)} caps)"
        ),
    )


def _check_exchange_keys() -> CheckResult:
    """Verify exchange API keys are accessible (generic + legacy fallback)."""
    try:
        from spirit.exchange.kraken import _load_credential
        key = _load_credential('EXCHANGE_API_KEY', 'KRAKEN_API_KEY')
        secret = _load_credential('EXCHANGE_API_SECRET', 'KRAKEN_API_SECRET')
        if key and secret:
            return CheckResult(
                name='exchange_keys',
                passed=True,
                severity='FATAL',
                message='Exchange API keys found',
            )
        missing = []
        if not key:
            missing.append('EXCHANGE_API_KEY (or KRAKEN_API_KEY)')
        if not secret:
            missing.append('EXCHANGE_API_SECRET (or KRAKEN_API_SECRET)')
        return CheckResult(
            name='exchange_keys',
            passed=False,
            severity='FATAL',
            message=f'Missing exchange keys: {", ".join(missing)}',
        )
    except Exception as e:
        return CheckResult(
            name='exchange_keys',
            passed=False,
            severity='FATAL',
            message='Failed to check exchange keys',
            detail=str(e),
        )


def _check_env_vars(skip_kraken: bool = False, tier: str = "") -> CheckResult:
    """Verify required environment variables / config values are set.

    Free-tier (`tier == 'free'`) skips the SPIRIT_API_KEY requirement —
    Free runs entirely against the local SQLite + direct exchange REST
    and never authenticates against the gateway.
    """
    from spirit.utils.config_loader import get_config

    # Secrets: must come from env vars (not YAML)
    secrets: list[str] = []
    if not skip_kraken:
        secrets.append('KRAKEN_API_KEY')
    missing = [v for v in secrets if not os.environ.get(v)]
    # Also check _FILE variants for Kraken keys (Docker secret pattern)
    if 'KRAKEN_API_KEY' in missing and os.environ.get('KRAKEN_API_KEY_FILE'):
        missing.remove('KRAKEN_API_KEY')

    # Plus/Pro tiers require a gateway API key. Free tier does not.
    if tier != "free":
        api_key = get_config("SPIRIT_API_KEY", "") or os.environ.get("SPIRIT_API_KEY", "")
        if not api_key:
            missing.append('SPIRIT_API_KEY')

    # Config: can come from env var OR YAML
    config_keys = ['SPIRIT_STRATEGY']
    missing += [v for v in config_keys if not get_config(v)]

    api_key_count = 0 if tier == "free" else 1
    total = len(secrets) + len(config_keys) + api_key_count
    if missing:
        return CheckResult(
            name='env_vars',
            passed=False,
            severity='FATAL',
            message=f'Missing env vars: {", ".join(missing)}',
        )
    return CheckResult(
        name='env_vars',
        passed=True,
        severity='FATAL',
        message=f'All {total} required env vars set',
    )


def _check_disk_space() -> CheckResult:
    """Check disk space on the working directory."""
    try:
        usage = shutil.disk_usage(os.getcwd())
        free_mb = usage.free / (1024 * 1024)
        if free_mb < 500:
            return CheckResult(
                name='disk_space',
                passed=False,
                severity='WARN',
                message=f'Low disk space: {free_mb:.0f}MB free',
            )
        return CheckResult(
            name='disk_space',
            passed=True,
            severity='WARN',
            message=f'Disk space OK: {free_mb:.0f}MB free',
        )
    except Exception as e:
        return CheckResult(
            name='disk_space',
            passed=False,
            severity='WARN',
            message='Failed to check disk space',
            detail=str(e),
        )


def _resolve_tier() -> str:
    """Read `SPIRIT_TIER` from env or config, normalised to lowercase.

    Returns '' (empty string) when unset — preserves the original
    Plus/Pro-default behaviour for callers that don't pass `tier`.
    """
    from spirit.utils.config_loader import get_config

    tier = (get_config("SPIRIT_TIER", "") or "").strip().lower()
    if not tier:
        tier = os.environ.get("SPIRIT_TIER", "").strip().lower()
    return tier


def run_preflight(skip_kraken: bool = False, tier: str | None = None) -> PreflightResult:
    """
    Run all pre-flight checks. Returns PreflightResult.

    FATAL failures mean Spirit should not start.
    WARN failures are logged but do not block startup.

    skip_kraken: If True, skip Kraken key check (e.g. paper/replay mode).
    tier:        Spirit tier ('free' / 'subscription' / 'pro' / ''). When
                 None (default), resolved from SPIRIT_TIER env/config.
                 Free tier skips the gateway connectivity + SPIRIT_API_KEY
                 checks — Free runs entirely local-plus-exchange-direct
                 and never reaches the gateway.
    """
    if tier is None:
        tier = _resolve_tier()

    checks = [
        _check_env_vars(skip_kraken=skip_kraken, tier=tier),
    ]

    # Gateway connectivity is a Plus/Pro concern. Free tier doesn't talk
    # to the gateway at all (CompositeDataProvider routes reads to the
    # exchange and writes to local SQLite).
    if tier != "free":
        checks.append(_check_api_gateway_connectivity())
        # Only resolve capabilities if env_vars + connectivity passed —
        # /v1/whoami won't be reachable otherwise.
        if all(c.passed for c in checks if c.severity == 'FATAL'):
            checks.append(_check_gateway_capabilities())
            # Strategy preflight depends on the cached whoami.
            if _session_whoami is not None:
                checks.append(_check_strategy_capabilities())

    if not skip_kraken:
        checks.append(_check_exchange_keys())

    checks.append(_check_disk_space())

    fatal_failed = any(not c.passed and c.severity == 'FATAL' for c in checks)

    result = PreflightResult(
        passed=not fatal_failed,
        checks=checks,
    )

    for check in checks:
        if check.passed:
            logger.info(f"[PREFLIGHT] PASS  {check.name}: {check.message}")
        elif check.severity == 'FATAL':
            detail = f" ({check.detail})" if check.detail else ""
            logger.error(f"[PREFLIGHT] FAIL  {check.name}: {check.message}{detail}")
        else:
            detail = f" ({check.detail})" if check.detail else ""
            logger.warning(f"[PREFLIGHT] WARN  {check.name}: {check.message}{detail}")

    if result.passed:
        logger.info(f"[PREFLIGHT] All checks passed ({len(checks)} total)")
    else:
        logger.error(
            f"[PREFLIGHT] {len(result.fatal_failures)} FATAL failure(s) — Spirit cannot start"
        )

    return result


def main():
    """CLI entry point for standalone preflight checks."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )
    result = run_preflight()
    print()
    print("=" * 60)
    print(f"Pre-flight: {'PASSED' if result.passed else 'FAILED'}")
    print(f"  Checks run: {len(result.checks)}")
    print(f"  Passed:     {sum(1 for c in result.checks if c.passed)}")
    print(f"  Warnings:   {len(result.warnings)}")
    print(f"  Fatal:      {len(result.fatal_failures)}")
    print("=" * 60)

    if not result.passed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
