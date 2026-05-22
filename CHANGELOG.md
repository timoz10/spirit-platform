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

## Unreleased

_v2.2.4 work in progress — see the section below._


## 2.2.4 — TBD

### Added

- **Free-tier BYOD OHLC store.** Free instances can now upload OHLC data via `python3 -m spirit.backfill <kraken-csv>` and query it back through the standard DataProvider Protocol. The backfill CLI is tier-agnostic — same command, same flags on Free and Paid. New SQLite tables `user_ohlc` + `user_ohlc_batches` ship in the v2.2.4 schema; idempotent dedupe via composite PK + `ON CONFLICT DO NOTHING`. CSV re-imports of overlapping windows are silent dedupes; the count surfaces in the per-chunk log line.
- **Boot-time OHLC catch-up.** When Spirit starts after downtime, it now fills the gap between "the last candle in my local store" and "now" before the strategy loop's first tick. Bounded by Kraken's 720-row per-call cap; if the gap is larger, the runner fills the most-recent 720 and logs a WARN advising a CSV-upload top-up. Empty local stores skip with INFO (no auto-seed). New env var `SPIRIT_OHLC_CATCHUP_INTERVALS` (default `60`) controls which intervals are caught up — comma-separated minutes. Adds ~15–20s to boot for 5 pairs × 3 intervals; single `[CATCHUP] complete in Xs (...)` summary log at the end.
- **`/v1/runs` gateway routes.** New `GET /v1/runs` (list runs, RLS-scoped, derived from `strategy_performance` GROUP BY `run_id`) and `DELETE /v1/runs/{run_id}` (cascade-delete across the three RLS-scoped result tables, refuses `'live'`). Real Paid customer installs can finally use `spirit --list-runs` / `spirit --delete-run` — the pre-v2.2.4 paid path used direct PG which crashes on customer machines (no DB creds). Capabilities `read:runs` + `delete:runs` granted to Plus, Pro, internal_canary, internal_test_plus, internal_test_pro (migration 040).

### Changed

- **`spirit --list-runs` / `spirit --delete-run` now go through the DataProvider Protocol** on both tiers. `run_manager` is 45% smaller (363 → 198 LOC) — six private branches/helpers deleted. Same UX surface; cleaner internals. (#779)
- **`spirit.backfill` CLI no longer accepts `--api-url` / `--api-key`.** Both are env-driven via `SPIRIT_API_URL` / `SPIRIT_API_KEY` through the tier factory. Free tier doesn't need an API key at all — the backfill goes to local SQLite. Scripts that passed the old flags will exit 2 with an argparse-rejected-flag message; switch to env vars.
- **`delete_run()` returns a uniform 4-key dict across tiers.** Pre-v2.2.4 Free returned `{strategy_performance: N}` (1-key). Now both tiers return `{strategy_performance, scorer_outcomes, risk_gate_decisions, replay_runs}` — Free reports zero for the three paid-only tables. Callers that were `assert counts == {"strategy_performance": N}` need updating.

### Fixed

- **`spirit --list-runs` on Free returned `[]` even when runs existed.** v2.2.3's `_resolve_sqlite_provider` read `dp.writes` but `CompositeDataProvider` stores it as `_writes`. The v2.2.4 architecture removes the helper entirely — `list_runs` now delegates through the Protocol. (#779)
- **Heartbeat log line showed `last_candle=unknown` despite candles flowing.** Read site asked for `last_candle_dt`; writers populate `last_candle_time`. Two-character source change + 3 regression tests. (#780)
- **`pip install spirit-platform` carried `sha=unknown` in every log line.** `git rev-parse` via subprocess fails on pipx installs (no `.git` in site-packages). Now `__git_sha__` is baked into the wheel at `publish.yml` build time; main.py prefers it, falls back to subprocess for source-tree installs. CI gates the injection on every PR + every published wheel. (#781)
- **`pairs.json` missing from the installed wheel — boot catch-up silently disabled.** rc1 surfaced this: `pyproject.toml.public`'s package-data glob was `["*.sql"]` only, so the JSON pair registry was dropped from the wheel. v2.2.4's new `OhlcCatchupRunner` is the first runtime consumer of the file, so the latent gap finally showed up. Glob updated to `["*.sql", "*.json"]` in both pyproject files + an install-smoke regression gate now asserts the file is on disk after pipx install. Spirit continued to run fine on rc1 via env-override fallback, but the v2.2.4 catch-up happy path never executed. (#796)
- **Log file landed inside the pipx venv — wiped on every upgrade.** Pre-rc2 `LOG_FILE` resolved as "three dirs up from `config.py`", which on a pipx install pointed at `~/.local/share/pipx/venvs/spirit-platform/.../logs/`. `pipx upgrade` wipes the venv and silently destroyed customer log history. `_resolve_log_file()` is now instance-aware: with `SPIRIT_INSTANCE` set and `~/.spirit/<instance>/` present, logs land at `~/.spirit/<instance>/logs/spirit_syslog.log` (matches the SqliteDataProvider layout — DB already lives there). `LOG_FILE` env override and the repo-dev fallback are preserved. Pinned by `tests/test_log_path_resolution.py` (5 branches) + an install-smoke gate that imports `LOG_FILE` from the pipx-installed wheel and asserts the resolved path. Pre-existing flaw — same code in v2.2.3; surfaced when rc1 customers actually upgraded. (#799)

### Architecture

- **`FrameworkDataProvider` Protocol is now the only tier switch in the codebase** — no `if tier == "free":` branches outside `get_data_provider()`. Documented in the new `docs/adr/0001-data-provider-is-the-tier-switch.md`. All five new BYOD/runs methods (`upload_user_ohlc`, `append_user_ohlc`, `get_user_ohlc`, `list_runs`, `delete_run`) preserve this invariant. Per-tier behaviour is owned by the concrete `SqliteDataProvider` / `ApiDataProvider` implementations, not by consumer code.
- **Module Contract Discipline (Rule 12) extended to v2.2.4 surface.** Contracts for the 5 new Protocol methods + `OhlcCatchupRunner` + `wire_boot_catchup` added to `docs/reference/MODULE_CONTRACTS.md`. Pinned by tests in `tests/test_sqlite_data_provider_*`, `tests/test_runs_routes.py`, `tests/test_ohlc_catchup_runner.py`.

### CI / release engineering

- **Five new install-smoke gates on every PR** (`tests.yml`):
  - SHA injection regex matches `__init__.py` shape (catches a v2.2.5+ format-drift regression early)
  - Installed wheel reports a non-`unknown` `__git_sha__` (the #781 contract end-to-end)
  - `[CATCHUP] complete in` log line appears during `spirit --mode test` (proves `wire_boot_catchup` wires up correctly)
  - `pairs.json` is on disk after pipx install + parses as JSON (#796 contract — rc2)
  - `LOG_FILE` imported from the pipx-installed wheel resolves under `~/.spirit/<instance>/logs/`, not inside the venv (#799 contract — rc2)
- **Pytest allowlist now includes 7 new v2.2.4 test files** — total 118 new tests across BYOD, runs, catchup, the three regression bug fixes, and log-path resolution.

---

## 2.2.3 — 2026-05-20

### GA-blocker fixes (post-rc9, included in the published 2.2.3 wheel)

- **Multi-instance guard now fires for pipx-installed customers (CRITICAL real-money safety fix).** Pre-#760 `runtime_lock.detect_other_spirit_processes()` used a pgrep regex (`\bspirit\.main\b`) that only matched the dev `python -m spirit.main` invocation. Every pipx-installed customer running `/.local/bin/spirit ...` has argv that contains the word `spirit` but never the literal `spirit.main` — pgrep returned empty, the guard fired for nobody. In live mode, two daemons on the same instance would have issued duplicate orders to the exchange. The regex now matches both patterns (`spirit\.main` for dev, `\bspirit\b` for installed), and a tightened stage-2 argv filter rejects the inevitable false positives (`spirit-health`, `spirit-preflight`, `spirit-setup`, shell wrappers, random python processes that mention "spirit" in argv). 33 unit tests pin every case in the detection matrix. Contract documented in `docs/reference/MODULE_CONTRACTS.md`. (#760)
- **`spirit --help` is now read-only.** Pre-#758 the CLI ran full preflight, initialised the Kraken exchange provider, opened SQLite, wrote a version row to `spirit_state`, and recorded a heartbeat — all BEFORE argparse printed the help text. A new user typing `spirit --help` to discover commands saw network errors, mutated their state DB, and (on a Free box with no Kraken keys) got scary WARN lines from the exchange init. Now argparse runs first thing in `main()`; `--help`, `--version`, unknown-flag rejection, `--list-runs` and `--delete-run` all return cleanly without touching DB, network, or exchange. Same pattern as #733's earlier fix for `spirit-setup`. (#758)
- **CI gates the read-only contract.** Three workflows (`tests.yml` install-smoke, `publish.yml` smoke-runtime, `rc-validation.yml` install-matrix) now assert that `spirit --help` on a fresh `HOME` produces no `~/.spirit/<instance>/spirit.db` file, no startup-machinery log lines, and exits 0. Contract documented in `docs/reference/MODULE_CONTRACTS.md`. (#758)
- **`spirit --list-runs` no longer crashes on Free tier.** v2.2.3-rc1..rc9 all shipped with a `ModuleNotFoundError: spirit.utils.db_connection` whenever a Free-tier user ran `--list-runs` or `--delete-run` — the lazy import inside `run_manager.py` was reaching a module excluded from the public mirror. Both flags now derive the run list from local SQLite (`strategy_performance` GROUP BY `run_id`) on Free, and read the PG `replay_runs` registry on Plus/Pro. `--delete-run` on a nonexistent run id now prints a clean "not found" message and exits 0 instead of pretending it deleted 0 rows. (#757)
- **`run_manager` now has a CI-enforced exit-code contract.** Three independent gates assert `spirit --list-runs` and `spirit --delete-run` exit cleanly on a Free install: per-PR (tests.yml install-smoke), pre-publish (publish.yml smoke-runtime), and per-RC tag multi-platform (rc-validation.yml install-matrix). Catches the broader bug class of "public-allowlisted module lazy-imports a denied module" for any future similar flag. See `docs/reference/MODULE_CONTRACTS.md`. (#757)
- **Renamed `--csv` to `--csv-input`** to make the intent clear: this flag reads OHLC from a CSV file (replay-style backtest input). It does NOT export to CSV — the original name read like an export and customers running `--csv --csv-path /tmp/output.csv` got a raw `FileNotFoundError` from pandas. The legacy `--csv` flag still works as a silent alias (no deprecation warning at this pre-launch stage; revisit in v2.3.x). A separate `--csv-export` for trade-history export is tracked as an enhancement, not in this fix. (#759)
- **`spirit --csv-input` now validates its arguments at argparse time.** Two new guards:
  - **`--csv-input` is rejected outside `--mode test`** — paper/live are forward-looking and would silently use static data. Suggests `--replay --start --end` for historical replays.
  - **`--csv-path` must point at an existing regular file.** Missing/directory paths produce a clean `parser.error()` ("file not found or not a regular file: ...") instead of a `pandas.read_csv` traceback at the start of the run.
  - Both checks run before any startup machinery (post-#758 argparse-first ordering), so the user sees the error before any DB write or preflight runs. (#759)
- **`spirit-setup` no longer leaves 0-byte `.env` files behind (defensive hardening for #766).** Three independent fixes:
  - `_ask_select` now accepts the value string (`free`, `paid`, `kraken`) in addition to the numeric index — pre-#766 a value-string input was silently rejected and consumed an extra stdin line, shifting all subsequent prompts. Invalid inputs are also bounded (max 3 retries) before falling through to the default rather than spinning forever; EOF falls through to default cleanly.
  - The paid path no longer writes `SPIRIT_API_KEY=` with an empty value when the user provides no key. Mirrors the Free path's `if api_key:` pattern.
  - `_write_env` refuses to create or rewrite a 0-byte `.env` file. Empty-string values are dropped before writing; if both existing content and new values are empty, the file is left alone (or never created).
  - Severity: LOW per the issue's gate (interactive customers using `questionary` were never affected — these bugs only triggered via scripted-stdin in CI scaffolding). 14 unit tests pin all three fixes.
  - **Note for existing users:** if you have a 0-byte `.env` from a pre-#766 wizard run, the new wizard won't touch it (no destructive overwrites). Remove it manually if it's bothering you — `spirit-preflight` reads from shell env when the file is missing or empty. (#766)

### Free-tier notes

- The `replay_runs` registry table doesn't exist on Free — runs are derived from `strategy_performance`. As a result, the `Tag`, `PF` (profit factor) and `git_hash` columns render as `-` in `--list-runs` output on Free. Win-rate, trade count, net PnL %, date range and pairs are all populated. Paid tiers show all columns.

---

### Changed (BREAKING)

- **Renamed paid tier role from `subscription` to `plus`** to match the public-facing tier name. (#747)
  - `/v1/whoami` now returns `"role": "plus"` for keys that previously returned `"role": "subscription"`.
  - `SPIRIT_TIER=subscription` in `~/.spirit/<instance>/.env` no longer works; update to `SPIRIT_TIER=plus`.
  - Existing portal-issued keys are auto-migrated server-side — no key reissue required.
  - Rationale: the DB role name (`subscription`) and the public name (`Plus`) had diverged at v2.2.0 (#481). Customer surfaces (`/v1/whoami`, `.env`, CSS class) leaked the legacy name. Pre-launch (zero paying customers) was the cheapest time to clean it up.

### Added

- **Internal test roles `internal_test_plus` and `internal_test_pro`** for CI gateway-success-branch coverage (#744). Mirror the public Plus / Pro capability sets. Used exclusively by the rc-validation matrix to assert the `✓ Gateway features` line — the same line every paying customer sees on first preflight.

### Fixed

- **`spirit-setup` now writes to `~/.spirit/<instance>/`, not into the install tree.** Pre-#733 the wizard computed its write target as `<setup.py-location>/../../config/spirit.yaml`, which on pipx installs resolved to inside the venv directory (e.g. `~/.local/share/pipx/venvs/spirit-platform/lib/python3.X/config/spirit.yaml`). That file survived `rm -rf ~/.spirit/`, survived `pipx upgrade`, and was silently read by every later `spirit-preflight` / `spirit-health` invocation — making fresh boxes look configured when they weren't. (#733)
- **`spirit.utils.config_loader` no longer walks up parent directories looking for YAML.** Resolution is now `SPIRIT_INSTANCE` env var → `~/.spirit/<that-name>/spirit.yaml`, or autodetect when the env is unset and **exactly one** instance lives under `~/.spirit/`. No filesystem-search fallback, no `<__file__>/../config/...` candidates. Same resolution applies to `spirit.logger`'s instance-name resolution. (#733)
- **`spirit-preflight` autodetects the lone instance post-setup.** rc2 left a gap: after `spirit-setup` created `~/.spirit/<name>/`, `spirit-preflight` (no env var) still reported `[no-instance]` rc=1, treating the box as if setup never happened. Now matches `spirit-health`'s behaviour — single-instance boxes "just work" without needing to export `SPIRIT_INSTANCE`. (#733 follow-up)
- **`spirit-health` no longer labels never-started instances as "orphan".** Freshly-set-up instances with `spirit.yaml` but no `spirit.db` are now labelled `configured, not yet started` with a ○ icon, and the repair tip points at `spirit --mode paper` (not the non-existent `--instance` flag). True orphans (DB without config) keep their `?` icon. (#733 follow-up)
- **`spirit-setup --help` no longer drops into the wizard.** Argparse runs before the interactive flow now; `--help` prints usage and exits cleanly. `EOFError` from short piped stdin is also caught and falls through to defaults instead of producing a traceback. (#733 follow-up)
- **`spirit-setup` non-TTY stdin no longer emits getpass warnings.** Password prompts under piped stdin used to log `GetPassWarning: Can not control echo on the terminal` lines. Now we skip getpass entirely on non-TTY and just use `input()` — masking has no effect on a pipe anyway. (#733 follow-up)
- **`spirit-preflight` (and `spirit --mode paper` on paid tier) now auto-load `~/.spirit/<instance>/.env`.** The setup wizard writes secrets (SPIRIT_API_KEY, EXCHANGE_API_*) to that file; pre-rc4 nothing actually read them, which meant the documented Pro-key flip flow ("edit `.env`, re-run `spirit-preflight`, see ✓ Gateway") silently did nothing. Standard dotenv precedence: an env var already set in the shell wins over the file. (rc3 install test, Bug A)
- **Capability summary text reflects the actual tier.** Pre-rc4 the "Paper trading" line hard-coded "Free tier" even on a paid-tier instance. Now: Free → "Free tier, exchange-direct OHLC"; Plus → "Plus tier, gateway-backed"; Pro → "Pro tier, gateway-backed". (rc3 install test, Bug B)
- **Capability summary distinguishes key-missing from key-rejected.** Pre-rc4 a 401 from the gateway produced the same "✗ Gateway features (set SPIRIT_API_KEY, get one at portal.tradebot.live)" as having no key at all — confusing for users who DO have a key. Now: 401/403 → "key rejected — verify at portal.tradebot.live/keys"; network error → "gateway unreachable"; key absent → original "set SPIRIT_API_KEY" message. (rc3 install test, Bug C)
- **`spirit-health` now finds your installation regardless of instance name.** The v2.2.2.post2 version defaulted to looking for an instance called `prod`, so if your instance was named anything else (e.g. `local` from the setup wizard) the tool reported "Spirit doesn't appear to be installed" even though it was. `spirit-health` now auto-discovers every instance under `~/.spirit/` and shows per-instance state — DB, config, last trade, version stamp — with the active instance (resolved from `SPIRIT_INSTANCE`) clearly marked.
- **`spirit-preflight` no longer reports FATAL on a working paper-mode setup.** Running the standalone diagnostic with no Kraken keys on the Free tier previously printed misleading FATAL errors even though `spirit --mode paper` worked perfectly. Standalone preflight now runs in diagnostic mode — missing optional keys produce informational warnings, missing required keys (like `SPIRIT_STRATEGY`) still fail. The in-run preflight that gates `spirit --mode live` is unchanged.

### Upgrade notes

If you ran `spirit-setup` from a pre-#733 wheel (v2.2.2.post2 or earlier), a stale `spirit.yaml` may exist inside your pipx venv at `~/.local/share/pipx/venvs/spirit-platform/lib/python3.X/config/spirit.yaml`. v2.2.3 ignores that file and warns about it on first run; you can safely `rm` it. The CI gate (rc-validation.yml's upgrade-matrix) now asserts the wizard never writes there.

### Contract changes

`spirit-health` and `spirit-preflight` now publish formal exit-code contracts. Monitoring scripts that wire either tool into Datadog / Nagios / etc. SHOULD check against this matrix; the codes are stable from v2.2.3 onwards and any change is a MAJOR version bump.

**`spirit-health` (4 states):**

| Code | Constant | Meaning |
|------|----------|---------|
| 0 | `RC_HEALTHY` | At least one instance has a heartbeat within the running threshold |
| 1 | `RC_NO_INSTANCES` | No instances configured (fresh box — informational, not an error) |
| 2 | `RC_DEGRADED` | Instances exist but none has a recent heartbeat (stale / orphan / stopped) |
| 3 | `RC_INTERNAL_ERROR` | Uncaught exception inside the tool itself |

**`spirit-preflight` (3 states):**

| Code | Constant | Meaning |
|------|----------|---------|
| 0 | `RC_DIAGNOSTIC_OK` | No FATAL checks (paper-mode would start) |
| 1 | `RC_DIAGNOSTIC_BLOCKING` | At least one FATAL (paper-mode would not start) |
| 2 | `RC_INTERNAL_ERROR` | Uncaught exception inside the tool itself |

Pinned by `tests/test_spirit_health_contract.py` + `tests/test_spirit_preflight_contract.py`. Enforced by both `.github/workflows/publish.yml` and `.github/workflows/rc-validation.yml` against the exact expected codes for the CI environment. Full design at `docs/reference/MODULE_CONTRACTS.md`.

### Added

- **"Capabilities enabled" summary** at the bottom of `spirit-preflight` output — answers the question users actually have ("what can this instance do?") with ✓/✗/– markers for paper trading, live trading, and gateway features.
- **Orphan detection in `spirit-health`** — flags DB-without-config and config-without-DB states with the right repair command for each.

### Changed

- **README copy polish.** Install callout no longer references macOS (unverified at this stage; INSTALL.md is honest about that). D-Limit / V3 scorer / risk-gate callout reframed from "lives behind the gateway" to an upgrade path with a link to `tradebot.live` for tier details. "Bring your own exchange" section renamed to "Supported exchanges" and now invites GitHub issues for exchange-prioritisation requests.

### Notes

This release also adds a 5-phase release engineering process (`docs/RELEASE_ENGINEERING_PROCESS.md`) and three independent CI gates that protect every PyPI publish. Internal/process changes; no behavioural impact for installed users beyond making future releases harder to ship broken.


## 2.2.2 — 2026-05-17

### Fixed

- **Headline `spirit` command crashed on a fresh `pip install`.** The v2.2.1 wheel shipped with imports of modules that weren't in the public allowlist. Any invocation of the `spirit` console_script raised `ModuleNotFoundError` — first at module-load time (`spirit.data_types` and four siblings, fixed in the initial v2.2.2 push) and then at `main()` runtime (`spirit.utils.run_manager`, `spirit.context_manager`, `spirit.strategy_registry`, `spirit.web`, fixed in 2.2.2.post1). Plus a missing `spirit/storage/sqlite_schema.sql` data file in the wheel. `spirit --mode paper` now runs end-to-end.

### Added

- **`INSTALL.md`** at repo root, covering pipx-based install (the recommended path on PEP 668 distros: Ubuntu 23.04+, Debian 12+), venv fallback, verification, first-run steps, troubleshooting, upgrade, and uninstall. Supported platforms line marks macOS / Windows as unverified at this stage.
- **CI runtime smoke gate.** `publish.yml` now runs `spirit --mode test` against the built wheel before publishing — catches "wheel imports but main() crashes" regressions like the one above.

### Changed

- README quick start rewritten to lead with `pipx` and link to `INSTALL.md` for full instructions. The previous `pip install spirit-platform` line failed on modern Ubuntu / Debian.

### Notes

Initial publish of 2.2.2 (yanked) was missing several runtime-imported modules from the wheel; republished as 2.2.2.post1 the same day with the complete bundle and the new CI smoke gate. 2.2.2.post2 added the `spirit-setup` console script (was previously only invokable as `python -m spirit.setup`, which doesn't work on pipx installs because pipx isolates the package from the system Python). Anyone running `pip install spirit-platform` resolves to the latest 2.2.2.postN distribution.


## 2.2.1 — 2026-05-14

First public release. Spirit ships as `pip install spirit-platform` from PyPI. Free tier runs entirely on your machine against your own exchange keys; Plus and Pro extend with hosted D-Limit indicators, Bring-Your-Own-Data historical OHLC storage, and cloud-backed trade history with crash recovery.

### Added

#### Free-tier framework

- **Pluggable `DataProvider` routing** — Free uses local SQLite + your own Kraken keys; Plus continues with the API gateway. New modules: `SqliteDataProvider`, `ExchangeBackedDataProvider`, `CompositeDataProvider`, `pair_registry` static fallback. Setup wizard prompts for tier on first run. (#561 / #564)
- **Two bundled reference strategies** at `src/spirit/strategies/examples/`:
  - `sma_crossover` — minimum-viable demo. Subclass `BaseStrategy`, implement `evaluate_trade()`, nothing else. Read this first.
  - `macd_demo` — full lifecycle tour. Multi-interval ATR stop via `on_monitoring_tick`, entry-confirmed state-stash, RSI/SMA200 filters, paper-by-default guard. All indicators computed in pandas — no `spirit.indicators.*` IP dependency. (#695)
- **Strategy registry split** so the public mirror ships a working registry with the two bundled examples only. IP entries (zone_bounce, regime_engine, etc.) live in a private companion file imported with try/except. (#701)

#### Bring Your Own Data (BYOD) — Plus / Pro

- **`POST /v1/ohlc/upload`** — bulk CSV upload, Kraken CSV format native, multi-batch ingest with precise insert counts. (#681)
- **`POST /v1/ohlc/append`** — incremental per-candle push, used by Spirit's runtime push-on-fetch on Pro tier. (#680)
- **`GET /v1/ohlc/user`** — list uploaded batches with row counts and timestamps. (#681)
- **`DELETE /v1/ohlc/user`** — two forms (`?pair=X&interval=Y&confirm=true` and `/batches/{batch_id}?confirm=true`). Confirm-guarded with a row-count preview when omitted. RLS-scoped to your instance. (#690 / #692)
- **Tier-aware storage caps** — Plus 5M rows, Pro 25M rows, internal/admin unlimited. 413 response on exceeding cap, with the message pointing at the DELETE endpoint as the recovery path. (#691 / #693)
- **`python3 -m spirit.backfill`** — standalone CLI for re-importing OHLC without re-running the setup wizard. Filename inference (`XBTUSDT_60.csv` → pair=XBTUSDT, interval=60); explicit flags override. (#686)
- **Setup wizard CSV backfill prompt** — first-run wizard now asks whether to import Kraken CSV history when the API key carries `write:ohlc_user`. (#683)
- **Runtime push-on-fetch + cloud-first read routing** — when Pro tier strategies pull recent OHLC, the candles are simultaneously pushed to `/v1/ohlc/append` so cloud-side storage stays current without a separate sync job. New env knob `SPIRIT_OHLC_SOURCE` (`auto` / `cloud_first` / `local_first`). (#682)
- **Capability discovery + typed 403 + strategy preflight** — Spirit calls `/v1/whoami` at startup, intersects capabilities against the active strategy's `required_capabilities` property, and fails fast at startup rather than a 403 from a cold call site at 03:00 UTC. (#670)
- **Migration 036** — revokes raw OHLC capability from `subscription` and `pro` roles; new `internal_canary` role retains OHLC for dev/CI use during the BYOD transition. (#665 / #669)
- **Migration 037** — `user_ohlc_uploads` table + `write:ohlc_user` capability for paid tiers. (#675)

#### Customer-facing CLI

- **`spirit-uninstall` wizard** — clean removal of an install: stops the systemd unit (or SIGTERMs a bare `python -m spirit.main` process), removes the install tree, optionally purges local data and the spirit system user. Honest about scope — API key revocation stays the user's responsibility via the portal. (#591, #593, #595, #600)
- **`spirit-health` CLI** — one-screen liveness summary reading the local SQLite (process state, last heartbeat + age, version, last trade, total trades). (#592)
- **Periodic `[SPIRIT] alive` log line** every 30 minutes (configurable via `SPIRIT_ALIVE_LOG_INTERVAL_S`) so quiet strategies don't make a healthy install look hung. Independent of the DB heartbeat. (#592)
- **Orphan-process guard at startup** — refuses to start if another `spirit.main` is already running, with a `--allow-multi-instance` override for legitimate Pro setups. (#592)

#### Setup wizard

- **Questionary-based arrow-key UI**, with plain `input()` fallback for non-TTY. (#569)
- **Optional TradeBOT API key entry on Free** — stored locally, unused until the user upgrades; warm-on-ramp to Plus. (#569)
- **Three built-in strategy choices**: `sma_crossover`, `macd_demo`, `custom`. (#696)
- **Re-run pre-population** — running the wizard a second time reads existing `config/spirit.yaml` and offers current values as defaults. Previously every prompt reverted to its hardcoded default, silently overwriting a user's earlier answers. (#699)

#### Public-mirror + PyPI infrastructure

- **Allow-list-based mirror filter** at `docs/features/platform/PUBLIC_MIRROR_ALLOWLIST.txt`. Anything not listed is dropped from the public repo. (#562)
- **Mirror dry-run script** at `scripts/public_mirror_dryrun.sh` — runs the filter into `/tmp/spirit-mirror-dryrun/` plus a sensitive-substring scan. (#562)
- **Mirror push script** at `scripts/public_mirror_push.sh` — guarded wrapper around the production push. (#562)
- **LICENSE (Apache-2.0)**, **`README.md.public`**, and **`pyproject.toml.public`** with slimmed dependency set (heavy ML/PG/TA deps stripped). The push script renames `.public` files at filter time so the public mirror has a normal-looking tree. (#562)
- **Pip package name `spirit-platform`** matches the GitHub repo name. (#705)
- **GitHub Action `.github/workflows/publish.yml`** builds + publishes via OIDC trusted publishing — no stored API tokens. Routes RC tags (`v2.2.1-rc*`) to TestPyPI, full tags (`v2.2.1`) to PyPI. (#706)

#### Documentation

- **Release publishing runbook** at `docs/RELEASE_PUBLISHING_RUNBOOK.md` — versioning, cadence, pre-release gates, tag procedure, public-mirror push, customer comms, hotfix fast-path, rollback procedures, post-release verification. (#587 / #601)
- **`docs/reference/EXCHANGE_PLUGIN_GUIDE.md`** — protocol reference for writing a non-Kraken exchange adapter.
- **Two architecture blog posts** (queued for Ghost): *Inside Spirit Orchestrator* (orchestrator + DataProvider + RiskGate walkthrough) and *Anatomy of a Spirit Strategy* (line-by-line `macd_demo.py` walkthrough).

### Changed

- **Pip package name**: `spirit-trading` → `spirit-platform`. Matches the GitHub repo name (`timoz10/spirit-platform`); one less name for users to remember. (#705)
- **API keys no longer carry a tier prefix.** New keys are `sk_<token>` only; tier lives on the `api_keys.role` column. Existing keys with the old prefix continue to work — auth hashes the full string and looks up by hash. No DB migration required. Fixes the "user upgrades Free → Plus but key still says `sk_free_…`" UX problem. (#570 / #589)
- **Public URL is now the default.** `data_provider.py` and `preflight.py` previously defaulted `SPIRIT_API_URL` to a private-net IP (the gateway's internal address inside the cloud network). Defaults are now `https://api.tradebot.live/v1` — the public URL — so a fresh install just works without an explicit env var. Internal callers that still need the private endpoint set `SPIRIT_API_URL` explicitly. (#562 / #702)
- **`__version__` and `pyproject.toml` aligned with the public release tag.** Internal counter scheme (was bumping toward 2.11.0) replaced with the tag scheme (2.2.1) from this release onward. CI gate keeps the two strings in lock-step. (#568 / #588)
- **`spirit-uninstall` defaults to Free-tier-friendly mode** — reads the local SQLite directly to detect open positions instead of initialising the runtime DataProvider (which on Free tier dragged in the Kraken adapter just to read one row). Plus fall through to the DataProvider as before. (#600)
- **Shutdown log line** no longer says "Final state saved to PG for all pairs" (misleading on Free tier, which uses SQLite). Now: `Final state saved for all pairs (instance=<name>)`. (#594)
- **`KrakenOHLCBuffer` log lines** carry the pair + interval prefix instead of repeating identically across all buffers (`[KrakenOHLCBuffer][XBTUSD/60m] Background updater stopped.`). (#594)
- **Portal tier label** — shows "Plus" instead of the raw DB role name "subscription" on the portal welcome page. DB column unchanged; display only. (#684)

### Fixed

- **Orphan-process guard false-positive on shell/tmux wrappers.** `pgrep -f spirit.main` matched the launcher's parent processes (bash, tmux) when their argv contained the launch command string, so Spirit refused to start via the documented `tmux new-session "... python3 -m spirit.main ..."` pattern. Fixed by verifying `argv[0]` is a real Python interpreter or the installed `spirit` console script. (#698)
- **Free-tier provider reads yaml via `get_config`, not `os.environ` directly.** Setup wizard writes to `config/spirit.yaml`; the Free-tier provider builder previously read only env vars, so a wizard-set `SPIRIT_INSTANCE: test-vm` was silently dropped and the SQLite path fell back to `~/.spirit/local/`. (#699)
- **Strategy registry public/IP split** so the public mirror ships a working `strategy_config.py` without leaking IP module paths. Without this, the public ship would have included `macd_demo.py` without a registry entry that could load it. (#701)
- **`macd_demo` registration in `_STRATEGY_REGISTRY`** + wizard third-option entry. `SPIRIT_STRATEGY=macd_demo` now resolves correctly. (#696)
- **CSV upload serializer + bulk-insert rowcount accuracy.** `_user_candle_to_wire` dict branch read `"datetime"` but the CSV reader yields `"timestamp"`, so every CSV-derived candle hit the gateway with `"timestamp": "None"` → 422 across every chunk. Plus: `cur.rowcount` reflected only the last sub-batch under psycopg2's default `page_size=100`, so the gateway reported wildly wrong insert counts. (#687 / #688)
- **`SpiritContext` warmup primes every declared interval**, not just the primary. Pre-fix, monitoring intervals (e.g. 1m for ATR stops on `zone_bounce` / `macd_cross`) silently started cold for ~50 minutes after every restart. Affected any strategy with `monitoring_intervals` declared; `sma_crossover` was unaffected. (#572 / #575)
- **`spirit-uninstall --dry-run` now shows what a live run would do.** Detection of bare `spirit.main` processes was short-circuited in dry-run, giving a false impression that nothing would happen — even though a real run absolutely would SIGTERM the process. (#595)
- **`spirit-health` column-name drift.** Two SQL queries used PG-flavoured column names that don't match the canonical SQLite schema (`updated_at` vs `last_heartbeat`, `pnl_realized` vs `pnl_pct`). Errors were swallowed silently, so every health run reported "(none)" for the heartbeat AND the last trade even when real rows existed. Plus a regression gate that builds the test DB from the canonical schema file so future drift fails CI. (#595)
- **`spirit-uninstall` no longer prints "✓ Stopped + disabled spirit.service" on a no-systemd box.** (#600)
- **`spirit-uninstall` correctly detects bare `python -m spirit.main` processes** started outside systemd, and refuses (exit 3) to remove the install tree if any are still running unless `--force` is set. (#593)
- **Preflight default URL** now defaults to the public `https://api.tradebot.live/v1` instead of an internal-network address. Matches the other call sites. (#702)
- **`trade_types.py` shipped in the public mirror.** Both bundled example strategies import `TradeRecord` from this module; without it in the allow-list the examples would have shipped uninstantiable. (#703)
- **Public-README correctness pass** — corrected the `BaseStrategy` code snippet (was `SpiritStrategy` with wrong method signatures), dropped a dead `docs/reference/platform/` link, dropped a fake blog-post link, removed an internal-canary reference, switched commercial contact to `support@tradebot.live`. (#703, #704, #707, #708)
- **WS subscription roles**: `internal_canary` + `admin` can subscribe to paid channels (was incorrectly gated on `subscription`/`pro` only). (#671)
- **Internal-username sanitisation** in three framework docstring examples surviving the public mirror filter (`pipeline/daemon_health.py`, `utils/pair_registry.py`, `setup.py`) — instance-name examples now use a generic placeholder. (#562)

### Internal

- **Calibrators stop spamming ERROR on tier-gated 403s.** Bridge mitigation until `/v1/me` capability advertisement (#597 / #598) lands. Each of the 6 capability-gated calibrators (`cooldown`, `risk_gate`, `entry_quality`, `composite_threshold`, `thesis_writer`, `bounce_signature`) now detects 403, logs INFO once with an upgrade hint, and skips subsequent retries for the lifetime of the process. Pro/admin keys unaffected. (#599)
- **Canary API key bumped subscription → pro** (migration 035) so the canary exercises the full Pro stack including the calibration endpoints gated on `read:scorer`. (#596)
- **`runtime_lock.py` orphan-process detection** module — used by both startup (refuses to launch with another Spirit running) and uninstall (SIGTERMs bare processes before removing files). (#592 / #593, #698)
- **`capability_check.py` helper** — `is_403_forbidden`, `is_capability_denied`, `mark_capability_denied`. Process-lifetime de-dup so the same caller doesn't log the same tier-gap twice. (#599)
- **Portal chrome / styling**: sync with theme + new `docs/reference/platform/WEB_FRONTEND_ARCHITECTURE.md` (#656), account popout polish + wider for long emails (#657), "Spirit Portal" H1 (was "Portal") (#658).
- **`scripts/push_blog_post.py`** — convert markdown + POST to Ghost as draft. (#659)
- **Substantial test coverage added** — 44 SqliteDataProvider tests (Rule 11 type round-trip, schema migration, crash recovery, heartbeat, performance, DST regression), 29 uninstall tests, 19 health tests, 12 runtime-lock tests (incl. 11 covering the shell/tmux false-positive fix), 15 capability-check tests, 6 multi-interval warmup tests, 9 strategy-registration tests across sma_crossover + macd_demo, 6 strategy-registry split tests, 3 yaml-driven free-tier smoke tests, 8 wizard yaml-rerun tests.

### Tracked follow-ups (filed but not in this release)

| # | Topic |
|---|---|
| #577 | Portal: paid tiers not visible at signup; rename Subscriber → Plus |
| #579 | Portal: welcome email + key-issuance flow on tier upgrade |
| #580 | Platform: terms of service + privacy policy for paid tiers |
| #582 | Platform: public status / uptime page |
| #583 | Platform: monitoring + alerting stack for solo-on-call posture |
| #584 | Platform: Gateway patching + zero-downtime deploy runbook |
| #585 | Platform: In-flight update procedure for live instances |
| #586 | Platform: end-to-end paid-tier signup smoke test |
| #597 | Gateway: `GET /v1/me` endpoint — expose role + capabilities |
| #598 | Spirit: read `/v1/me` capabilities at startup, skip endpoints the tier doesn't allow |
| #417 (scope #2) | Customer-facing self-revoke API endpoint (scope #1 closed by #591/#593) |
| #697 | Setup: non-interactive mode for agent / CI / automation driven install |
| #700 | Setup: wizard re-run should detect existing instance dirs + offer data migration |


## 2.2.0 — 2026-05-05

First "stable platform" tag. Phase A capability-based tier enforcement on data routes + Phase B per-key daily quotas + Hetzner egress accounting + Kraken data licence request committed. Internal tag — public release was originally targeted here but slipped to 2.2.1 to incorporate the Free tier.

### Added

- **Phase A — capability-based tier enforcement** on all data routes. 19 capabilities, 32 role-capability mappings stored in `public.role_capabilities`. Migration 033. (#549 / #556)
- **Phase B — per-key daily quotas + Hetzner egress accounting**. New table `api_key_quota_daily` (per-key, per-day rollup). Quota lookup is a single PK hit; daily rollover is implicit. Migration 034. (#549 / #559 / #560)
- **Kraken data licence request** committed at `docs/Kraken-data-licence-request-2026-05-05.md`. Sent 2026-05-05.

### Fixed

- Replaced oversized `limit=` sentinels in `_trajectory_recover_if_needed` and `_expectation_recover_if_needed` with tight, documented caps (60,000 — ~42 days of 1m candles). Pre-fix, calls passed `limit=500_000` against a gateway capped at `le=200_000` → silent 422, swallowed by outer `try/except Exception`, recovery never fired on api-mode. (#425 / #548)

For pre-2.2.0 changes (versions 2.0 through 2.10.x — internal counter scheme, ML experiments, infra migrations), see `docs/features/spirit/SPIRIT_V2_ARCHITECTURE.md` § "Version History" and `docs/archive/CHANGELOG_PRE_PUBLIC.md`.

---

Release tags: https://github.com/timoz10/spirit-platform/releases
