from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class CmdResult:
    cmd: list[str]
    rc: int
    stdout: str
    stderr: str


class SnaptoolAbortedError(Exception):
    """Raised when a critical adb command fails (or the adb transport drops)
    during backup/restore. Carries enough structured info for the CLI layer to
    emit a "restore-failed: ..." / "backup-failed: ..." message and abort.
    """

    def __init__(self, phase: str, reason: str, cmd: Sequence[str] | None = None,
                 rc: int | None = None, stderr: str = "", transport: bool = False):
        self.phase = phase
        self.reason = reason
        self.cmd = list(cmd) if cmd else []
        self.rc = rc
        self.stderr = (stderr or "").strip()
        self.transport = transport
        super().__init__(self._format())

    def _format(self) -> str:
        prefix = "lost adb connection" if self.transport else "command failed"
        msg = f"{self.phase}: {self.reason} ({prefix}"
        if self.rc is not None:
            msg += f", rc={self.rc}"
        msg += ")"
        if self.stderr:
            first = self.stderr.splitlines()[0][:200]
            msg += f": {first}"
        return msg


# Patterns that indicate the adb host<->device transport itself failed,
# meaning every subsequent command will also fail until reconnect. These are
# emitted by the local `adb` binary (not the device) and look the same whether
# the call was best-effort or critical.
_TRANSPORT_PATTERNS = [
    re.compile(r"error:\s*device\s+'[^']*'\s+not found", re.IGNORECASE),
    re.compile(r"error:\s*no devices/emulators? found", re.IGNORECASE),
    re.compile(r"error:\s*device offline", re.IGNORECASE),
    re.compile(r"error:\s*device not found", re.IGNORECASE),
    re.compile(r"error:\s*closed", re.IGNORECASE),
    re.compile(r"error:\s*protocol fault", re.IGNORECASE),
    re.compile(r"adb:\s*device unauthorized", re.IGNORECASE),
    re.compile(r"cannot connect to daemon", re.IGNORECASE),
    re.compile(r"adb server.*killed", re.IGNORECASE),
    re.compile(r"failed to (?:read|write).*Connection reset", re.IGNORECASE),
    re.compile(r"insufficient permissions for device", re.IGNORECASE),
]


def is_transport_failure(stderr: str, stdout: str = "") -> bool:
    """True if the combined adb output looks like a host<->device transport
    failure rather than a command-level nonzero from the device shell."""
    blob = f"{stderr or ''}\n{stdout or ''}"
    return any(p.search(blob) for p in _TRANSPORT_PATTERNS)


def run_checked(cmd: Sequence[str], logger: logging.Logger, **kwargs) -> CmdResult:
    logger.info("[cmd] %s", " ".join(cmd))
    proc = subprocess.run(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="ignore",
        check=True,
        **kwargs,
    )
    return CmdResult(cmd=list(cmd), rc=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")


def run_best_effort(cmd: Sequence[str], logger: logging.Logger, **kwargs) -> CmdResult:
    logger.info("[cmd] %s", " ".join(cmd))
    proc = subprocess.run(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="ignore",
        check=False,
        **kwargs,
    )
    if proc.returncode != 0:
        logger.warning("Command non-zero rc=%s (continuing): %s", proc.returncode, " ".join(cmd))
    return CmdResult(cmd=list(cmd), rc=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")
