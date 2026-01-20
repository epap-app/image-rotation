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
    
    # For adb push commands, don't capture stderr to avoid pipe buffer deadlock with large files
    if 'adb' in cmd[0] and len(cmd) > 1 and cmd[1] == 'push':
        proc = subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Merge stderr to stdout to avoid separate pipe buffer
            text=True,
            errors="ignore",
            check=False,
            **kwargs,
        )
        if proc.returncode != 0:
            logger.error("Command failed: %s", cmd)
            logger.error("output:\n%s", proc.stdout)
            raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout, None)
        return CmdResult(cmd=list(cmd), rc=proc.returncode, stdout=proc.stdout or "", stderr="")
    
    # Original behavior for non-push commands
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
        logger.error("Command failed: %s", cmd)
        logger.error("stdout:\n%s", proc.stdout)
        logger.error("stderr:\n%s", proc.stderr)
        raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout, proc.stderr)
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
