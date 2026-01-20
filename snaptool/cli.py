from __future__ import annotations

import argparse
import datetime
import logging
import shlex
import subprocess
from pathlib import Path

from .adb import AdbClient
from .android_state import AndroidStateReader
from .config import SnapshotPaths, ToolConfig
from .executor import ExecConfig, RestoreExecutor
from .logging_setup import setup_logging
from .planner import RestorePlanner
from .policy import RestorePolicy
from .runner import run_checked
from .tar_index import TarIndexer


def make_snapshot_name() -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"snap-{ts}"


def cmd_backup(args) -> int:
    import time
    
    backup_start = time.time()
    
    cfg = ToolConfig.default(adb_serial=args.serial, verbose=args.verbose, snap_root=args.snap_root)
    cfg.snap_root.mkdir(parents=True, exist_ok=True)

    snap_name = args.name or make_snapshot_name()
    paths = SnapshotPaths.for_snapshot(cfg.snap_root, snap_name)
    paths.snap_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    logger = setup_logging(cfg.verbose, log_file=paths.logs_dir / "backup.log")
    adb = AdbClient(logger=logger, serial=cfg.adb_serial)

    device_tar = "/data/local/tmp/data-backup.tar"
    local_tar = paths.snap_dir / "data.tar"

    logger.info("Creating snapshot '%s' in %s", snap_name, paths.snap_dir)

    # EXACT recovery6.py style: adb shell + su inside script
    script = f"""
su
cd /
tar \\
  --exclude=data/local/tmp/data-backup.tar \\
  --exclude=data/dalvik-cache \\
  --exclude=data/cache \\
  --exclude=data/anr \\
  --exclude=data/tombstones \\
  --exclude=data/system/dropbox \\
  -cpf {device_tar} \\
  data
exit
exit
"""
    tar_start = time.time()
    logger.info("Creating tar archive on device...")
    adb.shell_script(script, allow_fail=True)
    tar_elapsed = time.time() - tar_start
    logger.info("Device tar creation time: %.2f seconds (%.2f minutes)", tar_elapsed, tar_elapsed / 60)

    logger.info("Verifying device tar exists and is non-empty...")
    verify_start = time.time()
    res = subprocess.run(
        ["adb"] + (["-s", cfg.adb_serial] if cfg.adb_serial else []) +
        ["shell", "su", "-c", f"ls -l {shlex.quote(device_tar)} 2>/dev/null || true"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="ignore",
        check=False,
    )
    if not res.stdout or "data-backup.tar" not in res.stdout:
        logger.error("Device tar not found; backup failed.")
        return 1
    
    # Extract file size from ls output
    import re
    match = re.search(r'\s(\d+)\s', res.stdout)
    if match:
        device_tar_size = int(match.group(1))
        size_gb = device_tar_size / (1024**3)
        logger.info("Device tar size: %.2f GB", size_gb)
    verify_elapsed = time.time() - verify_start
    logger.info("Verification time: %.2f seconds", verify_elapsed)

    pull_start = time.time()
    logger.info("Pulling device tar to host...")
    adb.adb(["pull", device_tar, str(local_tar)], check=True)
    pull_elapsed = time.time() - pull_start
    if match:
        pull_speed_mbps = (size_gb * 1024) / pull_elapsed if pull_elapsed > 0 else 0
        logger.info("Pull time: %.2f seconds (%.2f MB/s)", pull_elapsed, pull_speed_mbps)
    else:
        logger.info("Pull time: %.2f seconds", pull_elapsed)

    compress_start = time.time()
    logger.info("Compressing with zstd...")
    run_checked(["zstd", "-T0", "-3", "-f", str(local_tar), "-o", str(paths.archive_zst)], logger)
    compress_elapsed = time.time() - compress_start
    logger.info("Compression time: %.2f seconds (%.2f minutes)", compress_elapsed, compress_elapsed / 60)

    cleanup_start = time.time()
    logger.info("Removing device tar...")
    adb.shell_root(f"rm -f {shlex.quote(device_tar)}", check=False)

    logger.info("Cleaning up host temp tar...")
    try:
        local_tar.unlink()
    except FileNotFoundError:
        pass
    cleanup_elapsed = time.time() - cleanup_start
    logger.info("Cleanup time: %.2f seconds", cleanup_elapsed)

    backup_total = time.time() - backup_start
    
    logger.info("=" * 60)
    logger.info("BACKUP TIMING SUMMARY")
    logger.info("=" * 60)
    logger.info("Device tar creation: %.2f seconds (%.2f minutes)", tar_elapsed, tar_elapsed / 60)
    logger.info("Verification:        %.2f seconds", verify_elapsed)
    logger.info("Pull from device:    %.2f seconds", pull_elapsed)
    if match:
        logger.info("  Transfer speed:    %.2f MB/s", pull_speed_mbps)
    logger.info("Compression:         %.2f seconds (%.2f minutes)", compress_elapsed, compress_elapsed / 60)
    logger.info("Cleanup:             %.2f seconds", cleanup_elapsed)
    logger.info("-" * 60)
    logger.info("TOTAL:               %.2f seconds (%.2f minutes)", backup_total, backup_total / 60)
    logger.info("=" * 60)
    
    logger.info("Backup complete: %s", paths.archive_zst)
    return 0


def cmd_restore_path(args) -> int:
    import time
    
    script_start = time.time()
    
    cfg = ToolConfig.default(adb_serial=args.serial, verbose=args.verbose, snap_root=args.snap_root)
    paths = SnapshotPaths.for_snapshot(cfg.snap_root, args.snapshot)

    if not paths.archive_zst.is_file():
        raise SystemExit(f"[!] Archive not found: {paths.archive_zst}")

    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(cfg.verbose, log_file=paths.logs_dir / "restore-path.log")

    adb = AdbClient(logger=logger, serial=cfg.adb_serial)

    logger.info("Using snapshot: %s", paths.snap_dir)
    
    # Time decompression
    decompress_start = time.time()
    logger.info("Decompressing zstd archive -> temp tar...")
    run_checked(["zstd", "-d", "-f", str(paths.archive_zst), "-o", str(paths.temp_tar)], logger)
    decompress_elapsed = time.time() - decompress_start
    logger.info("Decompression time: %.2f seconds (%.2f minutes)", decompress_elapsed, decompress_elapsed / 60)

    # Time tar indexing
    index_start = time.time()
    logger.info("Building tar index...")
    tar_index = TarIndexer(logger).build_from_tar(paths.temp_tar)
    index_elapsed = time.time() - index_start
    logger.info("Tar indexing time: %.2f seconds", index_elapsed)

    # Time device state reading
    state_start = time.time()
    logger.info("Reading device state...")
    state = AndroidStateReader(adb, logger)
    device_state = state.read_device_state()
    state_elapsed = time.time() - state_start
    logger.info("Device state reading time: %.2f seconds", state_elapsed)

    # Time planning
    plan_start = time.time()
    policy = RestorePolicy()
    planner = RestorePlanner(logger, policy, state)
    plan = planner.build_plan(tar_index, device_state, pkg_scope=args.pkg_scope)
    plan_elapsed = time.time() - plan_start
    logger.info("Planning time: %.2f seconds", plan_elapsed)

    # Time restore execution (timing is done internally in executor)
    restore_start = time.time()
    execu = RestoreExecutor(adb, logger, ExecConfig(chunk_size=120))
    execu.exec_restore_path(plan, tar_index, local_tar=paths.temp_tar)
    restore_elapsed = time.time() - restore_start
    logger.info("Restore execution time: %.2f seconds (%.2f minutes)", restore_elapsed, restore_elapsed / 60)

    logger.info("Cleaning up host temp tar...")
    cleanup_start = time.time()
    try:
        paths.temp_tar.unlink()
    except FileNotFoundError:
        pass
    cleanup_elapsed = time.time() - cleanup_start
    logger.info("Cleanup time: %.2f seconds", cleanup_elapsed)

    script_total = time.time() - script_start
    logger.info("=" * 60)
    logger.info("TIMING SUMMARY")
    logger.info("=" * 60)
    logger.info("Decompression:    %.2f seconds (%.2f minutes)", decompress_elapsed, decompress_elapsed / 60)
    logger.info("Tar indexing:     %.2f seconds", index_elapsed)
    logger.info("Device state:     %.2f seconds", state_elapsed)
    logger.info("Planning:         %.2f seconds", plan_elapsed)
    logger.info("Restore:          %.2f seconds (%.2f minutes)", restore_elapsed, restore_elapsed / 60)
    logger.info("Cleanup:          %.2f seconds", cleanup_elapsed)
    logger.info("-" * 60)
    logger.info("TOTAL:            %.2f seconds (%.2f minutes)", script_total, script_total / 60)
    logger.info("=" * 60)
    
    logger.info("Restore-path complete.")
    return 0


# def cmd_restore_full(args) -> int:
#     cfg = ToolConfig.default(adb_serial=args.serial, verbose=args.verbose, snap_root=args.snap_root)
#     paths = SnapshotPaths.for_snapshot(cfg.snap_root, args.snapshot)

#     if not paths.archive_zst.is_file():
#         raise SystemExit(f"[!] Archive not found: {paths.archive_zst}")

#     paths.logs_dir.mkdir(parents=True, exist_ok=True)
#     logger = setup_logging(cfg.verbose, log_file=paths.logs_dir / "restore-full.log")

#     adb = AdbClient(logger=logger, serial=cfg.adb_serial)

#     logger.warning("=== DANGER: FULL /data RESTORE ===")
#     if not args.yes:
#         answer = input("Type YES to continue: ")
#         if answer.strip() != "YES":
#             logger.info("Aborting restore-full.")
#             return 1

#     device_tar = "/data/local/tmp/restore-full.tar"
#     local_tar = paths.snap_dir / "restore-full.tar"

#     logger.info("Decompressing zstd archive -> temp tar...")
#     run_checked(["zstd", "-d", "-f", str(paths.archive_zst), "-o", str(local_tar)], logger)

#     logger.info("Pushing temp tar to device...")
#     adb.adb(["push", str(local_tar), device_tar], check=True)

#     execu = RestoreExecutor(adb, logger, ExecConfig(chunk_size=120))
#     execu.exec_restore_full(device_tar=device_tar)

#     adb.shell_root(f"rm -f {shlex.quote(device_tar)}", check=False)
#     try:
#         local_tar.unlink()
#     except FileNotFoundError:
#         pass

#     logger.info("restore-full complete. Reboot is strongly recommended.")
#     return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Android /data backup & restore helper")
    parser.add_argument("--serial", help="adb device serial (optional)")
    parser.add_argument("--snap-root", help="Override snapshots directory (optional)")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_backup = sub.add_parser("backup", help="Create a snapshot")
    p_backup.add_argument("--name", help="Optional snapshot name")
    p_backup.set_defaults(func=cmd_backup)

    p_restore_path = sub.add_parser("restore-path", help="Restore selected paths (apps/media) from snapshot")
    p_restore_path.add_argument("snapshot", help="Snapshot name")
    p_restore_path.add_argument(
        "--pkg-scope",
        choices=["apps", "all", "system", "thirdparty"],
        default="apps",
        help="apps=installed minus overlays; all=all installed; system=system pkgs only; thirdparty=non-system pkgs only",
    )
    p_restore_path.set_defaults(func=cmd_restore_path)

    # p_restore_full = sub.add_parser("restore-full", help="Restore full /data from snapshot (DANGEROUS)")
    # p_restore_full.add_argument("snapshot", help="Snapshot name")
    # p_restore_full.add_argument("--yes", action="store_true")
    # p_restore_full.set_defaults(func=cmd_restore_full)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
