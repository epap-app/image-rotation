from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class CmdResult:
    cmd: list[str]
    rc: int
    stdout: str
    stderr: str


def run_checked(cmd: Sequence[str], logger: logging.Logger, **kwargs) -> CmdResult:
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
        logger.error("Command failed rc=%s: %s", proc.returncode, " ".join(cmd))
        if proc.stdout and proc.stdout.strip():
            logger.error("stdout: %s", proc.stdout.strip())
        if proc.stderr and proc.stderr.strip():
            logger.error("stderr: %s", proc.stderr.strip())
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr)
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
