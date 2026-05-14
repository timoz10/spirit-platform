# Changelog

All notable changes to Spirit are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Versioning policy.** See `docs/features/spirit/SPIRIT_V2_ARCHITECTURE.md` § "Versioning Process" for what triggers a MAJOR / MINOR / PATCH bump.
>
> **Release process.** See `docs/RELEASE_PUBLISHING_RUNBOOK.md`.
>
> **Pre-platform history.** Anything before v2.2.0 (ML experiments, infra
> migrations, July-August 2025 bug fixes, etc.) lives in
> `docs/archive/CHANGELOG_PRE_PUBLIC.md`. Kept for grep-ability; not
> distributed to the public mirror.

---

## [Unreleased]

The first public release candidate. Adds the Free tier (`pip install spirit-trading`, no API key required, runs against your own Kraken keys), a customer-facing uninstall + health CLI, and a stack of launch-UX polish from a full Proxmox dry-run.

### Added

- **Free-tier framework** with pluggable `DataProvider` routing — Free uses local SQLite + your own Kraken keys; Plus continues with the API gateway. New modules: `SqliteDataProvider`, `ExchangeBackedDataProvider`, `CompositeDataProvider`, `pair_registry` static fallback. Setup wizard prompts for tier on first run. Bundled `sma_crossover` reference strategy registered as a built-in. (#561 / #564)
- **Setup wizard polish** — questionary-based arrow-key UI, optional TradeBOT API key entry on Free, sensible value-defaults, normalised custom strategy name. (#569)
- **`spirit-uninstall` wizard** — clean removal of an install: stops systemd unit (or SIGTERMs a bare `python -m spirit.main` process), removes the install tree, optionally purges local data and the spirit system user. Honest about scope — API key revocation stays the user's responsibility via the portal. (#591, #593, #595, #600)
- **`spirit-health` CLI** — one-screen liveness summary reading the local SQLite (process state, last heartbeat + age, version, last trade, total trades). (#592)
- **Periodic `[SPIRIT] alive` log line** every 30 minutes (configurable via `SPIRIT_ALIVE_LOG_INTERVAL_S`) so quiet strategies don't make a healthy install look hung. Independent of the DB heartbeat. (#592)
- **Orphan-process guard at startup** — refuses to start if another `spirit.main` is already running, with a `--allow-multi-instance` override for legitimate Pro setups. (#592)
- **Public-mirror infrastructure** — allow-list, dry-run filter script, procedure doc, LICENSE (Apache-2.0), `README.md.public`, `pyproject.toml.public` with slimmed dependency set (heavy ML/PG/TA deps stripped). The dry-run renames `.public` files at filter time so the public mirror gets a normal-looking tree. (#562)
- **GitHub Actions workflows** for the Free-tier framework — pytest gate (165 tests, ~10s) + weekly Kraken network smoke. Both run on the existing self-hosted runner. (#564)
- **Release publishing runbook** at `docs/RELEASE_PUBLISHING_RUNBOOK.md` — versioning, cadence, pre-release gates, tag procedure, public-mirror push, customer comms, hotfix fast-path, rollback procedures, post-release verification. (#587)

### Changed

- **API keys no longer carry a tier prefix.** New keys are `sk_<token>` only; tier lives on the `api_keys.role` column. Existing keys with the old prefix continue to work — auth hashes the full string and looks up by hash. No DB migration required. Fixes the "user upgrades Free → Plus but key still says `sk_free_…`" UX problem. (#570 / #589)
- **`__version__` and `pyproject.toml` aligned with the public release tag.** Internal counter scheme (was bumping toward 2.11.0) replaced with the tag scheme (2.2.1) from this release onward. CI gate (#568) keeps the two strings in lock-step. (#588)
- **`spirit-uninstall` defaults to Free-tier-friendly mode** — reads the local SQLite directly to detect open positions instead of initialising the runtime DataProvider (which on Free tier dragged in the Kraken adapter just to read one row). Plus fall through to the DataProvider as before. (#600)
- **Shutdown log line** no longer says "Final state saved to PG for all pairs" (misleading on Free tier, which uses SQLite). Now: `Final state saved for all pairs (instance=<name>)`. (#594)
- **`KrakenOHLCBuffer` log lines** carry the pair + interval prefix instead of repeating identically across all 14 buffers (`[KrakenOHLCBuffer][XBTUSD/60m] Background updater stopped.`). (#594)
- **Public URL is now the default**. `data_provider.py` and `preflight.py` previously defaulted `SPIRIT_API_URL` to a private-net IP (the gateway's internal address inside the cloud network). Defaults are now `https://api.tradebot.live/v1` — the public URL — so a fresh install just works without an explicit env var. Internal callers that still need the private endpoint set `SPIRIT_API_URL` explicitly. (#562)

### Fixed

- **`SpiritContext` warmup primes every declared interval**, not just the primary. Pre-fix, monitoring intervals (e.g. 1m for ATR stops on `zone_bounce` / `macd_cross`) silently started cold for ~50 minutes after every restart, with feature engineering no-op'ing on incoming candles. Affected any strategy with `monitoring_intervals` declared; `sma_crossover` (no monitoring intervals) was unaffected. (#572 / #575)
- **`spirit-uninstall --dry-run` now shows what a live run would do.** Detection of bare `spirit.main` processes was short-circuited in dry-run, giving a false impression that nothing would happen — even though a real run absolutely would SIGTERM the process. Detection is read-only (`pgrep`) and now runs in dry-run too; `stop_bare_processes` has its own dry-run path that prints `[DRY-RUN] Would SIGTERM PID X`. (#595)
- **`spirit-health` column-name drift.** Two SQL queries used PG-flavoured column names that don't match the canonical SQLite schema in `src/spirit/storage/sqlite_schema.sql`: `updated_at` (should be `last_heartbeat`) and `pnl_realized` (should be `pnl_pct`). Errors were swallowed silently, so every health run reported "(none)" for the heartbeat AND the last trade even when real rows existed. Plus a regression gate that builds the test DB from the canonical schema file so future drift fails CI. (#595)
- **`spirit-uninstall` no longer prints "✓ Stopped + disabled spirit.service" on a no-systemd box.** Misleading wording when the systemd unit was never installed; now prints `- no systemd unit named spirit.service (skipping)` and skips the no-op `systemctl stop`/`disable` calls. (#600)
- **`spirit-uninstall` correctly detects bare `python -m spirit.main` processes** that were started outside systemd. Pre-fix, the wizard would happily `rm -rf` the install tree out from under a live process. Now it SIGTERMs detected bare processes, waits up to 30s, and refuses (exit 3) to remove the install tree if any are still running unless `--force` is set. (#593)
- **Sanitised docstring examples** in three framework files (`pipeline/daemon_health.py`, `utils/pair_registry.py`, `setup.py`) — instance-name examples now use a generic placeholder instead of an internal username. (#562)

### Internal

- **Calibrators stop spamming ERROR on tier-gated 403s.** Bridge mitigation until `/v1/me` capability advertisement (#597 / #598) lands. Each of the 6 capability-gated calibrators (`cooldown`, `risk_gate`, `entry_quality`, `composite_threshold`, `thesis_writer`, `bounce_signature`) now detects 403, logs INFO once with an upgrade hint, and skips subsequent retries for the lifetime of the process. Pro/admin keys unaffected. (#599)
- **CX22 canary API key bumped subscription → pro** (migration 035) so the canary exercises the full Pro stack including the calibration endpoints gated on `read:scorer`. (#596)
- **`runtime_lock.py` orphan-process detection** module — used by both startup (refuses to launch with another Spirit running) and uninstall (SIGTERMs bare processes before removing files). (#592 / #593)
- **`capability_check.py` helper** — `is_403_forbidden`, `is_capability_denied`, `mark_capability_denied`. Process-lifetime de-dup so the same caller doesn't log the same tier-gap twice. (#599)
- **Substantial test coverage added** — 44 SqliteDataProvider tests (Rule 11 type round-trip, schema migration, crash recovery, heartbeat, performance, DST regression), 29 uninstall tests, 19 health tests (incl. schema-drift gates that build the test DB from the canonical schema file), 12 runtime-lock tests, 15 capability-check tests, plus 6 multi-interval warmup tests for #572.

### Tracked follow-ups (filed but not in this release)

| # | Topic |
|---|---|
| #565 | Add v2.2.1 framework files to PUBLIC_MIRROR_ALLOWLIST (closed by #562) |
| #570 | Drop tier prefix from new API keys (closed by #589) |
| #572 | SpiritContext warmup primes the primary interval only (closed by #575) |
| #577 | Portal: paid tiers not visible at signup; rename Subscriber → Plus |
| #579 | Portal: welcome email + key-issuance flow on tier upgrade |
| #580 | Platform: terms of service + privacy policy for paid tiers |
| #582 | Platform: public status / uptime page |
| #583 | Platform: monitoring + alerting stack for solo-on-call posture |
| #584 | Platform: CX33 gateway patching + zero-downtime deploy runbook |
| #585 | Platform: CX22 in-flight update procedure |
| #586 | Platform: end-to-end paid-tier signup smoke test |
| #587 | Platform: release publishing process (this runbook in progress) |
| #597 | Gateway: `GET /v1/me` endpoint — expose role + capabilities |
| #598 | Spirit: read `/v1/me` capabilities at startup, skip endpoints the tier doesn't allow |
| #417 (scope #2) | Customer-facing self-revoke API endpoint (scope #1 closed by #591/#593) |

---

## [2.2.0] — 2026-05-05

First "stable platform" tag. Phase A capability-based tier enforcement on data routes + Phase B per-key daily quotas + Hetzner egress accounting + Kraken data licence request committed. Internal tag — public release was originally targeted here but slipped to 2.2.1 to incorporate the Free tier.

### Added

- **Phase A — capability-based tier enforcement** on all data routes. 19 capabilities, 32 role-capability mappings stored in `public.role_capabilities`. Migration 033. (#549 / #556)
- **Phase B — per-key daily quotas + Hetzner egress accounting**. New table `api_key_quota_daily` (per-key, per-day rollup). Quota lookup is a single PK hit; daily rollover is implicit. Migration 034. (#549 / #559 / #560)
- **Kraken data licence request** committed at `docs/Kraken-data-licence-request-2026-05-05.md`. Sent 2026-05-05.

### Fixed

- Replaced oversized `limit=` sentinels in `_trajectory_recover_if_needed` and `_expectation_recover_if_needed` with tight, documented caps (60,000 — ~42 days of 1m candles). Pre-fix, calls passed `limit=500_000` against a gateway capped at `le=200_000` → silent 422, swallowed by outer `try/except Exception`, recovery never fired on api-mode. (#425 / #548)

For pre-2.2.0 changes (versions 2.0 through 2.10.x — internal counter scheme, ML experiments, infra migrations), see `docs/features/spirit/SPIRIT_V2_ARCHITECTURE.md` § "Version History" and `docs/archive/CHANGELOG_PRE_PUBLIC.md`.

---

[Unreleased]: https://github.com/timoz10/spirit-platform/compare/v2.2.0...HEAD
[2.2.0]: https://github.com/timoz10/spirit-platform/releases/tag/v2.2.0
