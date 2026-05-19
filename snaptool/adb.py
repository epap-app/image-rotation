from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Sequence

from .runner import (
    CmdResult,
    SnaptoolAbortedError,
    is_transport_failure,
)

# Layer-3 transient-drop auto-retry tuning.
# A transport-drop classifies as: the adb host<->device link broke (USB blip,
# adbd restart) — recognized via runner.is_transport_failure on stderr.
_TRANSPORT_RETRY_LIMIT = 2  # 1 initial attempt + N retries
_TRANSPORT_RETRY_BACKOFF_S = (1.0, 2.0)  # progressive backoff per retry
_WAIT_FOR_DEVICE_TIMEOUT_S = 30
_SHELL_PROBE_TIMEOUT_S = 10
_SHELL_PROBE_MARKER = "SNAPTOOL_PROBE_OK"

# Framework-ready probe (gap #1 + #2): after `stop ; start` of zygote,
# system_server takes 5-30+s to publish AMS/PMS/AppOpsService. Hitting it
# with binder calls during that window triggers crash-restart cycles that
# eventually trip the watchdog and full-reboot the device. We poll
# `service check NAME` until the services we care about are published.
_FRAMEWORK_READY_SERVICES = ("package", "appops", "activity")
_FRAMEWORK_READY_TIMEOUT_S = 60
_FRAMEWORK_READY_POLL_S = 0.5
_FRAMEWORK_READY_POLL_S_MAX = 2.0

# Stderr substrings that indicate a system_server-side service is not
# available to a `cmd`/`pm`/`am` call. Tracked as a counter so the CLI
# can refuse to declare a restore "completed cleanly" if any of these
# slipped past the framework-ready preflight.
_SERVICE_UNAVAILABLE_MARKERS = (
    "Can't find service:",
    "Can not find service",
)


@dataclass
class AdbClient:
    logger: logging.Logger
    serial: str | None = None
    # Cumulative count of transport retries performed during this client's
    # lifetime. Exposed for telemetry (RestoreState surfaces it on the marker).
    transport_retries: int = field(default=0)
    # Cumulative count of calls that returned a "Can't find service: X"
    # stderr — indicates system_server (or a specific service it owns)
    # was not responding to a `cmd`/`pm`/`am` call. CLI uses this at end
    # of restore to decide whether the run actually completed cleanly or
    # silently dropped post-stage state.
    service_unavailable_count: int = field(default=0)

    def _base(self) -> list[str]:
        cmd = ["adb"]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    # ------------------------------------------------------------------
    # Layer-3 transient-drop auto-retry
    # ------------------------------------------------------------------

    def _wait_for_device(self, timeout_s: int = _WAIT_FOR_DEVICE_TIMEOUT_S) -> bool:
        """Block until adb sees the device again AND can execute a shell
        command. Returns True on success, False on timeout/probe failure.

        wait-for-device only confirms the adb transport is reachable; we
        ALSO send an `adb shell echo` probe because adbd can be briefly
        responsive without being ready to execute commands. Note: this does
        NOT depend on `sys.boot_completed`, because during a restore the
        framework is intentionally stopped (zygote down). The probe just
        needs to confirm the shell pipe works.
        """
        wfd = self._base() + ["wait-for-device"]
        self.logger.info("[transport-retry] adb wait-for-device (timeout=%ds)", timeout_s)
        try:
            proc = subprocess.run(
                wfd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="ignore",
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.logger.warning("[transport-retry] wait-for-device timed out after %ds", timeout_s)
            return False
        if proc.returncode != 0:
            self.logger.warning(
                "[transport-retry] wait-for-device rc=%s stderr=%r",
                proc.returncode, (proc.stderr or "").strip()[:160],
            )
            return False

        probe = self._base() + ["shell", "echo", _SHELL_PROBE_MARKER]
        try:
            res = subprocess.run(
                probe,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="ignore",
                timeout=_SHELL_PROBE_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.logger.warning("[transport-retry] shell probe timed out")
            return False
        if _SHELL_PROBE_MARKER in (res.stdout or ""):
            self.logger.info("[transport-retry] device reachable; shell probe OK")
            return True
        self.logger.warning(
            "[transport-retry] shell probe failed rc=%s stderr=%r",
            res.returncode, (res.stderr or "").strip()[:160],
        )
        return False

    def _record_transport_retry(self) -> None:
        self.transport_retries += 1

    def _run_cmd(self, cmd: list[str], input_bytes: bytes | None = None) -> CmdResult:
        """Single subprocess invocation. Always returns a CmdResult and never
        raises. Logs the command at INFO before running."""
        if input_bytes is None:
            self.logger.info("[cmd] %s", " ".join(cmd))
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="ignore",
                check=False,
            )
            out = proc.stdout or ""
            err = proc.stderr or ""
        else:
            self.logger.info("[adb-shell-script] (%d bytes)", len(input_bytes))
            proc = subprocess.run(
                cmd,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            out = (proc.stdout or b"").decode("utf-8", errors="ignore")
            err = (proc.stderr or b"").decode("utf-8", errors="ignore")
        return CmdResult(cmd=list(cmd), rc=proc.returncode, stdout=out, stderr=err)

    def _run_with_retry(
        self,
        cmd: list[str],
        phase: str,
        input_bytes: bytes | None = None,
    ) -> CmdResult:
        """Run cmd through subprocess, auto-retrying on transport failures.

        Returns the final CmdResult. Never raises by itself — callers decide
        whether rc != 0 should escalate to SnaptoolAbortedError based on
        critical-ness. Applies to ALL adb calls (critical and non-critical):
        a transient USB blip during a best-effort cleanup is just as bad as
        during a critical extraction — both leave the device in an unknown
        state for the next call.
        """
        last_res: CmdResult | None = None
        for attempt in range(_TRANSPORT_RETRY_LIMIT + 1):
            res = self._run_cmd(cmd, input_bytes=input_bytes)
            last_res = res

            # Track system-service unavailability separately from transport
            # failures. This is a device-side signal (system_server / a
            # framework service not responding), not an adb-link signal,
            # so it doesn't trigger the transport retry — just a counter.
            if res.rc != 0 and res.stderr:
                if any(m in res.stderr for m in _SERVICE_UNAVAILABLE_MARKERS):
                    self.service_unavailable_count += 1

            if res.rc == 0:
                return res
            if not is_transport_failure(res.stderr, res.stdout):
                # Command-level non-zero from the device shell — no retry,
                # caller decides what to do with it.
                return res

            if attempt >= _TRANSPORT_RETRY_LIMIT:
                self.logger.error(
                    "[transport-retry] transport drop persists after %d retries during phase '%s'",
                    _TRANSPORT_RETRY_LIMIT, phase,
                )
                return res

            self.logger.warning(
                "[transport-retry] transport drop detected during phase '%s' "
                "(attempt %d/%d); waiting for device and retrying...",
                phase, attempt + 1, _TRANSPORT_RETRY_LIMIT + 1,
            )
            self._record_transport_retry()
            self._wait_for_device()  # best-effort: if it returns False, the next attempt will fail too and we'll exhaust retries
            backoff = _TRANSPORT_RETRY_BACKOFF_S[min(attempt, len(_TRANSPORT_RETRY_BACKOFF_S) - 1)]
            time.sleep(backoff)
        # Defensive — loop always returns inside it.
        assert last_res is not None
        return last_res

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _check_transport(self, res: CmdResult, phase: str, reason: str) -> None:
        """If the call produced a transport-level failure that survived the
        retry loop, raise SnaptoolAbortedError so the restore aborts and
        Layer-1 (marker) + Layer-2 (recover) take over."""
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
        """Run an adb command, with Layer-3 transient-drop auto-retry.

        critical=True   → raise SnaptoolAbortedError on any nonzero rc that
                          survived the retry loop.
        critical=False  → warn-only on ordinary nonzero rc, but still raise
                          on adb-transport failure that survived retries.
        critical=None   → fall back to legacy `check`: True == critical,
                          False == best-effort.

        kwargs are accepted but ignored for compatibility with older callers
        that pass subprocess-style options. (They were never honored by the
        new retry path; the old run_checked/run_best_effort signatures forwarded
        them, but no live call site relies on that.)
        """
        if critical is None:
            critical = check
        if reason is None:
            reason = " ".join(args[:3]) if args else "adb call"

        cmd = self._base() + list(args)
        res = self._run_with_retry(cmd, phase=phase)

        if res.rc != 0:
            transport = is_transport_failure(res.stderr, res.stdout)
            if critical or transport:
                raise SnaptoolAbortedError(
                    phase=phase,
                    reason=reason,
                    cmd=cmd,
                    rc=res.rc,
                    stderr=res.stderr,
                    transport=transport,
                )
            self.logger.warning(
                "Command non-zero rc=%s (continuing): %s",
                res.rc, " ".join(cmd),
            )
            # Surface stderr on best-effort failures so we can diagnose
            # systemic problems (e.g. a flood of identical rc=20 with
            # 'adb: device unauthorized' hiding in stderr). Single-line
            # truncated to keep the appops/grant loops skimmable.
            err_line = (res.stderr or "").strip().splitlines()
            if err_line:
                self.logger.warning("  stderr: %s", err_line[0][:200])
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
        """Pipe a multi-line script to `adb shell` over stdin, with auto-retry."""
        if critical is None:
            critical = not allow_fail

        cmd = self._base() + ["shell"]
        res = self._run_with_retry(cmd, phase=phase, input_bytes=script.encode("utf-8"))

        if res.rc != 0:
            transport = is_transport_failure(res.stderr, res.stdout)
            if critical or transport:
                self.logger.error("adb shell script failed rc=%s (phase=%s)", res.rc, phase)
                if res.stdout.strip():
                    self.logger.error("stdout:\n%s", res.stdout.strip())
                if res.stderr.strip():
                    self.logger.error("stderr:\n%s", res.stderr.strip())
                raise SnaptoolAbortedError(
                    phase=phase,
                    reason=reason,
                    cmd=cmd,
                    rc=res.rc,
                    stderr=res.stderr,
                    transport=transport,
                )
            self.logger.warning(
                "adb shell script non-zero (continuing) rc=%s phase=%s",
                res.rc, phase,
            )
            if res.stderr.strip():
                self.logger.warning("stderr:\n%s", res.stderr.strip())
        return res

    def ensure_root(self, phase: str = "adb root preflight") -> None:
        """Verify that `adb shell su -c id` returns uid=0.

        Without this check, every destructive operation silently no-ops on
        devices where `su` is missing: the `su` line in our shell scripts
        prints `su: not found` to stderr (rc=127), but the parent shell
        keeps reading the remainder of the script as the `shell` user (uid
        2000), where every `rm -rf /data/user/...` / `chown` / `restorecon`
        fails — and each `|| true` swallows the error. The final shell rc
        is 0, the tool reports success, and the operator only finds out
        the device was never restored when it boot-loops or behaves wrong.

        This preflight fails fast and loud instead.
        """
        res = self.shell_root(
            "id",
            critical=False,
            phase=phase,
            reason="root probe",
        )
        stdout = res.stdout or ""
        if res.rc != 0 or "uid=0" not in stdout:
            stderr_first = ""
            if res.stderr:
                lines = res.stderr.strip().splitlines()
                if lines:
                    stderr_first = lines[0][:200]
            raise SnaptoolAbortedError(
                phase=phase,
                reason=(
                    "root shell unavailable on device (`su -c id` did not return uid=0). "
                    "Snaptool requires working root to perform backup/restore. "
                    "Check: `adb shell su -c id`. Common causes: su binary missing, "
                    "Magisk denied the request, device in restricted/locked-down state."
                ),
                cmd=res.cmd,
                rc=res.rc,
                stderr=stderr_first or res.stderr,
                transport=False,
            )

    def ensure_framework_ready(
        self,
        phase: str = "adb framework-ready check",
        timeout_s: int = _FRAMEWORK_READY_TIMEOUT_S,
        services: tuple[str, ...] = _FRAMEWORK_READY_SERVICES,
    ) -> None:
        """Wait until the framework is *actually* ready to accept our flood
        of `cmd appops`/`pm grant`/`am force-stop` calls.

        `ensure_root` only verifies `su -c id` works — `id` is libc-only and
        never touches system_server. Naive `sleep 2` after `start zygote`
        isn't enough either — system_server's boot-phase
        `PermissionPolicyService.synchronizePermissionsAndAppOpsForUser`
        is doing its own internal AppOps work during early boot, and AppOps
        binder threads can be locked up handling exceptions/contention.
        Our calls land at the binder layer with no service responding fast
        enough; `cmd` reports `Can't find service: appops` even though the
        service is technically registered.

        We poll TWO signals together:
        1. `service check <name>`: the services are registered with
           servicemanager (necessary but not sufficient).
        2. `getprop sys.boot_completed == 1`: ActivityManagerService has
           completed all `onBootPhase` callbacks, including PermissionPolicy
           syncing. This is the canonical "system is ready" property.

        Both must be true. Raises SnaptoolAbortedError on timeout.
        """
        start = time.monotonic()
        # Chain service-check + getprop into one shell round-trip. Marker
        # prefix on the boot_completed line so we don't confuse it with
        # a service-check line.
        probe = (
            " ; ".join(f"service check {s}" for s in services)
            + " ; echo BOOT_COMPLETED=$(getprop sys.boot_completed)"
        )
        poll = _FRAMEWORK_READY_POLL_S
        first_iter = True
        last_missing: list[str] = list(services)
        last_boot_completed = False
        while True:
            res = self.shell_root(
                probe,
                critical=False,
                phase=phase,
                reason="framework-ready probe",
            )
            stdout = res.stdout or ""
            missing = [s for s in services if f"Service {s}: found" not in stdout]
            boot_completed = "BOOT_COMPLETED=1" in stdout
            last_missing = missing
            last_boot_completed = boot_completed

            if not missing and boot_completed:
                elapsed = time.monotonic() - start
                if not first_iter:
                    self.logger.info(
                        "[framework-ready] services published + sys.boot_completed=1 after %.1fs",
                        elapsed,
                    )
                return
            elapsed = time.monotonic() - start
            if first_iter:
                self.logger.info(
                    "[framework-ready] waiting for framework: missing services=%s, boot_completed=%s",
                    ", ".join(missing) or "(none)",
                    "1" if boot_completed else "0",
                )
                first_iter = False
            if elapsed >= timeout_s:
                raise SnaptoolAbortedError(
                    phase=phase,
                    reason=(
                        f"framework not ready after {timeout_s}s "
                        f"(missing services: {', '.join(last_missing) or 'none'}; "
                        f"sys.boot_completed={'1' if last_boot_completed else '0'}). "
                        f"system_server may have stalled during restart. "
                        f"Check `adb shell service list` and `adb shell getprop sys.boot_completed`."
                    ),
                    cmd=res.cmd,
                    rc=res.rc,
                    stderr=res.stderr,
                    transport=False,
                )
            time.sleep(poll)
            # Backoff slightly so we don't spam the log/device while waiting.
            if poll < _FRAMEWORK_READY_POLL_S_MAX:
                poll = min(poll + 0.5, _FRAMEWORK_READY_POLL_S_MAX)

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
