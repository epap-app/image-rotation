from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class RestorePolicy:
    systemui_pkg: str = "com.android.systemui"
    photos_pkg: str = "com.google.android.apps.photos"
    media_provider_pkgs: tuple[str, ...] = (
        "com.android.providers.media",
        "com.google.android.providers.media.module",
    )
