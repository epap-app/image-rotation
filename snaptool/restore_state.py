from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass

from .adb import AdbClient
from .runner import SnaptoolAbortedError


RESTORE_STATE_PATH = "/data/local/tmp/snaptool-restore.state"


@dataclass
class RestoreState:
    """Device-side marker for an in-progress restore.

    Lifecycle:
      1. `begin()` writes the marker to the device once the restore is
         genuinely underway (after the initial tar push).
      2. `update_phase(name)` rewrites the marker as each major phase
         completes. Best-effort: a failed update never aborts the restore.
      3. `clear()` deletes the marker at the END of a successful restore.

    If the restore aborts (SnaptoolAbortedError) the marker is NOT cleared,
    leaving the device flagged as "half-restored". The next restore command
    will see the marker and refuse to start unless the operator chooses how
    to recover (`recover-thirdparty`, `clear-restore-state`, or
    `--force-clean`).
    """

    adb: AdbClient
    snapshot: str
    cmd: str
    snap_root: str
    logger: logging.Logger
    started_at: str = ""
    last_phase: str = "init"
    phase_count: int = 0

    def __post_init__(self):
        if not self.started_at:
            self.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _payload(self) -> str:
        return json.dumps(
            {
                "snapshot": self.snapshot,
                "cmd": self.cmd,
                "snap_root": self.snap_root,
                "started_at": self.started_at,
                "last_phase": self.last_phase,
                "phase_count": self.phase_count,
                # Cumulative count of transient-drop retries observed during
                # this restore — surfaced for the operator so flaky devices
                # are visible in `recover-thirdparty` output.
                "transport_retries": getattr(self.adb, "transport_retries", 0),
            },
            separators=(",", ":"),
        )

    def _write(self, critical: bool) -> None:
        # Heredoc with a unique sentinel — avoids quoting issues for JSON
        # punctuation. Atomic write via tmp + mv so a transport drop mid-write
        # can't leave a half-written marker on disk.
        script = (
            "su\n"
            f"cat > {RESTORE_STATE_PATH}.tmp <<'SNAPTOOL_STATE_EOF'\n"
            f"{self._payload()}\n"
            "SNAPTOOL_STATE_EOF\n"
            f"mv {RESTORE_STATE_PATH}.tmp {RESTORE_STATE_PATH}\n"
            f"chmod 644 {RESTORE_STATE_PATH}\n"
            "exit\nexit\n"
        )
        self.adb.shell_script(
            script,
            critical=critical,
            phase=f"restore-state: write ({self.last_phase})",
            reason="could not write restore-state marker on device",
        )

    def begin(self) -> None:
        self.last_phase = "started"
        self.phase_count = 1
        self._write(critical=True)
        self.logger.info(
            "Restore-state marker written: snapshot=%s cmd=%s",
            self.snapshot,
            self.cmd,
        )

    def update_phase(self, phase: str) -> None:
        self.last_phase = phase
        self.phase_count += 1
        try:
            self._write(critical=False)
        except SnaptoolAbortedError:
            # Transport drop while updating the marker — let it propagate so
            # the restore aborts here rather than continuing into the next
            # critical phase blind.
            raise
        except Exception as exc:
            self.logger.warning(
                "Could not update restore-state marker (continuing): %s",
                exc,
            )

    def clear(self) -> None:
        try:
            self.adb.shell_root(
                f"rm -f {RESTORE_STATE_PATH}",
                critical=False,
                phase="restore-state: clear",
            )
            self.logger.info("Restore-state marker cleared (restore completed cleanly).")
        except SnaptoolAbortedError as exc:
            # Couldn't clear marker because connection dropped at the very end.
            # The restore actually succeeded; warn loudly but don't fail the
            # overall command. Operator can run `clear-restore-state` later.
            self.logger.error(
                "Restore completed but could not clear marker (transport drop): %s. "
                "Run `clear-restore-state` once the device is reachable, otherwise "
                "the next restore will refuse to start.",
                exc,
            )
        except Exception as exc:
            self.logger.warning("Could not clear restore-state marker: %s", exc)

    @classmethod
    def read_remote(cls, adb: AdbClient, logger: logging.Logger) -> dict | None:
        """Read the marker from the device. Returns None if absent. Returns
        a dict with at least `snapshot` and `cmd` keys if present. If the
        marker is present but unparseable, returns a stub dict so the caller
        still refuses to proceed."""
        res = adb.shell_root(
            f"cat {RESTORE_STATE_PATH} 2>/dev/null || true",
            critical=False,
            phase="restore-state: read",
        )
        text = (res.stdout or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("marker is not a JSON object")
            return data
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "Restore-state marker is present but unparseable (treating as present): %s",
                exc,
            )
            return {
                "snapshot": "<unparseable>",
                "cmd": "<unparseable>",
                "raw": text[:200],
            }

    @staticmethod
    def format_for_user(state: dict) -> str:
        return (
            f"  snapshot:           {state.get('snapshot', '?')}\n"
            f"  command:            {state.get('cmd', '?')}\n"
            f"  started_at:         {state.get('started_at', '?')}\n"
            f"  last completed:     {state.get('last_phase', '?')}\n"
            f"  phases completed:   {state.get('phase_count', '?')}\n"
            f"  transport retries:  {state.get('transport_retries', 0)}"
        )
