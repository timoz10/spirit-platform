"""Bridge mitigation for tier-gated endpoint 403s.

Phase A capability enforcement (#549, 2026-05-05) made every Spirit
calibrator fail with HTTP 403 when its tier doesn't include the
required capability (e.g. Plus calling /v1/calibrations/* which is
gated on read:scorer, a Pro-only capability).

The 403s are correct — calibration data is a Pro feature. But each
calibrator currently catches a generic Exception, logs ERROR, and
retries every cycle (hourly), producing log spam for endpoints the
tier will never reach.

This module is a small helper used by every capability-gated caller
to:

  1. Detect a 403 specifically (vs network / 5xx / parse errors).
  2. Log ONE friendly INFO line per (caller, capability) pair on
     the first 403, explaining the tier gap to the customer.
  3. Mark that caller+capability as "permanently denied for this
     process lifetime" so subsequent cycles short-circuit cleanly
     without re-trying or re-logging.

Real fix is the /v1/me capability advertisement (#597 + #598). Until
those land, this keeps Plus-tier customer logs clean without changing
gateway behaviour.
"""
from __future__ import annotations

from typing import Optional

# Process-lifetime cache of (caller_label, capability) → True. Once a
# pair is in here, we know that caller cannot reach an endpoint gated
# on that capability and shouldn't retry.
_DENIED: dict[tuple[str, str], bool] = {}


def is_403_forbidden(exc: BaseException) -> bool:
    """True if `exc` looks like an HTTP 403 from the gateway.

    Matches both `requests.HTTPError` (with response attribute) and
    string-formatted error messages from API client wrappers that
    don't preserve the original exception.
    """
    # Direct status_code check (requests-flavour exceptions)
    resp = getattr(exc, "response", None)
    if resp is not None:
        sc = getattr(resp, "status_code", None)
        if sc == 403:
            return True

    # String fallback — covers wrappers that re-raise as a plain
    # Exception with the response text concatenated into the message.
    msg = str(exc)
    if "403" in msg and ("Forbidden" in msg or "Client Error" in msg):
        return True
    return False


def is_capability_denied(caller: str, capability: str) -> bool:
    """Has this (caller, capability) been observed as 403 already?

    Calibrators consult this at the top of each cycle — if True, return
    static defaults without making the API call. Avoids both the wasted
    request and the duplicate log line.
    """
    return (caller, capability) in _DENIED


def mark_capability_denied(
    caller: str,
    capability: str,
    endpoint: str,
    logger,
    upgrade_hint: Optional[str] = None,
) -> None:
    """Record that `caller` was 403'd for `capability` and log ONCE.

    Subsequent calls for the same (caller, capability) pair are no-ops.

    Args:
        caller: Short label that goes in the log prefix (e.g. "COOLDOWN",
            "ENTRY_QUALITY_CAL"). Should match the caller's existing log
            format.
        capability: The capability the caller's tier is missing
            (e.g. "read:scorer"). Surfaces in the log so the customer
            can map it to the tier matrix.
        endpoint: HTTP endpoint that returned 403 (e.g.
            "/v1/calibrations/cooldown"). Used in the log only —
            de-dup is on (caller, capability), not endpoint.
        logger: The caller's existing logger.
        upgrade_hint: Optional human-friendly suggestion appended to
            the log message. Defaults to "Upgrade to Pro for live-tuned
            calibration." for the scorer capability.
    """
    key = (caller, capability)
    if key in _DENIED:
        return
    _DENIED[key] = True

    if upgrade_hint is None:
        if capability == "read:scorer":
            upgrade_hint = "Upgrade to Pro for live-tuned calibration."
        else:
            upgrade_hint = "Upgrade to Pro for full feature access."

    logger.info(
        f"[{caller}] {capability} not in your tier — using static defaults. "
        f"{upgrade_hint} (Endpoint {endpoint} will be skipped for the rest "
        f"of this run; this message appears once per process.)"
    )


def reset_denied_cache_for_tests() -> None:
    """Clear the process-lifetime cache. Test-only helper."""
    _DENIED.clear()
