from __future__ import annotations

import logging
from dataclasses import dataclass

from .android_state import AndroidStateReader
from .policy import RestorePolicy
from .tar_index import TarIndex


@dataclass(frozen=True)
class RestorePlan:
    user_ids: list[int]
    media_paths: list[str]
    app_paths: list[str]
    photos_pkg: str
    systemui_pkg: str


class RestorePlanner:
    def __init__(self, logger: logging.Logger, policy: RestorePolicy, state: AndroidStateReader):
        self.logger = logger
        self.policy = policy
        self.state = state

    def build_plan(self, tar_index: TarIndex, device_state: dict, pkg_scope: str) -> RestorePlan:
        present = tar_index.present_roots
        user_ids = device_state.get("user_ids") or self.state.get_all_user_ids()

        # For system/thirdparty, compute global sets once (then intersect with per-user installed list)
        system_set = self.state.list_system_pkgs() if pkg_scope == "system" else None
        third_set = self.state.list_thirdparty_pkgs() if pkg_scope == "thirdparty" else None

        all_paths: list[str] = []

        for uid in user_ids:
            pkgs = self.state.list_installed_pkgs_for_user(uid)
            overlays = self.state.list_overlay_pkgs_for_user(uid)

            # Base selection (same as before)
            if pkg_scope == "apps":
                selected_pkgs = [p for p in pkgs if p not in overlays]
            else:
                selected_pkgs = pkgs[:]

            # Additional filtering by type
            if pkg_scope == "system" and system_set is not None:
                selected_pkgs = [p for p in selected_pkgs if p in system_set]
            elif pkg_scope == "thirdparty" and third_set is not None:
                selected_pkgs = [p for p in selected_pkgs if p in third_set]

            # Always include media providers + Photos if installed (and passes scope filter)
            app_pkgs = set(pkgs)
            for mp in self.policy.media_provider_pkgs:
                if mp in app_pkgs:
                    if mp not in selected_pkgs:
                        if pkg_scope == "system" and system_set is not None and mp not in system_set:
                            pass
                        elif pkg_scope == "thirdparty" and third_set is not None and mp not in third_set:
                            pass
                        else:
                            selected_pkgs.append(mp)

            if self.policy.photos_pkg in app_pkgs and self.policy.photos_pkg not in selected_pkgs:
                if pkg_scope == "system" and system_set is not None and self.policy.photos_pkg not in system_set:
                    pass
                elif pkg_scope == "thirdparty" and third_set is not None and self.policy.photos_pkg not in third_set:
                    pass
                else:
                    selected_pkgs.append(self.policy.photos_pkg)

            self.logger.info(
                "User %s: installed=%d overlays=%d selected=%d (scope=%s)",
                uid, len(pkgs), len(overlays), len(selected_pkgs), pkg_scope
            )

            for pkg in selected_pkgs:
                # Never touch SystemUI internal data
                if pkg == self.policy.systemui_pkg:
                    continue

                ce = f"data/user/{uid}/{pkg}"
                de = f"data/user_de/{uid}/{pkg}"
                ext_data = f"data/media/{uid}/Android/data/{pkg}"
                ext_media = f"data/media/{uid}/Android/media/{pkg}"
                ext_obb = f"data/media/{uid}/Android/obb/{pkg}"

                if ce in present:
                    all_paths.append(ce)
                if de in present:
                    all_paths.append(de)
                if ext_data in present:
                    all_paths.append(ext_data)
                if ext_media in present:
                    all_paths.append(ext_media)
                if ext_obb in present:
                    all_paths.append(ext_obb)

            # DCIM/Pictures remain included if present (same as recovery6 behavior)
            for root in ("DCIM", "Pictures"):
                rp = f"data/media/{uid}/{root}"
                if rp in present:
                    all_paths.append(rp)

        media_paths = [p for p in all_paths if p.startswith("data/media/")]
        app_paths = [p for p in all_paths if not p.startswith("data/media/")]

        return RestorePlan(
            user_ids=user_ids,
            media_paths=media_paths,
            app_paths=app_paths,
            photos_pkg=self.policy.photos_pkg,
            systemui_pkg=self.policy.systemui_pkg,
        )
