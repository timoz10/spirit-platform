"""Spirit uninstall wizard.

Cleans up a local Spirit installation:
- stops + disables the systemd unit
- removes the install tree
- (interactive) removes ~/.spirit/<instance>/ local data
- (opt-in) removes the spirit system user

API key revocation is the security boundary and stays the user's
responsibility — the script prints a reminder pointing at
portal.tradebot.live where the key can be revoked manually. There is
no self-revoke API endpoint at v2.2.1; revocation is portal-driven.

Usage:
    python3 -m spirit.uninstall          # interactive
    spirit-uninstall                     # CLI entrypoint (same)
    python3 -m spirit.uninstall --dry-run

Default behaviour is interactive: confirms the global action, then
asks before removing local data. Each destructive step is idempotent
(skip-if-absent) so re-running on a partially uninstalled box is safe.

See docs/reference/infrastructure/SPIRIT_DECOMMISSION_RUNBOOK.md for
the manual fallback if this wizard fails partway through.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

INSTALL_TREE_DEFAULT = "/opt/spirit/kraken-bot"
SYSTEMD_UNIT_PATH = Path("/etc/systemd/system/spirit.service")
LOGROTATE_CFG_PATH = Path("/etc/logrotate.d/spirit")
LOG_DIR = Path("/var/log/spirit")
PORTAL_KEYS_URL = "https://portal.tradebot.live/keys"


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _systemctl_available() -> bool:
    """systemctl present on PATH."""
    return shutil.which("systemctl") is not None


def _service_is_active(unit: str = "spirit.service") -> bool:
    """Return True iff the systemd unit is currently active.

    Tolerates a missing systemctl (returns False).
    """
    if not _systemctl_available():
        return False
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "active"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _detect_open_positions(install_tree: Path) -> list[str]:
    """Best-effort: return list of pairs with open positions.

    Reads spirit_state via the runtime DataProvider if importable from
    the install tree. Returns an empty list on any failure (a missing
    or partially-uninstalled tree, an import error, no DB access, etc.)
    so we never block uninstall on a stale state file.
    """
    src = install_tree / "src"
    if not src.exists():
        return []

    saved_path = list(sys.path)
    try:
        sys.path.insert(0, str(src))
        try:
            from spirit.utils.data_provider import get_data_provider  # type: ignore[import]
        except Exception:
            return []
        try:
            dp = get_data_provider()
            state = dp.get_state("open_positions") or {}
            if isinstance(state, dict):
                return [pair for pair, pos in state.items() if pos]
            return []
        except Exception:
            return []
    finally:
        sys.path[:] = saved_path


def _detect_bare_spirit_processes() -> list[int]:
    """Return PIDs of spirit.main processes started outside systemd.

    Reuses ``runtime_lock.detect_other_spirit_processes`` so the wizard
    sees the same processes the startup guard sees. Returns [] on any
    import / detection failure so a partially-broken install doesn't
    block the uninstall.

    The systemd-active check (`_service_is_active`) ONLY catches
    processes managed by `spirit.service`. A user who started Spirit
    via `python3 -m spirit.main` directly (manual debug, no unit
    installed) won't show up there — without this check, the wizard
    would happily rm-rf the install tree out from under a live
    process.
    """
    try:
        from spirit.runtime_lock import detect_other_spirit_processes
    except Exception:
        return []
    try:
        return detect_other_spirit_processes()
    except Exception:
        return []


def _pid_alive(pid: int) -> bool:
    """True iff the process is still alive. Uses os.kill(pid, 0)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — still "alive" for our
        # purposes (we'll fail loudly rather than silently rm).
        return True


def stop_bare_processes(pids: list[int], dry_run: bool = False,
                        timeout_s: int = 30) -> bool:
    """SIGTERM each PID and wait up to ``timeout_s`` for graceful exit.

    Returns True iff all processes have exited by the deadline. False
    if any are still running (caller should refuse to remove the
    install tree in that case).
    """
    import signal
    import time

    if not pids:
        return True
    if dry_run:
        for pid in pids:
            print(f"  [DRY-RUN] Would SIGTERM PID {pid}")
        return True

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"  ✓ Sent SIGTERM to PID {pid}")
        except ProcessLookupError:
            print(f"  - PID {pid} already gone")
        except PermissionError:
            print(f"  ✗ Cannot signal PID {pid} (run as the owning user or root)")
            return False

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        still = [p for p in pids if _pid_alive(p)]
        if not still:
            print(f"  ✓ All bare Spirit processes exited cleanly")
            return True
        time.sleep(1)

    still = [p for p in pids if _pid_alive(p)]
    if still:
        print(f"  ✗ {len(still)} process(es) still running after {timeout_s}s: {still}")
        print(f"     Send SIGKILL manually if needed: kill -9 {' '.join(str(p) for p in still)}")
        return False
    return True


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------

def _confirm(prompt: str, default: bool = False) -> bool:
    """Yes/no prompt. Empty input returns `default`."""
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            ans = input(f"{prompt} {suffix}: ").strip().lower()
        except EOFError:
            return default
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  Please answer yes or no.")


def _print_step(msg: str) -> None:
    print(f"\n[{msg}]")


# ---------------------------------------------------------------------------
# Destructive operations (each idempotent + dry-run aware)
# ---------------------------------------------------------------------------

def archive_logs(target: Path, install_tree: Path, dry_run: bool) -> None:
    """tar.gz the log dir + .env to `target`."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        print(f"  [DRY-RUN] Would archive logs + .env → {target}")
        return
    with tarfile.open(target, "w:gz") as tar:
        if LOG_DIR.exists():
            tar.add(str(LOG_DIR), arcname="logs")
        env_file = install_tree / ".env"
        if env_file.exists():
            tar.add(str(env_file), arcname=".env")
    print(f"  ✓ Archived logs + .env → {target}")


def stop_systemd_unit(unit: str = "spirit.service", dry_run: bool = False) -> None:
    """Stop + disable the systemd unit. Tolerates not-present."""
    if not _systemctl_available():
        print("  - systemctl not available (skipping)")
        return
    if dry_run:
        print(f"  [DRY-RUN] Would: systemctl stop {unit} && systemctl disable {unit}")
        return
    subprocess.run(["systemctl", "stop", unit], capture_output=True, check=False)
    subprocess.run(["systemctl", "disable", unit], capture_output=True, check=False)
    print(f"  ✓ Stopped + disabled {unit}")


def remove_systemd_files(dry_run: bool = False) -> None:
    """Remove unit file + logrotate config; reload daemon."""
    for p in (SYSTEMD_UNIT_PATH, LOGROTATE_CFG_PATH):
        if not p.exists():
            print(f"  - {p} already absent")
            continue
        if dry_run:
            print(f"  [DRY-RUN] Would remove {p}")
            continue
        try:
            p.unlink()
            print(f"  ✓ Removed {p}")
        except PermissionError:
            print(f"  ✗ Could not remove {p} (try with sudo)")
    if not dry_run and _systemctl_available():
        subprocess.run(["systemctl", "daemon-reload"],
                       capture_output=True, check=False)


def remove_install_tree(install_tree: Path, dry_run: bool = False) -> None:
    """rm -rf the install tree."""
    if not install_tree.exists():
        print(f"  - {install_tree} already absent")
        return
    if dry_run:
        print(f"  [DRY-RUN] Would remove {install_tree}")
        return
    try:
        shutil.rmtree(install_tree)
        print(f"  ✓ Removed {install_tree}")
    except PermissionError:
        print(f"  ✗ Could not remove {install_tree} (try with sudo)")


def remove_local_data(instance: str, dry_run: bool = False) -> None:
    """rm -rf ~/.spirit/<instance>/."""
    data_dir = Path.home() / ".spirit" / instance
    if not data_dir.exists():
        print(f"  - {data_dir} already absent")
        return
    if dry_run:
        print(f"  [DRY-RUN] Would remove {data_dir}")
        return
    try:
        shutil.rmtree(data_dir)
        print(f"  ✓ Removed {data_dir}")
    except PermissionError:
        print(f"  ✗ Could not remove {data_dir} (file owned by another user?)")


def remove_system_user(dry_run: bool = False) -> None:
    """userdel the spirit system user."""
    if shutil.which("userdel") is None:
        print("  - userdel not available (skipping)")
        return
    if dry_run:
        print("  [DRY-RUN] Would: userdel spirit")
        return
    result = subprocess.run(["userdel", "spirit"],
                            capture_output=True, text=True, check=False)
    if result.returncode == 0:
        print("  ✓ Removed spirit system user")
    elif "does not exist" in (result.stderr or ""):
        print("  - spirit system user already absent")
    else:
        print(f"  ✗ userdel failed: {result.stderr.strip() or 'unknown error'}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="spirit-uninstall",
        description="Remove a Spirit installation (systemd + install tree, "
                    "optional local data + system user). Does NOT revoke your "
                    "API key — visit portal.tradebot.live/keys for that.",
    )
    p.add_argument("--install-tree", default=INSTALL_TREE_DEFAULT,
                   help="Path to the install tree (default: %(default)s)")
    p.add_argument("--instance",
                   default=os.environ.get("SPIRIT_INSTANCE", ""),
                   help="Instance name; scopes ~/.spirit/<instance>/ removal "
                        "(default: $SPIRIT_INSTANCE)")
    p.add_argument("--archive-to", type=Path, default=None,
                   help="Tar logs + .env to this path before removing")
    p.add_argument("--purge-data", action="store_true",
                   help="Skip the prompt; remove ~/.spirit/<instance>/")
    p.add_argument("--keep-data", action="store_true",
                   help="Skip the prompt; keep ~/.spirit/<instance>/")
    p.add_argument("--purge-user", action="store_true",
                   help="Also remove the spirit system user")
    p.add_argument("--force", action="store_true",
                   help="Skip the open-positions check")
    p.add_argument("--dry-run", action="store_true",
                   help="Print plan; do nothing")
    p.add_argument("--yes", action="store_true",
                   help="Skip the global confirmation prompt")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.purge_data and args.keep_data:
        parser.error("--purge-data and --keep-data are mutually exclusive")

    install_tree = Path(args.install_tree)
    instance = args.instance.strip()

    print()
    print("Spirit uninstall wizard")
    print("=" * 70)
    print(f"  Install tree:   {install_tree}")
    print(f"  Instance:       {instance or '(unset — local data prompt skipped)'}")
    print(f"  Mode:           {'DRY-RUN (no changes)' if args.dry_run else 'LIVE'}")
    print("=" * 70)

    # 1. Pre-flight: open positions
    if not args.force:
        open_pairs = _detect_open_positions(install_tree)
        if open_pairs:
            print()
            print(f"⚠  Open positions detected: {', '.join(open_pairs)}")
            print(f"   Close them first, or pass --force to override.")
            return 2

    # 1b. Pre-flight: running systemd service
    if _service_is_active():
        print()
        print("⚠  spirit.service is currently active (will be stopped).")
        if not args.yes and not args.dry_run:
            if not _confirm("Stop it now and continue?", default=True):
                print("Aborted.")
                return 1

    # 1c. Pre-flight: bare Spirit processes (manual `python3 -m spirit.main`,
    #     not under systemd). The systemd check above misses these — without
    #     this guard the wizard would rm-rf the install tree while a live
    #     process is still loading config/yaml from it.
    #
    # Run detection in --dry-run too, so the user sees what a live run
    # would do. `stop_bare_processes` has its own dry-run path that
    # prints "Would SIGTERM PID X" without sending the signal.
    bare_pids = _detect_bare_spirit_processes()
    if bare_pids:
        print()
        print(f"⚠  Bare Spirit process(es) running outside systemd: PID {bare_pids}")
        print("   These won't be stopped by `systemctl stop spirit`.")
        if args.dry_run:
            print("   The wizard would SIGTERM them before removing files (dry-run).")
        else:
            print("   The wizard will SIGTERM them directly before removing files.")
        if not args.yes and not args.dry_run and \
           not _confirm("Stop them now and continue?", default=True):
            print("Aborted.")
            return 1

    # 2. Global confirmation
    if not args.yes and not args.dry_run:
        print()
        if not _confirm(f"This will remove {install_tree}. Continue?",
                        default=False):
            print("Aborted.")
            return 1

    # 3. Archive logs (if requested)
    if args.archive_to is not None:
        _print_step("step 1/n — archive logs + .env")
        archive_logs(args.archive_to, install_tree, args.dry_run)

    # 4. Stop systemd unit
    _print_step("stop spirit.service")
    stop_systemd_unit(dry_run=args.dry_run)

    # 4b. SIGTERM any bare processes detected at pre-flight. We do this
    #     *between* the systemd stop and the rm so that everything that
    #     could be running has a chance to exit cleanly.
    if bare_pids:
        _print_step(f"SIGTERM bare Spirit processes ({len(bare_pids)})")
        ok = stop_bare_processes(bare_pids, dry_run=args.dry_run)
        if not ok and not args.force:
            print()
            print("✗ Some Spirit processes are still running. Refusing to remove the")
            print("  install tree while a live process holds modules in memory.")
            print("  SIGKILL them manually or pass --force, then re-run.")
            return 3

    # 5. Remove systemd + logrotate files
    _print_step("remove systemd + logrotate files")
    remove_systemd_files(dry_run=args.dry_run)

    # 6. Remove install tree
    _print_step("remove install tree")
    remove_install_tree(install_tree, dry_run=args.dry_run)

    # 7. Local data — interactive unless --purge-data / --keep-data
    purge_data = args.purge_data
    if not args.purge_data and not args.keep_data and instance:
        data_dir = Path.home() / ".spirit" / instance
        if data_dir.exists() and not args.dry_run:
            print()
            print(f"Local data at {data_dir} contains your strategy results,")
            print("paper-trade history, heartbeat record, and crash-recovery state.")
            purge_data = _confirm("Remove all local data?", default=False)

    if purge_data and instance:
        _print_step(f"remove local data at ~/.spirit/{instance}/")
        remove_local_data(instance, dry_run=args.dry_run)
    elif args.keep_data and instance:
        print(f"\n  - Keeping ~/.spirit/{instance}/ as requested")

    # 8. System user (only if --purge-user)
    if args.purge_user:
        _print_step("remove spirit system user")
        remove_system_user(dry_run=args.dry_run)

    # 9. Summary + key revocation reminder
    print()
    print("=" * 70)
    if args.dry_run:
        print("DRY-RUN complete. No changes were made.")
    else:
        print("Uninstall complete.")
    print("=" * 70)
    print()
    print("⚠  Your API key is NOT revoked.")
    print(f"   Visit {PORTAL_KEYS_URL} to revoke it manually.")
    print("   Until you do, the key stays valid against the gateway —")
    print("   anyone who has a copy could continue using it.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
