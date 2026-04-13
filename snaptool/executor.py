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
    runtime_state_batch_size_path: int = 24
    runtime_state_batch_size_app: int = 0
    sdk_version: int | None = None


class RestoreExecutor:
    def __init__(self, adb: "AdbClient", logger: logging.Logger, exec_cfg: ExecConfig):
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
        return bool(__import__("re").match(r"^data/media/\d+/(DCIM|Pictures)$", p))

    @staticmethod
    def _build_pkg_roots(uid: int, pkg: str) -> list[str]:
        return [
            f"data/user/{uid}/{pkg}",
            f"data/user_de/{uid}/{pkg}",
            f"data/media/{uid}/Android/data/{pkg}",
            f"data/media/{uid}/Android/media/{pkg}",
            f"data/media/{uid}/Android/obb/{pkg}",
        ]

    def _safe_media_refresh(self, plan: "RestorePlan") -> None:
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
            "exit",
            "exit",
            "",
        ]
        self.adb.shell_script("\n".join(lines), allow_fail=True)

    def _stop_framework_best_effort(self) -> None:
        self.logger.info("Stopping framework (best-effort) ...")
        self.adb.shell_script(
            "su\n"
            "if command -v stop >/dev/null 2>&1; then stop; "
            "elif command -v setprop >/dev/null 2>&1; then "
            "setprop ctl.stop zygote 2>/dev/null || true; "
            "setprop ctl.stop zygote_secondary 2>/dev/null || true; "
            "fi\n"
            "exit\nexit\n",
            allow_fail=True,
        )

    def _start_framework_best_effort(self) -> None:
        self.logger.info("Starting framework (best-effort) ...")
        self.adb.shell_script(
            "su\n"
            "if command -v start >/dev/null 2>&1; then start; "
            "elif command -v setprop >/dev/null 2>&1; then "
            "setprop ctl.start zygote 2>/dev/null || true; "
            "setprop ctl.start zygote_secondary 2>/dev/null || true; "
            "fi\n"
            "sleep 2\n"
            "exit\nexit\n",
            allow_fail=True,
        )

    def _apply_runtime_state(
        self,
        runtime_state: dict,
        package_batch_size: int = 1,
        apply_revokes: bool = False,
    ) -> None:
        if not runtime_state:
            return
        packages = [(pkg, user_map) for pkg, user_map in runtime_state.items() if isinstance(pkg, str) and isinstance(user_map, dict)]
        if not packages:
            return

        total = len(packages)
        if package_batch_size <= 0 or package_batch_size > total:
            package_batch_size = total
        total_batches = (total + package_batch_size - 1) // package_batch_size
        self.logger.info(
            "Applying package runtime state (permissions/appops): %d packages in %d batch(es) ...",
            total,
            total_batches,
        )

        for batch_i, start in enumerate(range(0, total, package_batch_size), start=1):
            chunk = packages[start:start + package_batch_size]
            self.logger.info(
                "Applying runtime state batch %d/%d (%d packages) ...",
                batch_i,
                total_batches,
                len(chunk),
            )
            for idx, (pkg, user_map) in enumerate(chunk, start=start + 1):
                self.logger.info("Applying runtime state for package %d/%d: %s", idx, total, pkg)
                for uid_s, state in user_map.items():
                    try:
                        uid = int(uid_s)
                    except Exception:
                        continue
                    if not isinstance(state, dict):
                        continue

                    uid_s_arg = str(uid)
                    self.adb.adb(["shell", "am", "force-stop", "--user", uid_s_arg, pkg], check=False)
                    self.adb.adb(["shell", "cmd", "appops", "reset", "--user", uid_s_arg, pkg], check=False)

                    perms = state.get("runtime_permissions")
                    if isinstance(perms, dict):
                        for perm, granted in perms.items():
                            if not isinstance(perm, str):
                                continue
                            if isinstance(granted, bool):
                                if granted:
                                    self.adb.adb(["shell", "pm", "grant", "--user", uid_s_arg, pkg, perm], check=False)
                                elif apply_revokes:
                                    self.adb.adb(["shell", "pm", "revoke", "--user", uid_s_arg, pkg, perm], check=False)
                            elif granted:
                                self.adb.adb(["shell", "pm", "grant", "--user", uid_s_arg, pkg, perm], check=False)
                    elif isinstance(perms, list):
                        for perm in perms:
                            if not isinstance(perm, str):
                                continue
                            self.adb.adb(["shell", "pm", "grant", "--user", uid_s_arg, pkg, perm], check=False)

                    appops = state.get("appops")
                    if isinstance(appops, dict):
                        for op, mode in appops.items():
                            if not isinstance(op, str) or not isinstance(mode, str):
                                continue
                            self.adb.adb(
                                ["shell", "cmd", "appops", "set", "--user", uid_s_arg, pkg, op, mode],
                                check=False,
                            )

    def _permission_state_file_fixups(self, user_ids: list[int]) -> None:
        self.logger.info("Post-restore: permission state file fixups ...")
        lines = ["su", "cd /", ""]

        lines += [
            "if [ -f /data/system/appops.xml ]; then",
            "  chown system:system /data/system/appops.xml >/dev/null 2>&1 || true",
            "  chmod 600 /data/system/appops.xml >/dev/null 2>&1 || true",
            "  if command -v restorecon >/dev/null 2>&1; then restorecon -v /data/system/appops.xml >/dev/null 2>&1 || true; fi",
            "fi",
            "",
        ]

        for uid in user_ids:
            lines += [
                f"if [ -f /data/system/users/{uid}/runtime-permissions.xml ]; then",
                f"  chown system:system /data/system/users/{uid}/runtime-permissions.xml >/dev/null 2>&1 || true",
                f"  chmod 600 /data/system/users/{uid}/runtime-permissions.xml >/dev/null 2>&1 || true",
                f"  if command -v restorecon >/dev/null 2>&1; then restorecon -v /data/system/users/{uid}/runtime-permissions.xml >/dev/null 2>&1 || true; fi",
                "fi",
                "",
                f"if [ -f /data/system/users/{uid}/package-restrictions.xml ]; then",
                f"  chown system:system /data/system/users/{uid}/package-restrictions.xml >/dev/null 2>&1 || true",
                f"  chmod 660 /data/system/users/{uid}/package-restrictions.xml >/dev/null 2>&1 || true",
                f"  if command -v restorecon >/dev/null 2>&1; then restorecon -v /data/system/users/{uid}/package-restrictions.xml >/dev/null 2>&1 || true; fi",
                "fi",
                "",
                # Android 13+ permission module storage.
                f"if [ -f /data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml ]; then",
                f"  chown system:system /data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml >/dev/null 2>&1 || true",
                f"  chmod 600 /data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml >/dev/null 2>&1 || true",
                f"  if command -v restorecon >/dev/null 2>&1; then restorecon -v /data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml >/dev/null 2>&1 || true; fi",
                "fi",
                "",
                f"if [ -f /data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml.reservecopy ]; then",
                f"  chown system:system /data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml.reservecopy >/dev/null 2>&1 || true",
                f"  chmod 600 /data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml.reservecopy >/dev/null 2>&1 || true",
                f"  if command -v restorecon >/dev/null 2>&1; then restorecon -v /data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml.reservecopy >/dev/null 2>&1 || true; fi",
                "fi",
                "",
                f"if [ -f /data/misc_de/{uid}/apexdata/com.android.permission/roles.xml ]; then",
                f"  chown system:system /data/misc_de/{uid}/apexdata/com.android.permission/roles.xml >/dev/null 2>&1 || true",
                f"  chmod 600 /data/misc_de/{uid}/apexdata/com.android.permission/roles.xml >/dev/null 2>&1 || true",
                f"  if command -v restorecon >/dev/null 2>&1; then restorecon -v /data/misc_de/{uid}/apexdata/com.android.permission/roles.xml >/dev/null 2>&1 || true; fi",
                "fi",
                "",
                f"if [ -f /data/misc_de/{uid}/apexdata/com.android.permission/roles.xml.reservecopy ]; then",
                f"  chown system:system /data/misc_de/{uid}/apexdata/com.android.permission/roles.xml.reservecopy >/dev/null 2>&1 || true",
                f"  chmod 600 /data/misc_de/{uid}/apexdata/com.android.permission/roles.xml.reservecopy >/dev/null 2>&1 || true",
                f"  if command -v restorecon >/dev/null 2>&1; then restorecon -v /data/misc_de/{uid}/apexdata/com.android.permission/roles.xml.reservecopy >/dev/null 2>&1 || true; fi",
                "fi",
                "",
            ]

        lines += [
            "if command -v restorecon >/dev/null 2>&1; then restorecon -RF /data/system/users >/dev/null 2>&1 || true; fi",
            "if command -v restorecon >/dev/null 2>&1; then restorecon -RF /data/system/appops >/dev/null 2>&1 || true; fi",
            "if command -v restorecon >/dev/null 2>&1; then restorecon -RF /data/misc_de >/dev/null 2>&1 || true; fi",
            "sync",
            "exit",
            "exit",
            "",
        ]
        self.adb.shell_script("\n".join(lines), allow_fail=True)

    # ---------------- NEW: keystore2 + locksettings fixups ----------------
    def _keystore_locksettings_fixups(self, device_tar: str) -> None:
        """
        If snapshot contains keystore2 + locksettings, restore them in a safe order:
        - stop keystore2 (best-effort)
        - replace /data/misc/keystore from tar (dir)
        - replace /data/system/locksettings.db* from tar
        - restorecon + perms/owners
        - start keystore2 (best-effort)

        This helps when accounts/app tokens are encrypted with keystore-backed material.
        """
        if self.cfg.sdk_version is None:
            self.logger.warning(
                "SDK version could not be determined; skipping keystore/locksettings "
                "restore for safety (fail closed)."
            )
            return
        if self.cfg.sdk_version >= 34:
            self.logger.info(
                "Skipping keystore/locksettings restore: Android 14+ (SDK %d) "
                "hardware-bound keys are not portable across device/boot sessions.",
                self.cfg.sdk_version,
            )
            return

        self.logger.info("Post-restore: Keystore2 + locksettings replace + fixups ...")

        dt = shlex.quote(device_tar)

        lines = ["su", "cd /", ""]

        # Helper: stop/start keystore2 best-effort.
        lines += [
            "# Stop keystore2 so we can safely replace its DB (best-effort)",
            "if command -v stop >/dev/null 2>&1; then",
            "  stop keystore2 >/dev/null 2>&1 || true",
            "  stop credstore >/dev/null 2>&1 || true",
            "fi",
            "if command -v setprop >/dev/null 2>&1; then",
            "  setprop ctl.stop keystore2 >/dev/null 2>&1 || true",
            "  setprop ctl.stop credstore >/dev/null 2>&1 || true",
            "fi",
            "sleep 0.5",
            "",
        ]

        # Restore /data/misc/keystore (directory) if present in tar.
        # We anchor on persistent.sqlite, but extract the whole directory for completeness.
        lines += [
            f"if tar -tf {dt} data/misc/keystore/persistent.sqlite >/dev/null 2>&1; then",
            "  # Clear existing dir contents then extract snapshot version",
            "  mkdir -p /data/misc/keystore >/dev/null 2>&1 || true",
            "  rm -f /data/misc/keystore/* >/dev/null 2>&1 || true",
            f"  tar -xpf {dt} data/misc/keystore >/dev/null 2>&1 || true",
            "  chown -R keystore:keystore /data/misc/keystore >/dev/null 2>&1 || true",
            "  chmod 700 /data/misc/keystore >/dev/null 2>&1 || true",
            "  chmod 600 /data/misc/keystore/* >/dev/null 2>&1 || true",
            "  if command -v restorecon >/dev/null 2>&1; then restorecon -RF /data/misc/keystore >/dev/null 2>&1 || true; fi",
            "fi",
            "",
        ]

        # Restore /data/system/locksettings.db* if present in tar
        # (we probe each file before extracting to avoid tar errors for missing members)
        lines += [
            "LS_LIST=''",
            f"for f in data/system/locksettings.db data/system/locksettings.db-wal data/system/locksettings.db-shm data/system/locksettings.db-journal; do",
            f"  if tar -tf {dt} \"$f\" >/dev/null 2>&1; then LS_LIST=\"$LS_LIST $f\"; fi",
            "done",
            "if [ -n \"$LS_LIST\" ]; then",
            "  rm -f /data/system/locksettings.db* >/dev/null 2>&1 || true",
            f"  tar -xpf {dt} $LS_LIST >/dev/null 2>&1 || true",
            "  chown system:system /data/system/locksettings.db* >/dev/null 2>&1 || true",
            "  chmod 600 /data/system/locksettings.db* >/dev/null 2>&1 || true",
            "  if command -v restorecon >/dev/null 2>&1; then restorecon -v /data/system/locksettings.db* >/dev/null 2>&1 || true; fi",
            "fi",
            "",
        ]

        # Start keystore2 back (best-effort)
        lines += [
            "sync",
            "# Start keystore2 back (best-effort)",
            "if command -v start >/dev/null 2>&1; then",
            "  start keystore2 >/dev/null 2>&1 || true",
            "  start credstore >/dev/null 2>&1 || true",
            "fi",
            "if command -v setprop >/dev/null 2>&1; then",
            "  setprop ctl.start keystore2 >/dev/null 2>&1 || true",
            "  setprop ctl.start credstore >/dev/null 2>&1 || true",
            "fi",
            "sleep 0.5",
            "",
            "exit",
            "exit",
            "",
        ]

        self.adb.shell_script("\n".join(lines), allow_fail=True)

    def exec_restore_path(
        self,
        plan: "RestorePlan",
        tar_index: "TarIndex",
        local_tar,
        runtime_state: dict | None = None,
        runtime_apply_revokes: bool = False,
        device_tar: str = "/data/local/tmp/restore.tar",
    ) -> None:
        ch = self.cfg.chunk_size

        self.logger.info("Pushing temp tar to device...")
        self.adb.adb(["push", str(local_tar), device_tar], check=True)

        # ---------------- Stage 1: media/files first ----------------
        media_bak_map = "/data/local/tmp/media_bak_map.txt"
        if plan.media_paths:
            self.logger.info("Stage 1/2: Restoring files/media FIRST... (%d paths)", len(plan.media_paths))

        for i in range(0, len(plan.media_paths), ch):
            chunk_paths = plan.media_paths[i:i + ch]
            self.logger.info("[Stage 1] Extracting chunk %d (%d paths)...", i // ch + 1, len(chunk_paths))

            prep_lines = []
            map_lines = []

            for p in chunk_paths:
                parsed = self._parse_uid_pkg_from_path(p)
                if parsed:
                    uid2, pkg = parsed
                    prep_lines.append(f'am force-stop --user {uid2} {shlex.quote(pkg)} >/dev/null 2>&1 || true')

                root_abs = "/" + p
                bak_abs = root_abs + ".bak_restore6"
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

    if [ -e "$ROOT" ]; then
      rm -rf "$BAK" >/dev/null 2>&1 || true
      mv "$ROOT" "$BAK" >/dev/null 2>&1 || true
    fi

    tar -xpf {device_tar} "${{ROOT#/}}" >/dev/null 2>&1 || true

    if [ "$EXPECT" = "1" ]; then
      if find "$ROOT" -type f -maxdepth 6 2>/dev/null | head -n 1 | grep -q .; then
        rm -rf "$BAK" >/dev/null 2>&1 || true
      else
        rm -rf "$ROOT" >/dev/null 2>&1 || true
        if [ -d "$BAK" ]; then
          mv "$BAK" "$ROOT" >/dev/null 2>&1 || true
        fi
      fi
    else
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

        # ---------------- Stage 2: apps after files ----------------
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

        # Replay runtime permissions/appops before framework stop/start so restore
        # is not blocked by lock-screen state after restart.
        if runtime_state:
            self.logger.info("Pre-stage: applying runtime state before framework restart ...")
            self._apply_runtime_state(
                runtime_state,
                package_batch_size=self.cfg.runtime_state_batch_size_path,
                apply_revokes=runtime_apply_revokes,
            )

        self._stop_framework_best_effort()

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

        # NEW: restore keystore2 + locksettings (if present) BEFORE accounts DB replace
        self._keystore_locksettings_fixups(device_tar)

        # AccountManager DB replace + perms/contexts
        self._accountmanager_fixups(plan.user_ids, device_tar)
        self._permission_state_file_fixups(plan.user_ids)

        self.logger.info("Post-stage: restorecon + start framework (best-effort) ...")
        self.adb.shell_script(
            "su\nsync\n"
            "if command -v restorecon >/dev/null 2>&1; then restorecon -RF /data/user /data/user_de >/dev/null 2>&1 || true; fi\n"
            "exit\nexit\n",
            allow_fail=True,
        )

        self._start_framework_best_effort()

        if runtime_state:
            self.logger.info("Post-stage: re-applying runtime state after framework restart ...")
            self._apply_runtime_state(
                runtime_state,
                package_batch_size=self.cfg.runtime_state_batch_size_path,
                apply_revokes=runtime_apply_revokes,
            )

        # Fixups (kept)
        fix_lines = ["su", "cd /"]
        for uid in plan.user_ids:
            fix_lines.append(f'chown -R media_rw:media_rw /data/media/{uid} >/dev/null 2>&1 || true')
            fix_lines.append(
                'if command -v restorecon >/dev/null 2>&1; then '
                f'restorecon -RF /data/media/{uid} >/dev/null 2>&1 || true; '
                'fi'
            )
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
        perm += ["exit", "exit"]
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

    def exec_restore_app(
        self,
        package: str,
        user_ids: list[int],
        local_tar,
        auth_pkgs: list[str] | None = None,
        include_account_db: bool = True,
        present_roots: set[str] | None = None,
        runtime_state: dict | None = None,
        device_tar: str = "/data/local/tmp/restore-app.tar",
    ) -> None:
        if not user_ids:
            self.logger.warning("No users supplied for app restore; nothing to do.")
            return

        pkgs: list[str] = []
        seen = set()
        for p in [package] + list(auth_pkgs or []):
            if p and p not in seen:
                seen.add(p)
                pkgs.append(p)

        app_paths: list[str] = []
        for uid in user_ids:
            for pkg in pkgs:
                app_paths.extend(self._build_pkg_roots(uid, pkg))

        if present_roots is not None:
            app_paths = [p for p in app_paths if p in present_roots]
        app_paths = list(dict.fromkeys(app_paths))

        ch = self.cfg.chunk_size
        self.logger.info(
            "Restoring app snapshot: package=%s users=%s packages=%d roots=%d account_db=%s",
            package,
            ",".join(str(u) for u in user_ids),
            len(pkgs),
            len(app_paths),
            include_account_db,
        )
        self.logger.info("Pushing temp tar to device...")
        self.adb.adb(["push", str(local_tar), device_tar], check=True)

        all_pairs = []
        all_seen = set()
        for p in app_paths:
            parsed = self._parse_uid_pkg_from_path(p)
            if parsed and parsed not in all_seen:
                all_seen.add(parsed)
                all_pairs.append(parsed)

        if all_pairs:
            force_lines = ["su"]
            for uid, pkg in all_pairs:
                force_lines.append(f'am force-stop --user {uid} {shlex.quote(pkg)} >/dev/null 2>&1 || true')
            force_lines += ["exit", "exit"]
            self.adb.shell_script("\n".join(force_lines) + "\n", allow_fail=True)
        else:
            self.logger.warning("No app data roots from snapshot match requested package/auth packages.")

        if runtime_state:
            self.logger.info("Pre-stage: applying runtime state before framework restart ...")
            self._apply_runtime_state(
                runtime_state,
                package_batch_size=self.cfg.runtime_state_batch_size_app,
                apply_revokes=True,
            )

        if include_account_db:
            self._stop_framework_best_effort()

        for i in range(0, len(app_paths), ch):
            chunk_paths = app_paths[i:i + ch]
            self.logger.info("App restore chunk %d (%d paths)...", i // ch + 1, len(chunk_paths))

            unique_pairs = []
            seen_pairs = set()
            for p in chunk_paths:
                parsed = self._parse_uid_pkg_from_path(p)
                if parsed and parsed not in seen_pairs:
                    seen_pairs.add(parsed)
                    unique_pairs.append(parsed)

            prep_lines = []
            fix_owner_lines = []
            fix_ctx_lines = []
            owner_idx = 0
            ctx_idx = 0
            for uid2, pkg in unique_pairs:
                owner_var = f"O{owner_idx}"
                owner_idx += 1
                ctx_var = f"C{ctx_idx}"
                ctx_idx += 1
                quoted_pkg = shlex.quote(pkg)
                prep_lines += [
                    # Capture current app UID:GID before deletion so restored files keep app ownership.
                    f'{owner_var}="$(stat -c \'%u:%g\' /data/user/{uid2}/{quoted_pkg} 2>/dev/null || stat -c \'%u:%g\' /data/user_de/{uid2}/{quoted_pkg} 2>/dev/null || true)"',
                    f'if [ -z "${owner_var}" ] && [ -f /data/system/packages.list ]; then '
                    f'{owner_var}="$(awk \'$1==\\"{pkg}\\" {{print $2 ":" $2; exit}}\' /data/system/packages.list 2>/dev/null || true)"; fi',
                    # Capture current SELinux app-data label (includes category set on most builds).
                    f'{ctx_var}="$(ls -Zd /data/user/{uid2}/{quoted_pkg} 2>/dev/null | awk \'{{print $1}}\' || ls -Zd /data/user_de/{uid2}/{quoted_pkg} 2>/dev/null | awk \'{{print $1}}\' || true)"',
                    f'rm -rf /data/user/{uid2}/{quoted_pkg} >/dev/null 2>&1 || true',
                    f'rm -rf /data/user_de/{uid2}/{quoted_pkg} >/dev/null 2>&1 || true',
                    f'rm -rf /data/media/{uid2}/Android/data/{quoted_pkg} >/dev/null 2>&1 || true',
                    f'rm -rf /data/media/{uid2}/Android/media/{quoted_pkg} >/dev/null 2>&1 || true',
                    f'rm -rf /data/media/{uid2}/Android/obb/{quoted_pkg} >/dev/null 2>&1 || true',
                ]
                fix_owner_lines += [
                    f'if [ -n "${owner_var}" ]; then',
                    f'  chown -R "${owner_var}" /data/user/{uid2}/{quoted_pkg} >/dev/null 2>&1 || true',
                    f'  chown -R "${owner_var}" /data/user_de/{uid2}/{quoted_pkg} >/dev/null 2>&1 || true',
                    f'fi',
                ]
                fix_ctx_lines += [
                    f'if [ -n "${ctx_var}" ] && command -v chcon >/dev/null 2>&1; then',
                    f'  chcon -R "${ctx_var}" /data/user/{uid2}/{quoted_pkg} >/dev/null 2>&1 || true',
                    f'  chcon -R "${ctx_var}" /data/user_de/{uid2}/{quoted_pkg} >/dev/null 2>&1 || true',
                    f'fi',
                ]

            joined = " ".join(shlex.quote(p) for p in chunk_paths)
            script = f"""
su
cd /
{chr(10).join(prep_lines)}
tar -xpf {device_tar} {joined} >/dev/null 2>&1 || true
{chr(10).join(fix_owner_lines)}
{chr(10).join(fix_ctx_lines)}
exit
exit
"""
            self.adb.shell_script(script, allow_fail=True)

        if include_account_db:
            self._keystore_locksettings_fixups(device_tar)
            self._accountmanager_fixups(user_ids, device_tar)

        restorecon_lines = ["su", "sync"]
        restorecon_lines.append("if command -v restorecon >/dev/null 2>&1; then")
        for uid in user_ids:
            for pkg in pkgs:
                restorecon_lines += [
                    f'  restorecon -RF /data/user/{uid}/{shlex.quote(pkg)} >/dev/null 2>&1 || true',
                    f'  restorecon -RF /data/user_de/{uid}/{shlex.quote(pkg)} >/dev/null 2>&1 || true',
                    f'  restorecon -RF /data/media/{uid}/Android/data/{shlex.quote(pkg)} >/dev/null 2>&1 || true',
                    f'  restorecon -RF /data/media/{uid}/Android/media/{shlex.quote(pkg)} >/dev/null 2>&1 || true',
                    f'  restorecon -RF /data/media/{uid}/Android/obb/{shlex.quote(pkg)} >/dev/null 2>&1 || true',
                ]
        restorecon_lines += ["fi", "exit", "exit", ""]
        self.adb.shell_script("\n".join(restorecon_lines), allow_fail=True)

        if include_account_db:
            self._start_framework_best_effort()

        if runtime_state:
            self.logger.info("Post-stage: re-applying runtime state after framework restart ...")
            self._apply_runtime_state(
                runtime_state,
                package_batch_size=self.cfg.runtime_state_batch_size_app,
                apply_revokes=True,
            )

        self.adb.shell_root(f"rm -f {shlex.quote(device_tar)}", check=False)

    def exec_restore_full(self, device_tar: str) -> None:
        # WARNING: This method bypasses _keystore_locksettings_fixups() entirely —
        # it untars /data directly. Do NOT re-enable without adding the SDK >= 34
        # guard to exclude keystore/locksettings from extraction.
        script = f"""
su
cd /
tar -xpf {device_tar} data || true
exit
exit
"""
        self.adb.shell_script(script, allow_fail=True)

    def _accountmanager_fixups(self, user_ids: list[int], device_tar: str) -> None:
        """
        AccountManager support:
        Replace AccountManager DBs from backup tar and ensure correct owner/perms/SELinux context.

        Required files (per user in tar):
        data/system_ce/<uid>/accounts_ce.db*
        data/system_de/<uid>/accounts_de.db*
        """
        self.logger.info("Post-restore: AccountManager DB replace + fixups ...")
        dt = shlex.quote(device_tar)

        lines = ["su", "cd /", ""]

        for uid in user_ids:
            lines += [
                f"if tar -tf {dt} data/system_ce/{uid}/accounts_ce.db >/dev/null 2>&1; then",
                f"  rm -f /data/system_ce/{uid}/accounts_ce.db* >/dev/null 2>&1 || true",
                f"  tar -xpf {dt} "
                f"data/system_ce/{uid}/accounts_ce.db "
                f"data/system_ce/{uid}/accounts_ce.db-wal "
                f"data/system_ce/{uid}/accounts_ce.db-shm "
                f">/dev/null 2>&1 || true",
                f"  chown system:system /data/system_ce/{uid}/accounts_ce.db* >/dev/null 2>&1 || true",
                f"  chmod 600 /data/system_ce/{uid}/accounts_ce.db* >/dev/null 2>&1 || true",
                f"  if command -v restorecon >/dev/null 2>&1; then restorecon -v /data/system_ce/{uid}/accounts_ce.db* >/dev/null 2>&1 || true; fi",
                f"fi",
                "",
            ]

            lines += [
                f"if tar -tf {dt} data/system_de/{uid}/accounts_de.db >/dev/null 2>&1; then",
                f"  rm -f /data/system_de/{uid}/accounts_de.db* >/dev/null 2>&1 || true",
                f"  tar -xpf {dt} "
                f"data/system_de/{uid}/accounts_de.db "
                f"data/system_de/{uid}/accounts_de.db-wal "
                f"data/system_de/{uid}/accounts_de.db-shm "
                f">/dev/null 2>&1 || true",
                f"  chown system:system /data/system_de/{uid}/accounts_de.db* >/dev/null 2>&1 || true",
                f"  chmod 600 /data/system_de/{uid}/accounts_de.db* >/dev/null 2>&1 || true",
                f"  if command -v restorecon >/dev/null 2>&1; then restorecon -v /data/system_de/{uid}/accounts_de.db* >/dev/null 2>&1 || true; fi",
                f"fi",
                "",
            ]

        lines += ["sync", "exit", "exit", ""]
        self.adb.shell_script("\n".join(lines), allow_fail=True)
