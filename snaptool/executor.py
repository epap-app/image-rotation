from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass

from .adb import AdbClient
from .planner import RestorePlan
from .tar_index import TarIndex


@dataclass(frozen=True)
class ExecConfig:
    chunk_size: int = 120


class RestoreExecutor:
    def __init__(self, adb: AdbClient, logger: logging.Logger, exec_cfg: ExecConfig):
        self.adb = adb
        self.logger = logger
        self.cfg = exec_cfg

    @staticmethod
    def _parse_uid_pkg_from_path(p: str):
        import re
        m = re.match(r"^data/user/(\d+)/([^/]+)$", p)
        if m:
            return int(m.group(1)), m.group(2)
        m = re.match(r"^data/user_de/(\d+)/([^/]+)$", p)
        if m:
            return int(m.group(1)), m.group(2)
        m = re.match(r"^data/media/(\d+)/Android/data/([^/]+)$", p)
        if m:
            return int(m.group(1)), m.group(2)
        m = re.match(r"^data/media/(\d+)/Android/media/([^/]+)$", p)
        if m:
            return int(m.group(1)), m.group(2)
        m = re.match(r"^data/media/(\d+)/Android/obb/([^/]+)$", p)
        if m:
            return int(m.group(1)), m.group(2)
        return None

    @staticmethod
    def _is_media_root_allow_empty(p: str) -> bool:
        # Allow empty DCIM/Pictures (snapshot may intentionally have none)
        return bool(__import__("re").match(r"^data/media/\d+/(DCIM|Pictures)$", p))

    def _check_adb_connection(self) -> None:
        """Check ADB connection health before large file transfers."""
        import subprocess
        
        self.logger.info("Checking ADB connection health...")
        
        # Check ADB version
        try:
            result = subprocess.run(["adb", "version"], capture_output=True, text=True, check=False)
            if result.returncode == 0:
                version_line = result.stdout.split('\n')[0] if result.stdout else "unknown"
                self.logger.info("ADB version: %s", version_line.strip())
        except Exception as e:
            self.logger.warning("Could not check ADB version: %s", e)
        
        # Check device connection
        try:
            devices_result = self.adb.adb(["devices", "-l"], check=False)
            self.logger.info("Connected devices:\n%s", devices_result.stdout.strip())
        except Exception as e:
            self.logger.warning("Could not list devices: %s", e)
        
        # Check USB connection speed if available
        try:
            usb_speed = self.adb.shell_root("getprop sys.usb.config", check=False)
            if usb_speed.stdout.strip():
                self.logger.info("USB config: %s", usb_speed.stdout.strip())
        except Exception as e:
            self.logger.warning("Could not check USB config: %s", e)

    def _push_with_verification(self, local_path: str, device_path: str, max_retries: int = 3) -> None:
        """
        Push a file to device with verification fallback for large files.
        Handles the 'failed to read copy response' ADB bug and disk space issues.
        """
        import os
        import re
        import subprocess
        import time
        
        # Get local file size for verification
        local_size = os.path.getsize(local_path)
        self.logger.info("Local file size: %d bytes (%.2f GB)", local_size, local_size / (1024**3))
        
        # Check ADB connection health before large transfers
        if local_size > 100 * 1024**2:  # > 100MB
            self._check_adb_connection()
        
        # Check available space on device before pushing
        df_result = self.adb.shell_root("df /data/local/tmp | tail -1 | awk '{print $4}'", check=False)
        try:
            # Available space in KB (from df output)
            available_kb = int(df_result.stdout.strip())
            available_bytes = available_kb * 1024
            self.logger.info("Device available space: %.2f GB", available_bytes / (1024**3))
            
            # Require at least 1.2x file size for safety (buffer + overhead)
            required_bytes = int(local_size * 1.2)
            if available_bytes < required_bytes:
                raise RuntimeError(
                    f"Insufficient space on device: need {required_bytes/(1024**3):.2f}GB, "
                    f"have {available_bytes/(1024**3):.2f}GB"
                )
        except (ValueError, AttributeError) as e:
            self.logger.warning("Could not check available space: %s", e)
        
        # Try push with retries
        last_exception = None
        for attempt in range(1, max_retries + 1):
            try:
                self.logger.info("Push attempt %d/%d...", attempt, max_retries)
                
                # For large files, restart ADB server before each attempt to avoid stale connections
                if attempt > 1 and local_size > 1024**3:  # > 1GB
                    self.logger.info("Restarting ADB server to clear stale connections...")
                    subprocess.run(["adb", "kill-server"], check=False, capture_output=True)
                    time.sleep(2)
                    subprocess.run(["adb", "start-server"], check=False, capture_output=True)
                    time.sleep(3)
                
                # Try normal push with check=True
                self.adb.adb(["push", str(local_path), device_path], check=True)
                self.logger.info("Push completed successfully")
                return
                
            except subprocess.CalledProcessError as e:
                last_exception = e
                
                # Log the full error details for diagnosis
                self.logger.error("ADB push failed on attempt %d with exit code %d", attempt, e.returncode)
                self.logger.error("Command: %s", e.cmd)
                if e.stdout:
                    self.logger.error("stdout: %s", e.stdout)
                if e.stderr:
                    self.logger.error("stderr: %s", e.stderr)
                
                # Check if this is the "failed to read copy response" error
                stderr = e.stderr or ""
                
                # Look for successful transfer indicators in stderr
                # Format: "X file pushed, Y skipped. Z MB/s (BYTES bytes in TIME)"
                transfer_pattern = r"(\d+)\s+file\s+pushed.*?\((\d+)\s+bytes\s+in\s+[\d.]+s\)"
                match = re.search(transfer_pattern, stderr)
                
                if match and "failed to read copy response" in stderr:
                    files_pushed = int(match.group(1))
                    bytes_transferred = int(match.group(2))
                    
                    self.logger.warning("ADB push returned error but reports transfer completion:")
                    self.logger.warning("  Files pushed: %d", files_pushed)
                    self.logger.warning("  Bytes reported: %d (%.2f GB)", 
                                      bytes_transferred, bytes_transferred / (1024**3))
                    
                    # Verify the file actually exists on device with correct size
                    self.logger.info("Verifying file on device...")
                    verify_result = self.adb.shell_root(
                        f"[ -f {shlex.quote(device_path)} ] && stat -c '%s' {shlex.quote(device_path)} 2>/dev/null || echo 0",
                        check=False
                    )
                    
                    try:
                        device_size = int(verify_result.stdout.strip())
                    except (ValueError, AttributeError):
                        device_size = 0
                    
                    self.logger.info("Device file size: %d bytes (%.2f GB)", device_size, device_size / (1024**3))
                    
                    if device_size == local_size:
                        self.logger.info("File verified: device size matches local file")
                        return
                    elif device_size == bytes_transferred:
                        self.logger.info("File verified: device size matches ADB report")
                        return
                    elif device_size > 0 and abs(device_size - local_size) < 1024:
                        self.logger.info("Size difference within tolerance, proceeding")
                        return
                    else:
                        # Incomplete transfer
                        completion_pct = (device_size / local_size) * 100 if local_size > 0 else 0
                        self.logger.error("Incomplete transfer: %.1f%% complete (%d/%d bytes)",
                                        completion_pct, device_size, local_size)
                        
                        # Clean up partial file before retry
                        self.logger.info("Removing incomplete file from device...")
                        self.adb.shell_root(f"rm -f {shlex.quote(device_path)}", check=False)
                        
                        if attempt < max_retries:
                            self.logger.info("Retrying after incomplete transfer...")
                            time.sleep(5)
                            continue
                        else:
                            raise RuntimeError(
                                f"Push failed after {max_retries} attempts: only {device_size/(1024**3):.2f}GB of "
                                f"{local_size/(1024**3):.2f}GB transferred ({completion_pct:.1f}%)"
                            )
                else:
                    # Different error or no transfer pattern found
                    if attempt < max_retries:
                        self.logger.warning("Retrying after %d seconds...", 5 * attempt)
                        time.sleep(5 * attempt)
                    else:
                        raise
        
        # If we've exhausted all retries, raise the last exception
        if last_exception:
            raise last_exception

    def _safe_media_refresh(self, plan: RestorePlan) -> None:
        self.logger.info("Post-restore: safe media refresh (Android 13) ...")
        lines = ["su", "set -e", ""]

        for uid in plan.user_ids:
            lines += [
                f"am force-stop --user {uid} com.android.providers.media >/dev/null 2>&1 || true",
                f"am force-stop --user {uid} {shlex.quote(plan.photos_pkg)} >/dev/null 2>&1 || true",
                f"rm -rf /data/media/{uid}/DCIM/.thumbnails >/dev/null 2>&1 || true",
                f"rm -rf /data/media/{uid}/Pictures/.thumbnails >/dev/null 2>&1 || true",
                f"rm -rf /data/media/{uid}/.thumbnails >/dev/null 2>&1 || true",
                f"restorecon -RF /data/media/{uid} >/dev/null 2>&1 || true",
            ]

        lines += [
            "",
            "if cmd media_store >/dev/null 2>&1; then",
            "  cmd media_store scan --volume external >/dev/null 2>&1 || true",
            "fi",
            "",
            "monkey -p com.android.providers.media 1 >/dev/null 2>&1 || true",
            f"monkey -p {shlex.quote(plan.photos_pkg)} 1 >/dev/null 2>&1 || true",
            "exit",
            "exit",
            "",
        ]
        self.adb.shell_script("\n".join(lines), allow_fail=True)

    def exec_restore_path(self, plan: RestorePlan, tar_index: TarIndex, local_tar, device_tar: str = "/data/local/tmp/restore.tar") -> None:
        import time
        import os
        
        ch = self.cfg.chunk_size
        overall_start = time.time()

        # Clean up old tar files to ensure sufficient space
        self.logger.info("Cleaning up old tar files on device...")
        cleanup_start = time.time()
        self.adb.shell_root(f"rm -f {shlex.quote(device_tar)} /data/local/tmp/*.tar /data/local/tmp/restore.tar.part* 2>/dev/null || true", check=False)
        cleanup_elapsed = time.time() - cleanup_start
        self.logger.info("Cleanup time: %.2f seconds", cleanup_elapsed)
        
        # Check file size and split if > 10GB to work around 16GB ADB transfer limit
        local_tar_path = str(local_tar)
        file_size = os.path.getsize(local_tar_path)
        size_gb = file_size / (1024**3)
        
        if file_size > 10 * (1024**3):  # > 10GB
            self.logger.info("Large file detected (%.2f GB) - splitting to work around 16GB ADB limit", size_gb)
            
            # Split into 8GB chunks to stay well under 16GB limit
            chunk_size = 8 * (1024**3)
            chunks = []
            chunk_num = 0
            total_push_time = 0
            total_read_write_time = 0
            
            with open(local_tar_path, 'rb') as f:
                while True:
                    chunk_num += 1
                    chunk_local = f"{local_tar_path}.part{chunk_num:03d}"
                    chunk_device = f"{device_tar}.part{chunk_num:03d}"
                    
                    # Time reading and writing chunk
                    rw_start = time.time()
                    self.logger.info("Reading chunk %d from tar...", chunk_num)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    
                    # Write chunk locally
                    with open(chunk_local, 'wb') as chunk_file:
                        chunk_file.write(data)
                    rw_elapsed = time.time() - rw_start
                    total_read_write_time += rw_elapsed
                    
                    chunk_mb = len(data) / (1024**2)
                    chunk_gb = len(data) / (1024**3)
                    self.logger.info("Chunk %d read/write: %.2f seconds (%.2f GB)", chunk_num, rw_elapsed, chunk_gb)
                    
                    # Time the push
                    push_start = time.time()
                    self.logger.info("Pushing chunk %d (%.0f MB) to device...", chunk_num, chunk_mb)
                    self.adb.adb(["push", chunk_local, chunk_device], check=True)
                    push_elapsed = time.time() - push_start
                    total_push_time += push_elapsed
                    
                    push_speed_mbps = chunk_mb / push_elapsed if push_elapsed > 0 else 0
                    self.logger.info("Chunk %d push: %.2f seconds (%.2f MB/s)", chunk_num, push_elapsed, push_speed_mbps)
                    
                    # Clean up local chunk
                    os.unlink(chunk_local)
                    chunks.append(chunk_device)
            
            self.logger.info("Total chunks: %d", len(chunks))
            self.logger.info("Total read/write time: %.2f seconds (%.2f minutes)", total_read_write_time, total_read_write_time / 60)
            self.logger.info("Total push time: %.2f seconds (%.2f minutes)", total_push_time, total_push_time / 60)
            self.logger.info("Average push speed: %.2f MB/s", (size_gb * 1024) / total_push_time if total_push_time > 0 else 0)
            
            # Time reassembly
            reassembly_start = time.time()
            self.logger.info("Reassembling %d chunks on device...", len(chunks))
            parts_pattern = f"{device_tar}.part*"
            cat_cmd = f"cd /data/local/tmp && cat {parts_pattern} > {shlex.quote(device_tar)} && rm {parts_pattern}"
            self.adb.shell_root(cat_cmd, check=True)
            reassembly_elapsed = time.time() - reassembly_start
            self.logger.info("Reassembly time: %.2f seconds (%.2f GB)", reassembly_elapsed, size_gb)
        else:
            # Time single push for smaller files
            push_start = time.time()
            self.logger.info("Pushing tar to device (%.2f GB)...", size_gb)
            self.adb.adb(["push", local_tar_path, device_tar], check=True)
            push_elapsed = time.time() - push_start
            push_speed_mbps = (size_gb * 1024) / push_elapsed if push_elapsed > 0 else 0
            self.logger.info("Push time: %.2f seconds (%.2f MB/s)", push_elapsed, push_speed_mbps)
        
        transfer_total = time.time() - overall_start
        self.logger.info("Total transfer phase: %.2f seconds (%.2f minutes)", transfer_total, transfer_total / 60)

        # ---------------- Stage 1: media/files first (fixed for empty DCIM/Pictures) ----------------
        media_bak_map = "/data/local/tmp/media_bak_map.txt"
        if plan.media_paths:
            self.logger.info("Stage 1/2: Restoring files/media FIRST... (%d paths)", len(plan.media_paths))

        for i in range(0, len(plan.media_paths), ch):
            chunk_paths = plan.media_paths[i:i + ch]
            self.logger.info("[Stage 1] Extracting chunk %d (%d paths)...", i // ch + 1, len(chunk_paths))

            prep_lines = []
            map_lines = []

            # map lines: ROOT|BAK|EXPECTFILES(0/1)
            for p in chunk_paths:
                parsed = self._parse_uid_pkg_from_path(p)
                if parsed:
                    uid2, pkg = parsed
                    prep_lines.append(f'am force-stop --user {uid2} {shlex.quote(pkg)} >/dev/null 2>&1 || true')

                root_abs = "/" + p
                bak_abs = root_abs + ".bak_restore6"

                # FIX: DCIM/Pictures may be intentionally empty in snapshot => EXPECT=0
                expect = "0" if self._is_media_root_allow_empty(p) else "1"
                map_lines.append(f"{root_abs}|{bak_abs}|{expect}")

            map_payload = "\n".join(map_lines) + "\n"
            self.adb.shell_script(
                f"su\ncat > {shlex.quote(media_bak_map)} <<'EOF'\n{map_payload}EOF\nexit\nexit\n",
                allow_fail=True,
            )

            prep_blob = "\n".join(prep_lines)
            script = f"""
su
cd /
{prep_blob}
if [ -f {shlex.quote(media_bak_map)} ]; then
  while IFS='|' read -r ROOT BAK EXPECT; do
    [ -z "$ROOT" ] && continue

    # backup existing dir
    if [ -e "$ROOT" ]; then
      rm -rf "$BAK" >/dev/null 2>&1 || true
      mv "$ROOT" "$BAK" >/dev/null 2>&1 || true
    fi

    # extract this root (may be empty, e.g. DCIM/Pictures)
    tar -xpf {device_tar} "${{ROOT#/}}" >/dev/null 2>&1 || true

    if [ "$EXPECT" = "1" ]; then
      # must have at least one file; otherwise rollback
      if find "$ROOT" -type f -maxdepth 6 2>/dev/null | head -n 1 | grep -q .; then
        rm -rf "$BAK" >/dev/null 2>&1 || true
      else
        rm -rf "$ROOT" >/dev/null 2>&1 || true
        if [ -d "$BAK" ]; then
          mv "$BAK" "$ROOT" >/dev/null 2>&1 || true
        fi
      fi
    else
      # EXPECT=0: empty is valid, but ROOT must at least exist; otherwise rollback
      if [ -e "$ROOT" ]; then
        rm -rf "$BAK" >/dev/null 2>&1 || true
      else
        if [ -d "$BAK" ]; then
          mv "$BAK" "$ROOT" >/dev/null 2>&1 || true
        fi
      fi
    fi
  done < {shlex.quote(media_bak_map)}
fi
exit
exit
"""
            self.adb.shell_script(script, allow_fail=True)

        # ---------------- Stage 2: apps after files (EXACT recovery6.py) ----------------
        if plan.app_paths:
            self.logger.info("Stage 2/2: Restoring apps AFTER files... (%d paths)", len(plan.app_paths))

        unique_pairs = []
        seen = set()
        for p in plan.app_paths:
            parsed = self._parse_uid_pkg_from_path(p)
            if not parsed:
                continue
            if parsed not in seen:
                seen.add(parsed)
                unique_pairs.append(parsed)

        if unique_pairs:
            lines = ["su"]
            for uid2, pkg in unique_pairs:
                lines.append(f'am force-stop --user {uid2} {shlex.quote(pkg)} >/dev/null 2>&1 || true')
            lines += ["exit", "exit"]
            self.adb.shell_script("\n".join(lines) + "\n", allow_fail=True)

        self.logger.info("Stopping framework (best-effort) ...")
        self.adb.shell_script(
            'su\n'
            'if command -v stop >/dev/null 2>&1; then stop; '
            'elif command -v setprop >/dev/null 2>&1; then setprop ctl.stop zygote 2>/dev/null || true; setprop ctl.stop zygote_secondary 2>/dev/null || true; fi\n'
            'exit\nexit\n',
            allow_fail=True,
        )

        for i in range(0, len(plan.app_paths), ch):
            chunk_paths = plan.app_paths[i:i + ch]
            self.logger.info("[Stage 2] Extracting chunk %d (%d paths)...", i // ch + 1, len(chunk_paths))

            prep_lines = []
            for p in chunk_paths:
                parsed = self._parse_uid_pkg_from_path(p)
                if not parsed:
                    continue
                uid2, pkg = parsed
                if p.startswith("data/user/") or p.startswith("data/user_de/"):
                    prep_lines.append(f'rm -rf /data/user/{uid2}/{shlex.quote(pkg)} >/dev/null 2>&1 || true')
                    prep_lines.append(f'rm -rf /data/user_de/{uid2}/{shlex.quote(pkg)} >/dev/null 2>&1 || true')

            joined = " ".join(shlex.quote(p) for p in chunk_paths)
            script = f"""
su
cd /
{chr(10).join(prep_lines)}
tar -xpf {device_tar} {joined} || true
exit
exit
"""
            self.adb.shell_script(script, allow_fail=True)

        self.logger.info("Post-stage: restorecon + start framework (best-effort) ...")
        self.adb.shell_script(
            "su\nsync\n"
            "if command -v restorecon >/dev/null 2>&1; then restorecon -RF /data/user /data/user_de >/dev/null 2>&1 || true; fi\n"
            "exit\nexit\n",
            allow_fail=True,
        )

        self.adb.shell_script(
            'su\n'
            'if command -v start >/dev/null 2>&1; then start; '
            'elif command -v setprop >/dev/null 2>&1; then setprop ctl.start zygote 2>/dev/null || true; setprop ctl.start zygote_secondary 2>/dev/null 2>&1 || true; fi\n'
            'sleep 2\n'
            'exit\nexit\n',
            allow_fail=True,
        )

        # Fixups (kept)
        fix_lines = ["su", "cd /"]
        for uid in plan.user_ids:
            fix_lines.append(f'chown -R media_rw:media_rw /data/media/{uid} >/dev/null 2>&1 || true')
            fix_lines.append('if command -v restorecon >/dev/null 2>&1; then '
                             f'restorecon -RF /data/media/{uid} >/dev/null 2>&1 || true; '
                             'fi')
            fix_lines.append(f'am force-stop --user {uid} com.android.providers.media >/dev/null 2>&1 || true')
            fix_lines.append(f'am force-stop --user {uid} com.google.android.apps.photos >/dev/null 2>&1 || true')
        fix_lines += ["exit", "exit"]
        self.adb.shell_script("\n".join(fix_lines) + "\n", allow_fail=True)

        # Photos permissions (kept)
        perm = ["su", "set -e", ""]
        for uid in plan.user_ids:
            perm += [
                f'pm grant --user {uid} {plan.photos_pkg} android.permission.READ_MEDIA_IMAGES >/dev/null 2>&1 || true',
                f'pm grant --user {uid} {plan.photos_pkg} android.permission.READ_MEDIA_VIDEO >/dev/null 2>&1 || true',
                f'pm grant --user {uid} {plan.photos_pkg} android.permission.READ_EXTERNAL_STORAGE >/dev/null 2>&1 || true',
            ]
        perm += [f'monkey -p {plan.photos_pkg} 1 >/dev/null 2>&1 || true', "exit", "exit"]
        self.adb.shell_script("\n".join(perm) + "\n", allow_fail=True)

        # Ensure SystemUI not stopped (kept)
        ui = ["su"]
        for uid in plan.user_ids:
            ui += [
                f'pm enable --user {uid} {plan.systemui_pkg} >/dev/null 2>&1 || true',
                f'cmd package set-stopped-state --user {uid} {plan.systemui_pkg} false >/dev/null 2>&1 || true',
                f'am set-stopped-state --user {uid} {plan.systemui_pkg} false >/dev/null 2>&1 || true',
            ]
        ui += ["exit", "exit"]
        self.adb.shell_script("\n".join(ui) + "\n", allow_fail=True)

        # Safe refresh (kept)
        self._safe_media_refresh(plan)

        # Cleanup device tar
        self.adb.shell_root(f"rm -f {shlex.quote(device_tar)}", check=False)

    def exec_restore_full(self, device_tar: str) -> None:
        script = f"""
su
cd /
tar -xpf {device_tar} data || true
exit
exit
"""
        self.adb.shell_script(script, allow_fail=True)
