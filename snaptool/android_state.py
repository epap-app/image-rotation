from __future__ import annotations

import logging
import re

from .adb import AdbClient


class AndroidStateReader:
    def __init__(self, adb: AdbClient, logger: logging.Logger):
        self.adb = adb
        self.logger = logger
        self._system_cache: set[str] | None = None
        self._thirdparty_cache: set[str] | None = None

    def get_all_user_ids(self) -> list[int]:
        candidates: list[int] = []
        for cmdline in [
            "cmd user list 2>/dev/null || true",
            "pm list users 2>/dev/null || true",
        ]:
            res = self.adb.shell_root(cmdline, check=False)
            out = res.stdout or ""
            for m in re.finditer(r"UserInfo\{(\d+):", out):
                candidates.append(int(m.group(1)))

        seen = set()
        user_ids = []
        for u in candidates:
            if u not in seen:
                seen.add(u)
                user_ids.append(u)
        return user_ids or [0]

    def list_installed_pkgs_for_user(self, uid: int) -> list[str]:
        res = self.adb.shell_root(f"pm list packages --user {uid} 2>/dev/null || true", check=False)
        out = res.stdout or ""
        pkgs = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                pkgs.append(line.split("package:", 1)[1])
        return pkgs

    def list_overlay_pkgs_for_user(self, uid: int) -> list[str]:
        res = self.adb.shell_root(f"cmd overlay list --user {uid} 2>/dev/null || true", check=False)
        out = res.stdout or ""
        pkgs = set()
        for line in out.splitlines():
            line = line.strip()
            m = re.search(r"([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)+)", line)
            if m:
                pkgs.add(m.group(1))
        return sorted(pkgs)

    def list_system_pkgs(self) -> set[str]:
        """
        Global system packages set (pm list packages -s).
        Cached to avoid re-running for each user.
        """
        if self._system_cache is not None:
            return self._system_cache
        res = self.adb.shell_root("pm list packages -s 2>/dev/null || true", check=False)
        out = res.stdout or ""
        pkgs: set[str] = set()
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                pkgs.add(line.split("package:", 1)[1])
        self._system_cache = pkgs
        return pkgs

    def list_thirdparty_pkgs(self) -> set[str]:
        """
        Global third-party packages set (pm list packages -3).
        Cached to avoid re-running for each user.
        """
        if self._thirdparty_cache is not None:
            return self._thirdparty_cache
        res = self.adb.shell_root("pm list packages -3 2>/dev/null || true", check=False)
        out = res.stdout or ""
        pkgs: set[str] = set()
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                pkgs.add(line.split("package:", 1)[1])
        self._thirdparty_cache = pkgs
        return pkgs

    def read_device_state(self) -> dict:
        return {"user_ids": self.get_all_user_ids()}
