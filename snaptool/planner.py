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

    def build_plan(
        self,
        tar_index: TarIndex,
        device_state: dict,
        pkg_scope: str,
        include_permission_files: bool = True,
    ) -> RestorePlan:
        present = tar_index.present_roots
        user_ids = device_state.get("user_ids") or self.state.get_all_user_ids()

        # Keep global fallback sets in case user-scoped query is unavailable/empty.
        system_set = self.state.list_system_pkgs() if pkg_scope == "system" else None
        third_set = self.state.list_thirdparty_pkgs() if pkg_scope in ("system", "thirdparty") else None

        all_paths: list[str] = []

        for uid in user_ids:
            pkgs = self.state.list_installed_pkgs_for_user(uid)
            overlays = self.state.list_overlay_pkgs_for_user(uid)
            user_third: set[str] = set()

            # Base selection (same as before)
            if pkg_scope == "apps":
                selected_pkgs = [p for p in pkgs if p not in overlays]
            else:
                selected_pkgs = pkgs[:]

            # Additional filtering by type
            if pkg_scope in ("system", "thirdparty"):
                user_third = self.state.list_thirdparty_pkgs_for_user(uid)
                if not user_third and third_set is not None:
                    user_third = set(third_set)
                self.logger.info("User %s: detected thirdparty=%d", uid, len(user_third))

                # Prefer pm -s classification for scope decisions. This keeps
                # updated system apps (code under /data/app) in "system" scope.
                def _is_system_pkg(pkg: str) -> bool:
                    if system_set:
                        return pkg in system_set
                    return pkg not in user_third

                def _is_thirdparty_pkg(pkg: str) -> bool:
                    if system_set:
                        return pkg not in system_set
                    return pkg in user_third

                if pkg_scope == "thirdparty":
                    selected_pkgs = [p for p in selected_pkgs if _is_thirdparty_pkg(p)]
                else:
                    selected_pkgs = [p for p in selected_pkgs if _is_system_pkg(p)]

            # Always include media providers + Photos if installed (and passes scope filter)
            app_pkgs = set(pkgs)
            for mp in self.policy.media_provider_pkgs:
                if mp in app_pkgs:
                    if mp not in selected_pkgs:
                        if pkg_scope == "system" and not _is_system_pkg(mp):
                            pass
                        elif pkg_scope == "thirdparty" and not _is_thirdparty_pkg(mp):
                            pass
                        else:
                            selected_pkgs.append(mp)

            if self.policy.photos_pkg in app_pkgs and self.policy.photos_pkg not in selected_pkgs:
                if pkg_scope == "system" and not _is_system_pkg(self.policy.photos_pkg):
                    pass
                elif pkg_scope == "thirdparty" and not _is_thirdparty_pkg(self.policy.photos_pkg):
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

        if include_permission_files:
            # Permission/AppOps state files (global/per-user) used as fallback when
            # runtime metadata replay is not available.
            all_paths.append("data/system/appops.xml")
            all_paths.append("data/system/appops")
            for uid in user_ids:
                all_paths.append(f"data/system/users/{uid}/runtime-permissions.xml")
                all_paths.append(f"data/system/users/{uid}/package-restrictions.xml")
                # Android 13+ permission module state (primary runtime permissions source).
                all_paths.append(f"data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml")
                all_paths.append(f"data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml.reservecopy")
                all_paths.append(f"data/misc_de/{uid}/apexdata/com.android.permission/roles.xml")
                all_paths.append(f"data/misc_de/{uid}/apexdata/com.android.permission/roles.xml.reservecopy")

        all_paths = list(dict.fromkeys(all_paths))

        media_paths = [p for p in all_paths if p.startswith("data/media/")]
        app_paths = [p for p in all_paths if not p.startswith("data/media/")]

        return RestorePlan(
            user_ids=user_ids,
            media_paths=media_paths,
            app_paths=app_paths,
            photos_pkg=self.policy.photos_pkg,
            systemui_pkg=self.policy.systemui_pkg,
        )
