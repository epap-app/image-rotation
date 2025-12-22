from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TarIndex:
    present_roots: set[str]


class TarIndexer:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.rx_pkg_ce = re.compile(r"^(?:\./)?data/user/(\d+)/([^/]+)/")
        self.rx_pkg_de = re.compile(r"^(?:\./)?data/user_de/(\d+)/([^/]+)/")
        self.rx_ext_data = re.compile(r"^(?:\./)?data/media/(\d+)/Android/data/([^/]+)/")
        self.rx_ext_media = re.compile(r"^(?:\./)?data/media/(\d+)/Android/media/([^/]+)/")
        self.rx_ext_obb = re.compile(r"^(?:\./)?data/media/(\d+)/Android/obb/([^/]+)/")

        # IMPORTANT FIX:
        # match BOTH:
        #   data/media/<uid>/DCIM/
        #   data/media/<uid>/DCIM
        # and also any files under it
        self.rx_media_root = re.compile(r"^(?:\./)?data/media/(\d+)/(DCIM|Pictures)(?:/|$)")

    def build_from_tar(self, local_tar_path: Path) -> TarIndex:
        present = set()

        proc = subprocess.Popen(
            ["tar", "-tf", str(local_tar_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="ignore",
        )
        assert proc.stdout is not None

        for line in proc.stdout:
            p = (line or "").strip()
            if not p:
                continue
            p = p.lstrip("./")

            m = self.rx_pkg_ce.match(p)
            if m:
                uid, pkg = m.group(1), m.group(2)
                present.add(f"data/user/{uid}/{pkg}")
                continue

            m = self.rx_pkg_de.match(p)
            if m:
                uid, pkg = m.group(1), m.group(2)
                present.add(f"data/user_de/{uid}/{pkg}")
                continue

            m = self.rx_ext_data.match(p)
            if m:
                uid, pkg = m.group(1), m.group(2)
                present.add(f"data/media/{uid}/Android/data/{pkg}")
                continue

            m = self.rx_ext_media.match(p)
            if m:
                uid, pkg = m.group(1), m.group(2)
                present.add(f"data/media/{uid}/Android/media/{pkg}")
                continue

            m = self.rx_ext_obb.match(p)
            if m:
                uid, pkg = m.group(1), m.group(2)
                present.add(f"data/media/{uid}/Android/obb/{pkg}")
                continue

            # FIX: add DCIM/Pictures even if they are empty dirs in the tar
            m = self.rx_media_root.match(p)
            if m:
                uid, root = m.group(1), m.group(2)
                present.add(f"data/media/{uid}/{root}")
                continue

        try:
            proc.wait(timeout=60)
        except Exception:
            proc.kill()

        return TarIndex(present_roots=present)
