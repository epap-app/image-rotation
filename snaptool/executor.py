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
        ch = self.cfg.chunk_size

        self.logger.info("Pushing temp tar to device...")
        self.adb.adb(["push", str(local_tar), device_tar], check=True)

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
