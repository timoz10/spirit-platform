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


Exit code contract (standalone CLI: ``spirit-preflight``)
=========================================================

When invoked as a CLI (`main()` below — entry point for ``spirit-preflight``),
this module follows a three-state exit code contract. See
``docs/reference/MODULE_CONTRACTS.md`` for the cross-tool design.

    RC_DIAGNOSTIC_OK       = 0  — diagnostic completed, no FATAL checks.
                                  Paper-mode would start. Live-mode readiness
                                  is shown in the "Capabilities enabled"
                                  summary (✓/✗), NOT in the exit code —
                                  this is intentional: standalone preflight
                                  is informational, the real live-mode gate
                                  is inside `spirit --mode live`.
    RC_DIAGNOSTIC_BLOCKING = 1  — diagnostic completed, at least one FATAL
                                  check (typically missing SPIRIT_STRATEGY,
                                  missing instance dir, broken config).
                                  Paper-mode would NOT start either.
                                  CI gates on a fresh box assert this code
                                  because env_vars FAIL is expected there.
    RC_INTERNAL_ERROR      = 2  — uncaught exception inside the tool itself
                                  (import failure, gateway URL malformed,
                                  etc.). Alert on the tool, not on the
                                  user's config.

The contract is pinned by ``tests/test_spirit_preflight_contract.py`` and
asserted by both CI gates. Any change is a breaking change for downstream
scripts — bump MAJOR and call it out in CHANGELOG.

The in-run preflight called from ``spirit.main`` continues to use the
``run_preflight()`` function's ``PreflightResult`` directly — the exit
code contract above applies only to the standalone CLI entry point.
"""

import json
import logging
import os
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional


# Exit-code contract (see module docstring).
RC_DIAGNOSTIC_OK = 0
RC_DIAGNOSTIC_BLOCKING = 1
RC_INTERNAL_ERROR = 2

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
        "SPIRIT_API_URL", "https://api.tradebot.live/v1"
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


def _check_exchange_keys(required: bool = True) -> CheckResult:
    """Verify exchange API keys are accessible (generic + legacy fallback).

    Args:
        required: When True (in-run preflight for --mode live), missing keys
            are FATAL — Spirit can't place orders without them. When False
            (standalone diagnostic), missing keys are WARN — paper-mode works
            without them, so the user just needs to know live trading is
            disabled.
    """
    severity = 'FATAL' if required else 'WARN'
    try:
        from spirit.exchange.kraken import _load_credential
        key = _load_credential('EXCHANGE_API_KEY', 'KRAKEN_API_KEY')
        secret = _load_credential('EXCHANGE_API_SECRET', 'KRAKEN_API_SECRET')
        if key and secret:
            return CheckResult(
                name='exchange_keys',
                passed=True,
                severity=severity,
                message='Exchange API keys found — live trading enabled',
            )
        missing = []
        if not key:
            missing.append('EXCHANGE_API_KEY (or KRAKEN_API_KEY)')
        if not secret:
            missing.append('EXCHANGE_API_SECRET (or KRAKEN_API_SECRET)')
        if required:
            msg = f'Missing exchange keys: {", ".join(missing)}'
        else:
            msg = f'Exchange keys not set — live trading disabled (paper mode works). Missing: {", ".join(missing)}'
        return CheckResult(
            name='exchange_keys',
            passed=False,
            severity=severity,
            message=msg,
        )
    except Exception as e:
        return CheckResult(
            name='exchange_keys',
            passed=False,
            severity=severity,
            message='Failed to check exchange keys',
            detail=str(e),
        )


def _check_env_vars(skip_kraken: bool = False, tier: str = "", diagnostic: bool = False) -> CheckResult:
    """Verify required environment variables / config values are set.

    Free-tier (`tier == 'free'`) skips the SPIRIT_API_KEY requirement —
    Free runs entirely against the local SQLite + direct exchange REST
    and never authenticates against the gateway.

    Args:
        skip_kraken: Caller already knows Kraken keys aren't needed (e.g.
            paper/replay mode). KRAKEN_API_KEY is not checked at all.
        tier: 'free' / 'plus' / 'pro' / ''. Determines whether
            SPIRIT_API_KEY is required.
        diagnostic: When True (standalone preflight), report missing
            optional keys as WARN with informative messages rather than
            FATAL. Required keys (SPIRIT_STRATEGY always; SPIRIT_API_KEY
            for Plus/Pro) remain FATAL even in diagnostic mode.
    """
    from spirit.utils.config_loader import get_config

    # SPIRIT_STRATEGY is always required (always FATAL when missing)
    required_missing: list[str] = []
    optional_missing: list[str] = []

    config_keys = ['SPIRIT_STRATEGY']
    required_missing += [v for v in config_keys if not get_config(v)]

    # KRAKEN_API_KEY: required only when not skip_kraken AND not diagnostic mode
    # In diagnostic mode it's a WARN (paper-mode users don't need it)
    if not skip_kraken:
        if not os.environ.get('KRAKEN_API_KEY') and not os.environ.get('KRAKEN_API_KEY_FILE'):
            if diagnostic:
                optional_missing.append('KRAKEN_API_KEY (live trading disabled)')
            else:
                required_missing.append('KRAKEN_API_KEY')

    # SPIRIT_API_KEY: required for Plus/Pro. Free tier skips it entirely
    # (already filtered out at the call-site for in-run preflight; we
    # repeat the check here to keep the diagnostic branch self-contained).
    if tier != "free":
        api_key = get_config("SPIRIT_API_KEY", "") or os.environ.get("SPIRIT_API_KEY", "")
        if not api_key:
            required_missing.append('SPIRIT_API_KEY')

    if required_missing:
        return CheckResult(
            name='env_vars',
            passed=False,
            severity='FATAL',
            message=f'Missing required env vars: {", ".join(required_missing)}',
        )
    if optional_missing:
        return CheckResult(
            name='env_vars',
            passed=False,  # not passing, but not fatal
            severity='WARN',
            message=f'Optional env vars not set: {", ".join(optional_missing)}',
        )
    total = len(config_keys) + (0 if skip_kraken else 1) + (0 if tier == "free" else 1)
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


def run_preflight(
    skip_kraken: bool = False,
    tier: str | None = None,
    diagnostic: bool = False,
) -> PreflightResult:
    """
    Run all pre-flight checks. Returns PreflightResult.

    FATAL failures mean Spirit should not start.
    WARN failures are logged but do not block startup.

    Args:
        skip_kraken: If True, skip Kraken key check (e.g. paper/replay mode).
        tier: Spirit tier ('free' / 'plus' / 'pro' / ''). When None
            (default), resolved from SPIRIT_TIER env/config. Free tier skips
            the gateway connectivity + SPIRIT_API_KEY checks — Free runs
            entirely local-plus-exchange-direct and never reaches the gateway.
        diagnostic: When True (standalone `spirit-preflight` console script),
            run in informational mode — missing optional keys produce WARN
            instead of FATAL. The user is asking "what's wired up?" not
            "can I start?". Required keys remain FATAL.
    """
    if tier is None:
        tier = _resolve_tier()

    checks = [
        _check_env_vars(skip_kraken=skip_kraken, tier=tier, diagnostic=diagnostic),
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
        # Diagnostic mode: missing keys are WARN (paper-mode users don't need them).
        # Startup mode: missing keys are FATAL (would fail on first order).
        checks.append(_check_exchange_keys(required=not diagnostic))

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


def _paper_trading_label(tier: str) -> str:
    """Short tier-aware descriptor for the paper-trading line."""
    # rc4 Bug B fix: don't leak "Free tier" when the actual tier is paid.
    if tier == "plus":
        return "Plus tier, gateway-backed"
    if tier == "pro":
        return "Pro tier, gateway-backed"
    if tier == "free":
        return "Free tier, exchange-direct OHLC"
    # tier unset → assume free behavior (no gateway, direct OHLC).
    return "exchange-direct OHLC"


def _gateway_failure_reason(check) -> str:
    """Map a failed gateway check's message to a user-actionable hint.

    rc4 Bug C fix: pre-rc4 we said "set SPIRIT_API_KEY, get one at portal"
    regardless of failure mode, even when the user HAD a key and it was
    just rejected by the gateway. Now we read the check message to tell
    the two states apart.
    """
    msg = (check.message or "").lower() if check else ""
    if "rejected" in msg or "401" in msg or "403" in msg or "invalid" in msg or "unauthorized" in msg:
        return "key rejected — verify at portal.tradebot.live/keys"
    if "timeout" in msg or "unreachable" in msg or "could not reach" in msg or "connection" in msg:
        return "gateway unreachable — check network / firewall"
    # Default: assume the key is missing entirely.
    return "set SPIRIT_API_KEY, get one at portal.tradebot.live"


def _capabilities_summary(result: PreflightResult, tier: str) -> str:
    """Render a 'what can this instance actually do?' summary from check results.

    The diagnostic standalone preflight should answer "what's wired up?" not
    just "what's missing?". This helps a new user understand whether paper
    mode will work, whether live trading is gated on a missing key, and what
    gateway features they'd unlock with a Plus/Pro key.
    """
    by_name = {c.name: c for c in result.checks}
    env_passed = by_name.get('env_vars') and by_name['env_vars'].passed
    exchange_passed = by_name.get('exchange_keys') and by_name['exchange_keys'].passed
    gateway_check = by_name.get('gateway_capabilities')
    gateway_passed = gateway_check and gateway_check.passed

    lines = ["", "Capabilities enabled:"]
    # Paper trading needs SPIRIT_STRATEGY (in env_vars) + a usable exchange
    # data provider (no key required for Kraken public OHLC).
    if env_passed or (
        by_name.get('env_vars')
        and by_name['env_vars'].severity == 'WARN'
    ):
        lines.append(f"  ✓ Paper trading ({_paper_trading_label(tier)})")
    else:
        lines.append("  ✗ Paper trading (missing SPIRIT_STRATEGY — run `spirit-setup`)")

    # Live trading needs exchange keys.
    if exchange_passed:
        lines.append("  ✓ Live trading (exchange keys present)")
    else:
        lines.append("  ✗ Live trading (set EXCHANGE_API_KEY + EXCHANGE_API_SECRET)")

    # Gateway features only relevant for Plus/Pro.
    if tier == "free":
        lines.append("  – Gateway features (Free tier — not applicable)")
    elif gateway_passed:
        lines.append("  ✓ Gateway features (Plus/Pro indicators + scorer + risk gate)")
    else:
        lines.append(f"  ✗ Gateway features ({_gateway_failure_reason(gateway_check)})")

    return "\n".join(lines)


def _compute_exit_code(result: 'PreflightResult') -> int:
    """Apply the spirit-preflight exit-code contract to a PreflightResult.

    See module docstring for the contract. Pure function — kept separate
    so tests/test_spirit_preflight_contract.py can assert it directly.
    """
    if result.passed:
        return RC_DIAGNOSTIC_OK
    return RC_DIAGNOSTIC_BLOCKING


def main():
    """CLI entry point for standalone preflight checks.

    Runs in 'diagnostic' mode — informational, missing-but-not-strictly-required
    keys produce WARN instead of FATAL. For the in-run preflight that
    actually blocks startup, see `run_preflight()` called from spirit.main.

    Exit codes: see module docstring "Exit code contract" section.
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    try:
        result = run_preflight(diagnostic=True)
        tier = _resolve_tier()

        print()
        print("=" * 60)
        print(f"Pre-flight diagnostic: {'PASSED' if result.passed else 'FAILED'}")
        print(f"  Checks run: {len(result.checks)}")
        print(f"  Passed:     {sum(1 for c in result.checks if c.passed)}")
        print(f"  Warnings:   {len(result.warnings)}")
        print(f"  Fatal:      {len(result.fatal_failures)}")
        print("=" * 60)
        print(_capabilities_summary(result, tier))
        print()

        raise SystemExit(_compute_exit_code(result))
    except SystemExit:
        raise
    except Exception as exc:
        # Tool itself failed — distinguish from "user config has FATAL".
        print(
            f"\nspirit-preflight: internal error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(RC_INTERNAL_ERROR)


if __name__ == '__main__':
    main()
