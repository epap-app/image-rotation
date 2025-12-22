from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def default_snap_root() -> Path:
    # project root = parent of snaptool/
    return Path(__file__).resolve().parents[1] / "snapshots"


@dataclass(frozen=True)
class ToolConfig:
    adb_serial: str | None
    verbose: bool
    snap_root: Path

    @staticmethod
    def default(adb_serial: str | None, verbose: bool, snap_root: str | None) -> "ToolConfig":
        root = Path(snap_root).expanduser().resolve() if snap_root else default_snap_root()
        return ToolConfig(adb_serial=adb_serial, verbose=verbose, snap_root=root)


@dataclass(frozen=True)
class SnapshotPaths:
    snap_root: Path
    snap_name: str
    snap_dir: Path
    logs_dir: Path
    archive_zst: Path
    temp_tar: Path

    @staticmethod
    def for_snapshot(snap_root: Path, snap_name: str) -> "SnapshotPaths":
        snap_dir = snap_root / snap_name
        logs_dir = snap_dir / "logs"
        archive_zst = snap_dir / "data.tar.zst"
        temp_tar = snap_dir / "restore.tar"
        return SnapshotPaths(
            snap_root=snap_root,
            snap_name=snap_name,
            snap_dir=snap_dir,
            logs_dir=logs_dir,
            archive_zst=archive_zst,
            temp_tar=temp_tar,
        )
