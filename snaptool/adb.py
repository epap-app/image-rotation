from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Sequence

from .runner import (
    CmdResult,
    SnaptoolAbortedError,
    is_transport_failure,
    run_best_effort,
    run_checked,
)


@dataclass(frozen=True)
class AdbClient:
    logger: logging.Logger
    serial: str | None = None

    def _base(self) -> list[str]:
        cmd = ["adb"]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def _check_transport(self, res: CmdResult, phase: str, reason: str) -> None:
        """If the adb invocation produced a transport-level failure (device
        offline, no devices, closed pipe, etc.), raise SnaptoolAbortedError so
        the operation aborts immediately. Continuing past a transport failure
        is what causes half-finished restores."""
        if res.rc != 0 and is_transport_failure(res.stderr, res.stdout):
            raise SnaptoolAbortedError(
                phase=phase,
                reason=reason,
                cmd=res.cmd,
                rc=res.rc,
                stderr=res.stderr,
                transport=True,
            )

    def adb(
        self,
        args: Sequence[str],
        check: bool = True,
        *,
        critical: bool | None = None,
        phase: str = "adb",
        reason: str | None = None,
        **kwargs,
    ) -> CmdResult:
        """Run an adb command.

        critical=True   → raise SnaptoolAbortedError on any nonzero rc.
        critical=False  → warn-only, but still raise on adb-transport failure.
        critical=None   → fall back to legacy `check`: True == critical, False == best-effort.
        """
        if critical is None:
            critical = check

        if reason is None:
            reason = " ".join(args[:3]) if args else "adb call"

        cmd = self._base() + list(args)
        if critical:
            try:
                return run_checked(cmd, self.logger, **kwargs)
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else (
                    exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else ""
                )
                stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else (
                    exc.stdout.decode("utf-8", errors="ignore") if exc.stdout else ""
                )
                transport = is_transport_failure(stderr, stdout)
                raise SnaptoolAbortedError(
                    phase=phase,
                    reason=reason,
                    cmd=cmd,
                    rc=exc.returncode,
                    stderr=stderr,
                    transport=transport,
                ) from None
        else:
            res = run_best_effort(cmd, self.logger, **kwargs)
            self._check_transport(res, phase, reason)
            return res

    def shell_root(
        self,
        cmdline: str,
        check: bool = True,
        *,
        critical: bool | None = None,
        phase: str = "adb shell su",
        reason: str | None = None,
        **kwargs,
    ) -> CmdResult:
        # one-liners: adb shell su -c "<cmdline>"
        if reason is None:
            reason = cmdline.splitlines()[0][:120] if cmdline else "shell su -c"
        return self.adb(
            ["shell", "su", "-c", cmdline],
            check=check,
            critical=critical,
            phase=phase,
            reason=reason,
            **kwargs,
        )

    def shell_script(
        self,
        script: str,
        allow_fail: bool = False,
        *,
        critical: bool | None = None,
        phase: str = "adb shell-script",
        reason: str = "shell script",
    ) -> CmdResult:
        """Pipe a multi-line script to `adb shell` over stdin.

        critical=True   → raise SnaptoolAbortedError on any nonzero rc.
        critical=False  → warn-only, but still raise on adb-transport failure.
        critical=None   → fall back to legacy `allow_fail`: False == critical, True == best-effort.
        """
        if critical is None:
            critical = not allow_fail

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
        res = CmdResult(cmd=list(cmd), rc=proc.returncode, stdout=out, stderr=err)

        if proc.returncode != 0:
            transport = is_transport_failure(err, out)
            if critical or transport:
                self.logger.error("adb shell script failed rc=%s (phase=%s)", proc.returncode, phase)
                if out.strip():
                    self.logger.error("stdout:\n%s", out.strip())
                if err.strip():
                    self.logger.error("stderr:\n%s", err.strip())
                raise SnaptoolAbortedError(
                    phase=phase,
                    reason=reason,
                    cmd=cmd,
                    rc=proc.returncode,
                    stderr=err,
                    transport=transport,
                )
            self.logger.warning("adb shell script non-zero (continuing) rc=%s phase=%s", proc.returncode, phase)
            if err.strip():
                self.logger.warning("stderr:\n%s", err.strip())

        return res

    def ensure_device_online(self, phase: str = "adb preflight", timeout_s: int = 5) -> None:
        """Pre-flight check before a critical stage. Verifies the device is
        reachable; if it's missing, raises SnaptoolAbortedError immediately
        instead of letting the next adb call fail mid-stream."""
        cmd = self._base() + ["get-state"]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="ignore",
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise SnaptoolAbortedError(
                phase=phase,
                reason="adb get-state timed out",
                cmd=cmd,
                rc=None,
                stderr="timeout",
                transport=True,
            ) from None

        state = (proc.stdout or "").strip()
        if proc.returncode != 0 or state != "device":
            raise SnaptoolAbortedError(
                phase=phase,
                reason=f"device not ready (state={state or 'unknown'})",
                cmd=cmd,
                rc=proc.returncode,
                stderr=proc.stderr or "",
                transport=True,
            )
