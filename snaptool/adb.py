from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Sequence

from .runner import CmdResult, run_best_effort, run_checked


@dataclass(frozen=True)
class AdbClient:
    logger: logging.Logger
    serial: str | None = None

    def _base(self) -> list[str]:
        cmd = ["adb"]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def adb(self, args: Sequence[str], check: bool = True, **kwargs) -> CmdResult:
        cmd = self._base() + list(args)
        return run_checked(cmd, self.logger, **kwargs) if check else run_best_effort(cmd, self.logger, **kwargs)

    def shell_root(self, cmdline: str, check: bool = True, **kwargs) -> CmdResult:
        # one-liners: adb shell su -c "<cmdline>"
        return self.adb(["shell", "su", "-c", cmdline], check=check, **kwargs)

    def shell_script(self, script: str, allow_fail: bool = False) -> CmdResult:
       
        cmd = self._base() + ["shell"]
        self.logger.info("[adb-shell-script] (%d bytes)", len(script.encode("utf-8", errors="ignore")))

        proc = subprocess.run(
            cmd,
            input=script.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        out = (proc.stdout or b"").decode("utf-8", errors="ignore")
        err = (proc.stderr or b"").decode("utf-8", errors="ignore")

        if proc.returncode != 0 and not allow_fail:
            self.logger.error("adb shell script failed rc=%s", proc.returncode)
            if out.strip():
                self.logger.error("stdout:\n%s", out.strip())
            if err.strip():
                self.logger.error("stderr:\n%s", err.strip())
            raise subprocess.CalledProcessError(proc.returncode, cmd, output=out, stderr=err)

        if proc.returncode != 0 and allow_fail:
            self.logger.warning("adb shell script non-zero (continuing) rc=%s", proc.returncode)
            if err.strip():
                self.logger.warning("stderr:\n%s", err.strip())

        return CmdResult(cmd=list(cmd), rc=proc.returncode, stdout=out, stderr=err)
