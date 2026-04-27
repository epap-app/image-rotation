from __future__ import annotations

import argparse
import datetime
import json
import re
import shlex
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from .adb import AdbClient
from .android_state import AndroidStateReader
from .config import SnapshotPaths, ToolConfig
from .executor import ExecConfig, RestoreExecutor
from .logging_setup import setup_logging
from .planner import RestorePlan, RestorePlanner
from .policy import RestorePolicy
from .runner import run_checked
from .tar_index import TarIndexer

APP_META_FILE = "app_snapshot.json"
APPS_META_FILE = "apps_snapshot.json"
FULL_STATE_FILE = "snapshot_state.json"
PERMISSION_STATE_FILE = "permissions_state.json"
DEFAULT_AUTH_PKGS = ("com.google.android.gsf.login",)
PKG_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
APP_OP_MODES = {"allow", "ignore", "deny", "default", "foreground", "errored"}


def make_snapshot_name() -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"snap-{ts}"


def make_app_snapshot_name(package: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", package)
    return f"app-{safe}-{ts}"


def make_apps_snapshot_name(scope: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", scope)
    return f"apps-{safe}-{ts}"


def _validate_package(pkg: str) -> str:
    if not PKG_RE.match(pkg):
        raise SystemExit(f"[!] Invalid package name: {pkg}")
    return pkg


def _unique_keep_order(items):
    out = []
    seen = set()
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _resolve_users_for_package(state: AndroidStateReader, package: str, explicit_users: list[int]) -> list[int]:
    if explicit_users:
        user_ids = []
        for uid in sorted(set(explicit_users)):
            pkgs = state.list_installed_pkgs_for_user(uid)
            if package in pkgs:
                user_ids.append(uid)
        return user_ids

    user_ids = []
    for uid in state.get_all_user_ids():
        pkgs = state.list_installed_pkgs_for_user(uid)
        if package in pkgs:
            user_ids.append(uid)
    return user_ids


def _pkg_paths_for_user(uid: int, package: str, include_external: bool) -> list[str]:
    out = [
        f"data/user/{uid}/{package}",
        f"data/user_de/{uid}/{package}",
    ]
    if include_external:
        out += [
            f"data/media/{uid}/Android/data/{package}",
            f"data/media/{uid}/Android/media/{package}",
            f"data/media/{uid}/Android/obb/{package}",
        ]
    return out


def _account_db_paths_for_user(uid: int) -> list[str]:
    return [
        f"data/system_ce/{uid}/accounts_ce.db",
        f"data/system_ce/{uid}/accounts_ce.db-wal",
        f"data/system_ce/{uid}/accounts_ce.db-shm",
        f"data/system_ce/{uid}/accounts_ce.db-journal",
        f"data/system_de/{uid}/accounts_de.db",
        f"data/system_de/{uid}/accounts_de.db-wal",
        f"data/system_de/{uid}/accounts_de.db-shm",
        f"data/system_de/{uid}/accounts_de.db-journal",
    ]


def _keystore_locksettings_paths() -> list[str]:
    return [
        "data/misc/keystore",
        "data/system/locksettings.db",
        "data/system/locksettings.db-wal",
        "data/system/locksettings.db-shm",
        "data/system/locksettings.db-journal",
    ]


def _permission_state_paths_for_user(uid: int) -> list[str]:
    return [
        f"data/system/users/{uid}/runtime-permissions.xml",
        f"data/system/users/{uid}/package-restrictions.xml",
        # Android 13+ permission module state (primary runtime permissions source).
        f"data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml",
        f"data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml.reservecopy",
        f"data/misc_de/{uid}/apexdata/com.android.permission/roles.xml",
        f"data/misc_de/{uid}/apexdata/com.android.permission/roles.xml.reservecopy",
        # Some builds keep a mirror under misc_ce.
        f"data/misc_ce/{uid}/apexdata/com.android.permission/runtime-permissions.xml",
        f"data/misc_ce/{uid}/apexdata/com.android.permission/runtime-permissions.xml.reservecopy",
        f"data/misc_ce/{uid}/apexdata/com.android.permission/roles.xml",
        f"data/misc_ce/{uid}/apexdata/com.android.permission/roles.xml.reservecopy",
    ]


def _permission_state_paths_for_users(user_ids: list[int]) -> list[str]:
    out = [
        "data/system/appops.xml",
        "data/system/appops",
    ]
    for uid in user_ids:
        out.extend(_permission_state_paths_for_user(uid))
    return out


def _create_device_tar_from_paths(adb: AdbClient, device_tar: str, candidate_paths: list[str]) -> bool:
    if not candidate_paths:
        return False

    marker = "__SNAPTOOL_NO_PATHS__"
    payload = "\n".join(candidate_paths) + "\n"
    script = f"""
su
cd /
OUT={shlex.quote(device_tar)}
LIST=/data/local/tmp/snaptool-paths.txt
EXISTS=/data/local/tmp/snaptool-paths-exist.txt
cat > "$LIST" <<'EOF'
{payload}EOF
: > "$EXISTS"
while IFS= read -r P; do
  [ -z "$P" ] && continue
  if [ -e "$P" ]; then
    echo "$P" >> "$EXISTS"
  fi
done < "$LIST"
rm -f "$LIST"
if [ ! -s "$EXISTS" ]; then
  rm -f "$EXISTS" >/dev/null 2>&1 || true
  rm -f "$OUT" >/dev/null 2>&1 || true
  echo {marker}
  exit
fi
rm -f "$OUT" >/dev/null 2>&1 || true
tar -cpf "$OUT" -T "$EXISTS" >/dev/null 2>&1 || true
rm -f "$EXISTS" >/dev/null 2>&1 || true
if [ ! -s "$OUT" ]; then
  echo {marker}
fi
exit
exit
"""
    res = adb.shell_script(script, allow_fail=True)
    return marker not in (res.stdout or "")


def _read_json_dict(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_app_meta(meta_file: Path) -> dict:
    return _read_json_dict(meta_file)


def _tar_member_exists(local_tar: Path, member: str) -> bool:
    proc = subprocess.run(
        ["tar", "-tf", str(local_tar), member],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode == 0


def _tar_read_text_member(local_tar: Path, member: str) -> str:
    proc = subprocess.run(
        ["tar", "-xOf", str(local_tar), member],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="ignore",
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def _extract_runtime_permissions_from_tar(
    local_tar: Path,
    user_ids: list[int],
    selected_packages: set[str] | None,
    shared_user_by_package: dict[str, str] | None,
    logger,
) -> dict[str, dict[str, dict]]:
    """
    Build runtime permission state directly from backup tar members:
      data/system/users/<uid>/runtime-permissions.xml
    """
    out: dict[str, dict[str, dict]] = {}

    def _tag_name(tag: str) -> str:
        if "}" in tag:
            return tag.rsplit("}", 1)[-1]
        return tag

    def _is_true(v: str | None) -> bool:
        if v is None:
            return False
        return v.strip().lower() in {"1", "true", "yes"}

    parsed_users = 0
    for uid in user_ids:
        primary_member = f"data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml"
        legacy_member = f"data/system/users/{uid}/runtime-permissions.xml"

        member = primary_member
        xml_text = _tar_read_text_member(local_tar, member)
        if not xml_text.strip():
            member = legacy_member
            xml_text = _tar_read_text_member(local_tar, member)
        if not xml_text.strip():
            continue
        parsed_users += 1

        try:
            root = ET.fromstring(xml_text)
        except Exception as exc:
            logger.warning("Failed parsing %s from snapshot: %s", member, exc)
            continue

        shared_user_perms: dict[str, dict[str, bool]] = {}

        for pkg_node in root.iter():
            node_tag = _tag_name(pkg_node.tag)
            if node_tag not in {"package", "pkg", "shared-user"}:
                continue

            pkg = (pkg_node.attrib.get("name") or pkg_node.attrib.get("n") or "").strip()
            if not pkg:
                continue

            perms: dict[str, bool] = {}
            for perm_node in pkg_node.iter():
                if _tag_name(perm_node.tag) not in {"permission", "perm", "item"}:
                    continue
                perm = (perm_node.attrib.get("name") or perm_node.attrib.get("n") or "").strip()
                if not perm:
                    continue
                if _is_true(perm_node.attrib.get("granted") or perm_node.attrib.get("g")):
                    perms[perm] = True

            if not perms:
                continue

            if node_tag == "shared-user":
                shared_user_perms[pkg] = perms
                continue

            if selected_packages is not None and pkg not in selected_packages:
                continue

            user_map = out.setdefault(pkg, {})
            state = user_map.setdefault(str(uid), {})
            state["runtime_permissions"] = perms
            # Keep schema shape expected by restore executor.
            state.setdefault("appops", {})

        if shared_user_perms and shared_user_by_package:
            target_pkgs = selected_packages if selected_packages is not None else set(shared_user_by_package.keys())
            for pkg in target_pkgs:
                shared_name = shared_user_by_package.get(pkg)
                if not shared_name:
                    continue
                shared_perms = shared_user_perms.get(shared_name)
                if not shared_perms:
                    continue
                user_map = out.setdefault(pkg, {})
                state = user_map.setdefault(str(uid), {})
                merged = state.setdefault("runtime_permissions", {})
                if not isinstance(merged, dict):
                    merged = {}
                    state["runtime_permissions"] = merged
                merged.update(shared_perms)
                state.setdefault("appops", {})

    logger.info(
        "Parsed runtime-permissions from snapshot for %d user file(s), %d package(s).",
        parsed_users,
        len(out),
    )
    return out


def _collect_runtime_permissions(
    adb: AdbClient,
    uid: int,
    package: str,
    include_denied: bool = False,
) -> dict[str, bool]:
    out = (adb.shell_root(f"dumpsys package {shlex.quote(package)} 2>/dev/null || true", check=False).stdout or "")
    if not out.strip():
        return {}

    requested = _parse_requested_permissions_from_dumpsys(out)
    perms = _parse_runtime_permissions_from_dumpsys(out, uid=uid, include_denied=include_denied)
    if requested:
        perms = {perm: granted for perm, granted in perms.items() if perm in requested}
    return perms


_DUMPSYS_SECTION_HEADER_RE = re.compile(r"^\s*[A-Za-z0-9_ .()/-]+:\s*$")


def _parse_requested_permissions_from_dumpsys(text: str) -> set[str]:
    out: set[str] = set()
    in_requested = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.lower() == "requested permissions:":
            in_requested = True
            continue

        if not in_requested:
            continue

        if _DUMPSYS_SECTION_HEADER_RE.match(line):
            break

        m = re.match(r"^\s*([A-Za-z0-9_.]+)\s*$", line)
        if m:
            out.add(m.group(1))

    return out


def _parse_runtime_permissions_from_dumpsys(text: str, uid: int, include_denied: bool) -> dict[str, bool]:
    lines = text.splitlines()
    perm_line_re = re.compile(r"^\s*([A-Za-z0-9_.]+):\s*granted=(true|false)\b")
    # Handles both:
    #   "User 0:"
    #   "User 0: ceDataInode=..."
    user_line_re = re.compile(r"^\s*User\s+(\d+):(?:\s.*)?$")

    perms: dict[str, bool] = {}
    saw_any_user_blocks = False

    def _ingest_perm_line(line: str) -> bool:
        m = perm_line_re.match(line)
        if not m:
            return False
        granted = (m.group(2) == "true")
        if include_denied or granted:
            perms[m.group(1)] = granted
        return True

    # First pass: read runtime permissions for the target user block.
    in_target_user = False
    in_runtime = False
    for line in lines:
        user_match = user_line_re.match(line)
        if user_match:
            saw_any_user_blocks = True
            in_target_user = (int(user_match.group(1)) == uid)
            in_runtime = False
            continue

        if in_target_user and line.strip().lower() == "runtime permissions:":
            in_runtime = True
            continue

        if not in_runtime:
            continue

        if _ingest_perm_line(line):
            continue

        if not line.strip():
            continue

        if _DUMPSYS_SECTION_HEADER_RE.match(line):
            in_runtime = False

    if perms:
        return perms

    # If dumpsys has per-user blocks but target user produced no entries,
    # don't parse a global fallback section from another user.
    if saw_any_user_blocks:
        return perms

    # Fallback pass: some builds print a single runtime section (without User N blocks).
    in_runtime = False
    for line in lines:
        if line.strip().lower() == "runtime permissions:":
            in_runtime = True
            continue

        if not in_runtime:
            continue

        if _ingest_perm_line(line):
            continue

        if not line.strip():
            continue

        if _DUMPSYS_SECTION_HEADER_RE.match(line):
            break

    return perms


def _collect_appops(adb: AdbClient, uid: int, package: str) -> dict[str, str]:
    res = adb.shell_root(f"cmd appops get --user {uid} {shlex.quote(package)} 2>/dev/null || true", check=False)
    out = res.stdout or ""
    appops: dict[str, str] = {}
    for line in out.splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.-]+):\s*([A-Za-z_]+)", line.strip())
        if not m:
            continue
        op = m.group(1)
        mode = m.group(2).lower()
        if mode not in APP_OP_MODES:
            continue
        appops[op] = mode
    return appops


def _collect_appops_bulk(
    adb: AdbClient,
    user_ids: list[int],
    packages: list[str],
    logger,
) -> dict[int, dict[str, dict[str, str]]]:
    """
    Collect AppOps for many packages with a single adb shell script to avoid
    per-package adb round-trips.
    Returns: user_id -> package -> {op: mode}
    """
    if not user_ids or not packages:
        return {}

    header_re = re.compile(r"^===SNAPTOOL_APPOPS\|(\d+)\|(.+?)===$")
    op_re = re.compile(r"^\s*([A-Za-z0-9_.-]+):\s*([A-Za-z_]+)")

    payload = "\n".join(sorted({p for p in packages if p})) + "\n"
    users = " ".join(str(u) for u in sorted({int(u) for u in user_ids}))

    script = f"""
su
set +e
for U in {users}; do
  while IFS= read -r P; do
    [ -z "$P" ] && continue
    echo "===SNAPTOOL_APPOPS|$U|$P==="
    cmd appops get --user "$U" "$P" 2>/dev/null || true
  done <<'EOF'
{payload}EOF
done
exit
exit
"""
    res = adb.shell_script(script, allow_fail=True)
    out = res.stdout or ""

    collected: dict[int, dict[str, dict[str, str]]] = {}
    cur_uid: int | None = None
    cur_pkg: str | None = None

    for line in out.splitlines():
        line = line.rstrip("\r\n")
        m = header_re.match(line.strip())
        if m:
            uid_s = m.group(1)
            pkg_s = m.group(2)
            if not uid_s or not pkg_s:
                # Defensive: don't crash on malformed marker lines.
                cur_uid = None
                cur_pkg = None
                continue
            cur_uid = int(uid_s)
            cur_pkg = pkg_s.strip()
            if cur_pkg:
                collected.setdefault(cur_uid, {}).setdefault(cur_pkg, {})
            continue

        if cur_uid is None or not cur_pkg:
            continue

        m2 = op_re.match(line.strip())
        if not m2:
            continue

        op = m2.group(1)
        mode = m2.group(2).lower()
        if mode not in APP_OP_MODES:
            continue

        collected.setdefault(cur_uid, {}).setdefault(cur_pkg, {})[op] = mode

    logger.info(
        "Collected bulk AppOps for %d user(s), %d package(s).",
        len(collected),
        len({p for um in collected.values() for p in um.keys()}),
    )
    return collected


def _read_runtime_permissions_xml_for_user(adb: AdbClient, uid: int) -> str:
    paths = [
        f"/data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml",
        f"/data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml.reservecopy",
        f"/data/misc_ce/{uid}/apexdata/com.android.permission/runtime-permissions.xml",
        f"/data/misc_ce/{uid}/apexdata/com.android.permission/runtime-permissions.xml.reservecopy",
        f"/data/system/users/{uid}/runtime-permissions.xml",
        f"/data/system/users/{uid}/runtime-permissions.xml.bak",
    ]
    quoted_paths = " ".join(shlex.quote(p) for p in paths)
    cmd = (
        f"for P in {quoted_paths}; do "
        "if [ -s \"$P\" ]; then cat \"$P\"; break; fi; "
        "done; true"
    )
    res = adb.shell_root(cmd, check=False)
    return res.stdout or ""


def _extract_runtime_permissions_from_xml_text(
    xml_text: str,
    selected_packages: set[str],
    shared_user_by_package: dict[str, str] | None,
    include_denied: bool,
) -> dict[str, dict[str, bool]]:
    out: dict[str, dict[str, bool]] = {}
    if not xml_text.strip():
        return out

    def _tag_name(tag: str) -> str:
        if "}" in tag:
            return tag.rsplit("}", 1)[-1]
        return tag

    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out

    shared_user_perms: dict[str, dict[str, bool]] = {}

    for pkg_node in root.iter():
        node_tag = _tag_name(pkg_node.tag)
        if node_tag not in {"package", "pkg", "shared-user"}:
            continue

        name = (pkg_node.attrib.get("name") or pkg_node.attrib.get("n") or "").strip()
        if not name:
            continue

        perms: dict[str, bool] = {}
        for perm_node in pkg_node.iter():
            if _tag_name(perm_node.tag) not in {"permission", "perm", "item"}:
                continue
            perm = (perm_node.attrib.get("name") or perm_node.attrib.get("n") or "").strip()
            if not perm:
                continue
            raw_granted = perm_node.attrib.get("granted") or perm_node.attrib.get("g")
            if raw_granted is None:
                continue
            granted = raw_granted.strip().lower() in {"1", "true", "yes"}
            if include_denied or granted:
                perms[perm] = granted

        if not perms:
            continue

        if node_tag == "shared-user":
            shared_user_perms[name] = perms
            continue

        if name in selected_packages:
            out[name] = perms

    if shared_user_perms and shared_user_by_package:
        for pkg in selected_packages:
            shared_name = shared_user_by_package.get(pkg)
            if not shared_name:
                continue
            shared_perms = shared_user_perms.get(shared_name)
            if not shared_perms:
                continue
            merged = out.setdefault(pkg, {})
            for perm, granted in shared_perms.items():
                merged.setdefault(perm, granted)

    return out


def _collect_package_runtime_state(
    adb: AdbClient,
    state: AndroidStateReader,
    user_ids: list[int],
    packages: list[str],
    logger,
) -> dict[str, dict[str, dict]]:
    selected_packages = {pkg for pkg in packages if isinstance(pkg, str) and pkg}

    xml_runtime_by_user: dict[int, dict[str, dict[str, bool]]] = {}
    for uid in user_ids:
        xml_text = _read_runtime_permissions_xml_for_user(adb, uid)
        xml_runtime_by_user[uid] = _extract_runtime_permissions_from_xml_text(
            xml_text,
            selected_packages=selected_packages,
            # For one-app snapshots, keep per-package runtime permission state only.
            # Shared-user state is global and can over-grant when replayed at app scope.
            shared_user_by_package=None,
            include_denied=True,
        )

    installed_by_user: dict[int, set[str]] = {}
    for uid in user_ids:
        installed_by_user[uid] = set(state.list_installed_pkgs_for_user(uid))

    out: dict[str, dict[str, dict]] = {}
    for pkg in packages:
        user_map: dict[str, dict] = {}
        for uid in user_ids:
            if pkg not in installed_by_user.get(uid, set()):
                continue
            runtime_permissions = xml_runtime_by_user.get(uid, {}).get(pkg)
            if runtime_permissions is None:
                runtime_permissions = _collect_runtime_permissions(
                    adb,
                    uid,
                    pkg,
                    include_denied=True,
                )
                logger.info(
                    "Runtime permission fallback (dumpsys): package=%s user=%d entries=%d",
                    pkg,
                    uid,
                    len(runtime_permissions),
                )
            user_map[str(uid)] = {
                "runtime_permissions": runtime_permissions,
                "appops": _collect_appops(adb, uid, pkg),
            }
        if user_map:
            out[pkg] = user_map

    logger.info(
        "Collected app runtime state for %d package(s) across %d user(s).",
        len(out),
        len(user_ids),
    )
    return out


def _collect_shared_user_map(adb: AdbClient, packages: set[str], logger) -> dict[str, str]:
    """
    Resolve package -> sharedUserName from live device, e.g.:
      sharedUser=SharedUserSetting{... android.uid.systemui/10218}
    """
    if not packages:
        return {}

    pkg_list = sorted(packages)
    payload = "\n".join(pkg_list)
    script = f"""
su
while IFS= read -r P; do
[ -z "$P" ] && continue
L="$(dumpsys package "$P" 2>/dev/null | grep -m1 'sharedUser=SharedUserSetting{{' || true)"
[ -z "$L" ] && continue
S="$(printf '%s\\n' "$L" | sed -n 's/.*SharedUserSetting{{[^ ]* \\([^/ ]*\\)\\/.*$/\\1/p')"
if [ -n "$S" ]; then
  echo "$P|$S"
fi
done <<'EOF'
{payload}
EOF
true
exit
exit
"""
    res = adb.shell_script(script, allow_fail=True)
    out = res.stdout or ""
    mapping: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        pkg, shared = line.split("|", 1)
        pkg = pkg.strip()
        shared = shared.strip()
        if pkg and shared:
            mapping[pkg] = shared
    logger.info("Resolved shared-user mapping for %d/%d package(s).", len(mapping), len(pkg_list))
    return mapping


def _restore_root_package(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) >= 4 and parts[0] == "data" and parts[1] in {"user", "user_de"}:
        return parts[3]
    if len(parts) >= 6 and parts[0] == "data" and parts[1] == "media" and parts[3] == "Android":
        if parts[4] in {"data", "media", "obb"}:
            return parts[5]
    return None


def _filter_runtime_state_for_paths(runtime_state: dict, app_paths: list[str]) -> dict:
    selected_pkgs = {pkg for pkg in (_restore_root_package(p) for p in app_paths) if pkg}
    if not selected_pkgs:
        return {}
    return {pkg: state for pkg, state in runtime_state.items() if pkg in selected_pkgs}


def _snapshot_has_permission_state(local_tar: Path, user_ids: list[int]) -> bool:
    if _tar_member_exists(local_tar, "data/system/appops.xml"):
        return True
    if _tar_member_exists(local_tar, "data/system/appops"):
        return True
    for uid in user_ids:
        if _tar_member_exists(local_tar, f"data/misc_de/{uid}/apexdata/com.android.permission/runtime-permissions.xml"):
            return True
        if _tar_member_exists(local_tar, f"data/system/users/{uid}/runtime-permissions.xml"):
            return True
        if _tar_member_exists(local_tar, f"data/system/users/{uid}/package-restrictions.xml"):
            return True
    return False


def cmd_backup(args) -> int:
    cfg = ToolConfig.default(adb_serial=args.serial, verbose=args.verbose, snap_root=args.snap_root)
    cfg.snap_root.mkdir(parents=True, exist_ok=True)

    snap_name = args.name or make_snapshot_name()
    paths = SnapshotPaths.for_snapshot(cfg.snap_root, snap_name)
    paths.snap_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    logger = setup_logging(cfg.verbose, log_file=paths.logs_dir / "backup.log")
    adb = AdbClient(logger=logger, serial=cfg.adb_serial)

    device_tar = "/data/local/tmp/data-backup.tar"
    local_tar = paths.snap_dir / "data.tar"

    logger.info("Creating snapshot '%s' in %s", snap_name, paths.snap_dir)

    # EXACT recovery6.py style: adb shell + su inside script
    script = f"""
su
cd /
tar \\
  --exclude=data/local/tmp/data-backup.tar \\
  --exclude=data/dalvik-cache \\
  --exclude=data/cache \\
  --exclude=data/anr \\
  --exclude=data/tombstones \\
  --exclude=data/system/dropbox \\
  -cpf {device_tar} \\
  data
exit
exit
"""
    adb.shell_script(script, allow_fail=True)

    logger.info("Verifying device tar exists and is non-empty...")
    res = subprocess.run(
        ["adb"] + (["-s", cfg.adb_serial] if cfg.adb_serial else []) +
        ["shell", "su", "-c", f"ls -l {shlex.quote(device_tar)} 2>/dev/null || true"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="ignore",
        check=False,
    )
    if not res.stdout or "data-backup.tar" not in res.stdout:
        logger.error("Device tar not found; backup failed.")
        return 1

    logger.info("Pulling device tar to host...")
    adb.adb(["pull", device_tar, str(local_tar)], check=True)

    logger.info("Compressing with zstd...")
    run_checked(["zstd", "-T0", "-3", "-f", str(local_tar), "-o", str(paths.archive_zst)], logger)

    logger.info("Removing device tar...")
    adb.shell_root(f"rm -f {shlex.quote(device_tar)}", check=False)

    logger.info("Cleaning up host temp tar...")
    try:
        local_tar.unlink()
    except FileNotFoundError:
        pass

    logger.info("Backup complete: %s", paths.archive_zst)
    return 0


def cmd_backup_thirdparty(args) -> int:
    cfg = ToolConfig.default(adb_serial=args.serial, verbose=args.verbose, snap_root=args.snap_root)
    cfg.snap_root.mkdir(parents=True, exist_ok=True)

    extra_auth_pkgs = [_validate_package(p) for p in (args.auth_pkg or [])]
    auth_pkgs = _unique_keep_order(list(DEFAULT_AUTH_PKGS) + extra_auth_pkgs)
    include_account_db = not args.no_account_db

    snap_name = args.name or make_apps_snapshot_name("thirdparty")
    paths = SnapshotPaths.for_snapshot(cfg.snap_root, snap_name)
    paths.snap_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    logger = setup_logging(cfg.verbose, log_file=paths.logs_dir / "backup-thirdparty.log")
    adb = AdbClient(logger=logger, serial=cfg.adb_serial)
    state = AndroidStateReader(adb, logger)

    if args.user:
        user_ids = sorted({int(u) for u in args.user})
    else:
        user_ids = state.get_all_user_ids()
    if not user_ids:
        logger.error("No Android users detected.")
        return 1

    installed_by_user: dict[int, set[str]] = {}
    thirdparty_by_user: dict[int, list[str]] = {}
    state_pkgs_by_user: dict[int, list[str]] = {}
    for uid in user_ids:
        installed = set(state.list_installed_pkgs_for_user(uid))
        installed_by_user[uid] = installed
        third = sorted(state.list_thirdparty_pkgs_for_user(uid))
        thirdparty = [p for p in third if p in installed]
        thirdparty_by_user[uid] = thirdparty

        state_pkgs_for_user = thirdparty[:]
        for auth_pkg in auth_pkgs:
            if auth_pkg in installed and auth_pkg not in state_pkgs_for_user:
                state_pkgs_for_user.append(auth_pkg)
        state_pkgs_by_user[uid] = state_pkgs_for_user

    all_thirdparty = sorted({p for pkgs in thirdparty_by_user.values() for p in pkgs})
    state_pkgs = _unique_keep_order([p for uid in user_ids for p in state_pkgs_by_user.get(uid, [])])

    candidate_paths: list[str] = []
    for uid in user_ids:
        for pkg in thirdparty_by_user.get(uid, []):
            candidate_paths.extend(_pkg_paths_for_user(uid, pkg, include_external=True))
        for auth_pkg in auth_pkgs:
            if auth_pkg in installed_by_user.get(uid, set()):
                candidate_paths.extend(_pkg_paths_for_user(uid, auth_pkg, include_external=False))
        if include_account_db:
            candidate_paths.extend(_account_db_paths_for_user(uid))

    if include_account_db:
        sdk = state.get_sdk_version()
        if sdk is not None and sdk >= 34:
            logger.info(
                "Android 14+ (SDK %d): skipping keystore/locksettings from backup "
                "(hardware-bound keys are not portable).",
                sdk,
            )
        else:
            candidate_paths.extend(_keystore_locksettings_paths())

    # Keep permission state files in snapshot so restore can replay permissions/appops
    # without restoring global files outside intended package scope.
    candidate_paths.extend(_permission_state_paths_for_users(user_ids))

    candidate_paths = _unique_keep_order(candidate_paths)

    device_tar = "/data/local/tmp/apps-backup.tar"
    local_tar = paths.snap_dir / "data.tar"

    logger.info(
        "Creating thirdparty apps snapshot '%s' (users=%s thirdparty_pkgs=%d auth_pkgs=%d account_db=%s)...",
        snap_name,
        ",".join(str(u) for u in user_ids),
        len(all_thirdparty),
        len(auth_pkgs),
        include_account_db,
    )

    if not _create_device_tar_from_paths(adb, device_tar, candidate_paths):
        logger.error("No matching app/auth/account/permission paths found on device.")
        return 1

    logger.info("Verifying device tar exists and is non-empty...")
    res = subprocess.run(
        ["adb"] + (["-s", cfg.adb_serial] if cfg.adb_serial else []) +
        ["shell", "su", "-c", f"ls -l {shlex.quote(device_tar)} 2>/dev/null || true"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="ignore",
        check=False,
    )
    if not res.stdout or "apps-backup.tar" not in res.stdout:
        logger.error("Device tar not found; backup-thirdparty failed.")
        return 1

    logger.info("Pulling device tar to host...")
    adb.adb(["pull", device_tar, str(local_tar)], check=True)

    logger.info("Compressing with zstd...")
    run_checked(["zstd", "-T0", "-3", "-f", str(local_tar), "-o", str(paths.archive_zst)], logger)

    logger.info("Collecting runtime state (permissions/appops) for %d package(s)...", len(state_pkgs))
    xml_runtime_by_user: dict[int, dict[str, dict[str, bool]]] = {}
    for uid in user_ids:
        xml_text = _read_runtime_permissions_xml_for_user(adb, uid)
        xml_runtime_by_user[uid] = _extract_runtime_permissions_from_xml_text(
            xml_text,
            selected_packages=set(state_pkgs_by_user.get(uid, [])),
            shared_user_by_package=None,
            include_denied=True,
        )

    appops_by_user: dict[int, dict[str, dict[str, str]]] = {}
    for uid in user_ids:
        pkgs_for_uid = state_pkgs_by_user.get(uid, [])
        if not pkgs_for_uid:
            continue
        per = _collect_appops_bulk(adb, user_ids=[uid], packages=pkgs_for_uid, logger=logger)
        appops_by_user[uid] = per.get(uid, {})

    runtime_state: dict[str, dict[str, dict]] = {}
    for uid in user_ids:
        perms_for_user = xml_runtime_by_user.get(uid, {})
        appops_for_user = appops_by_user.get(uid, {})
        for pkg in state_pkgs_by_user.get(uid, []):
            runtime_permissions = perms_for_user.get(pkg)
            if runtime_permissions is None:
                runtime_permissions = _collect_runtime_permissions(adb, uid, pkg, include_denied=True)
                logger.info(
                    "Runtime permission fallback (dumpsys): package=%s user=%d entries=%d",
                    pkg,
                    uid,
                    len(runtime_permissions),
                )
            runtime_state.setdefault(pkg, {})[str(uid)] = {
                "runtime_permissions": runtime_permissions if isinstance(runtime_permissions, dict) else {},
                "appops": appops_for_user.get(pkg, {}),
            }

    logger.info("Collected runtime state for %d package(s).", len(runtime_state))

    permission_state_meta = {
        "type": "permission-state-v1",
        "scope": "thirdparty",
        "user_ids": user_ids,
        "runtime_state": runtime_state,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    (paths.snap_dir / PERMISSION_STATE_FILE).write_text(
        json.dumps(permission_state_meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if include_account_db:
        if sdk is not None and sdk >= 34:
            account_bundle_val = "accounts-only"
        else:
            account_bundle_val = "accounts+keystore+locksettings"
    else:
        account_bundle_val = "none"

    meta = {
        "type": "apps-snapshot-v1",
        "scope": "thirdparty",
        "user_ids": user_ids,
        "thirdparty_by_user": {str(uid): thirdparty_by_user.get(uid, []) for uid in user_ids},
        "auth_packages": auth_pkgs,
        "include_account_db": include_account_db,
        "account_bundle": account_bundle_val,
        "runtime_state": runtime_state,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    (paths.snap_dir / APPS_META_FILE).write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    logger.info("Removing device tar...")
    adb.shell_root(f"rm -f {shlex.quote(device_tar)}", check=False)

    logger.info("Cleaning up host temp tar...")
    try:
        local_tar.unlink()
    except FileNotFoundError:
        pass

    logger.info("Backup-thirdparty complete: %s", paths.archive_zst)
    return 0


def cmd_backup_app(args) -> int:
    cfg = ToolConfig.default(adb_serial=args.serial, verbose=args.verbose, snap_root=args.snap_root)
    cfg.snap_root.mkdir(parents=True, exist_ok=True)

    package = _validate_package(args.package)
    extra_auth_pkgs = [_validate_package(p) for p in (args.auth_pkg or [])]
    auth_pkgs = _unique_keep_order(list(DEFAULT_AUTH_PKGS) + extra_auth_pkgs)
    state_pkgs = _unique_keep_order([package] + auth_pkgs)
    include_account_db = not args.no_account_db

    snap_name = args.name or make_app_snapshot_name(package)
    paths = SnapshotPaths.for_snapshot(cfg.snap_root, snap_name)
    paths.snap_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    logger = setup_logging(cfg.verbose, log_file=paths.logs_dir / "backup-app.log")
    adb = AdbClient(logger=logger, serial=cfg.adb_serial)
    state = AndroidStateReader(adb, logger)

    user_ids = _resolve_users_for_package(state, package, args.user or [])
    if not user_ids:
        logger.error("Package '%s' is not installed for any selected user.", package)
        return 1

    candidate_paths: list[str] = []
    for uid in user_ids:
        candidate_paths.extend(_pkg_paths_for_user(uid, package, include_external=True))
        for auth_pkg in auth_pkgs:
            if auth_pkg == package:
                continue
            candidate_paths.extend(_pkg_paths_for_user(uid, auth_pkg, include_external=False))
        if include_account_db:
            candidate_paths.extend(_account_db_paths_for_user(uid))
    if include_account_db:
        sdk = state.get_sdk_version()
        if sdk is not None and sdk >= 34:
            logger.info(
                "Android 14+ (SDK %d): skipping keystore/locksettings from backup "
                "(hardware-bound keys are not portable).",
                sdk,
            )
        else:
            candidate_paths.extend(_keystore_locksettings_paths())
    candidate_paths = _unique_keep_order(candidate_paths)

    device_tar = "/data/local/tmp/app-backup.tar"
    local_tar = paths.snap_dir / "data.tar"
    logger.info(
        "Creating app snapshot '%s' (package=%s users=%s account_db=%s)...",
        snap_name,
        package,
        ",".join(str(u) for u in user_ids),
        include_account_db,
    )

    if not _create_device_tar_from_paths(adb, device_tar, candidate_paths):
        logger.error("No matching app/auth/account paths found on device.")
        return 1

    logger.info("Verifying device tar exists and is non-empty...")
    res = subprocess.run(
        ["adb"] + (["-s", cfg.adb_serial] if cfg.adb_serial else []) +
        ["shell", "su", "-c", f"ls -l {shlex.quote(device_tar)} 2>/dev/null || true"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="ignore",
        check=False,
    )
    if not res.stdout or "app-backup.tar" not in res.stdout:
        logger.error("Device tar not found; backup-app failed.")
        return 1

    logger.info("Pulling device tar to host...")
    adb.adb(["pull", device_tar, str(local_tar)], check=True)

    logger.info("Compressing with zstd...")
    run_checked(["zstd", "-T0", "-3", "-f", str(local_tar), "-o", str(paths.archive_zst)], logger)

    logger.info("Collecting package runtime state (permissions/appops)...")
    runtime_state = _collect_package_runtime_state(
        adb,
        state,
        user_ids=user_ids,
        packages=state_pkgs,
        logger=logger,
    )

    meta = {
        "type": "app-snapshot-v2",
        "package": package,
        "user_ids": user_ids,
        "auth_packages": auth_pkgs,
        "state_packages": state_pkgs,
        "include_account_db": include_account_db,
        "account_bundle": (
            "accounts-only" if (sdk is not None and sdk >= 34) else "accounts+keystore+locksettings"
        ) if include_account_db else "none",
        "runtime_state": runtime_state,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    (paths.snap_dir / APP_META_FILE).write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    logger.info("Removing device tar...")
    adb.shell_root(f"rm -f {shlex.quote(device_tar)}", check=False)

    logger.info("Cleaning up host temp tar...")
    try:
        local_tar.unlink()
    except FileNotFoundError:
        pass

    logger.info("App backup complete: %s", paths.archive_zst)
    return 0


def cmd_restore_path(args) -> int:
    cfg = ToolConfig.default(adb_serial=args.serial, verbose=args.verbose, snap_root=args.snap_root)
    paths = SnapshotPaths.for_snapshot(cfg.snap_root, args.snapshot)

    if not paths.archive_zst.is_file():
        raise SystemExit(f"[!] Archive not found: {paths.archive_zst}")

    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(cfg.verbose, log_file=paths.logs_dir / "restore-path.log")

    adb = AdbClient(logger=logger, serial=cfg.adb_serial)

    logger.info("Using snapshot: %s", paths.snap_dir)
    logger.info("Decompressing zstd archive -> temp tar...")
    run_checked(["zstd", "-d", "-f", str(paths.archive_zst), "-o", str(paths.temp_tar)], logger)

    # Always scan tar freshly (like recovery6.py does)
    tar_index = TarIndexer(logger).build_from_tar(paths.temp_tar)

    full_state = _read_json_dict(paths.snap_dir / FULL_STATE_FILE)
    runtime_state_meta = full_state.get("runtime_state")
    if not isinstance(runtime_state_meta, dict):
        runtime_state_meta = {}

    state = AndroidStateReader(adb, logger)
    device_state = state.read_device_state()
    plan_user_ids = device_state.get("user_ids") or state.get_all_user_ids()

    policy = RestorePolicy()
    planner = RestorePlanner(logger, policy, state)
    has_permission_state_files = _snapshot_has_permission_state(paths.temp_tar, plan_user_ids)
    runtime_state_for_restore: dict[str, dict[str, dict]] = {}

    if args.pkg_scope == "all":
        # Full-scope restore: prefer snapshot permission files (appops + runtime-permissions)
        # so state is applied in one shot, not per-permission commands.
        include_permission_files = True
        if has_permission_state_files:
            logger.info(
                "Full restore detected: using permission XML/appops files from snapshot in one pass."
            )
        elif runtime_state_meta:
            # Compatibility fallback for older snapshots that only have runtime metadata.
            include_permission_files = False
            runtime_state_for_restore = runtime_state_meta
            logger.warning(
                "Snapshot missing permission XML/appops files; using runtime metadata replay fallback."
            )
        else:
            logger.warning(
                "Snapshot has neither permission XML/appops files nor runtime metadata; permissions may be partial."
            )

        plan = planner.build_plan(
            tar_index,
            device_state,
            pkg_scope=args.pkg_scope,
            include_permission_files=include_permission_files,
        )

        if runtime_state_for_restore:
            runtime_state_for_restore = _filter_runtime_state_for_paths(runtime_state_for_restore, plan.app_paths)
            logger.info(
                "Full restore fallback: runtime state filtered to %d package(s).",
                len(runtime_state_for_restore),
            )
    else:
        # Scoped restore:
        # Keep app-data and permission restore strict to selected packages only.
        # Never include global permission XML/appops files here, because those are
        # global state and can reapply permissions outside requested scope.
        prelim_plan = planner.build_plan(
            tar_index,
            device_state,
            pkg_scope=args.pkg_scope,
            include_permission_files=False,
        )

        selected_pkgs = {pkg for pkg in (_restore_root_package(p) for p in prelim_plan.app_paths) if pkg}

        if runtime_state_meta:
            runtime_state_for_restore = _filter_runtime_state_for_paths(runtime_state_meta, prelim_plan.app_paths)
            logger.info(
                "Scoped restore: using runtime metadata for %d package(s).",
                len(runtime_state_for_restore),
            )
        elif has_permission_state_files and selected_pkgs:
            shared_user_by_package = _collect_shared_user_map(adb, selected_pkgs, logger)
            runtime_state_for_restore = _extract_runtime_permissions_from_tar(
                paths.temp_tar,
                user_ids=plan_user_ids,
                selected_packages=selected_pkgs,
                shared_user_by_package=shared_user_by_package,
                logger=logger,
            )
            logger.info(
                "Scoped restore: extracted permission state from snapshot for %d package(s).",
                len(runtime_state_for_restore),
            )
        elif not selected_pkgs:
            logger.warning("Scoped restore selected no app packages; permission replay skipped.")
        else:
            logger.warning(
                "Scoped restore has no permission metadata/files; permission restore may be partial."
            )

        include_permission_files = False
        plan = prelim_plan
        if has_permission_state_files:
            logger.info(
                "Scoped restore: skipping global permission files to enforce pkg-scope=%s.",
                args.pkg_scope,
            )
        if not runtime_state_for_restore:
            logger.warning(
                "Scoped restore: no per-app runtime replay metadata; permissions may be partial."
            )

    sdk = state.get_sdk_version()
    execu = RestoreExecutor(adb, logger, ExecConfig(chunk_size=120, sdk_version=sdk))
    if runtime_state_for_restore:
        logger.info(
            "Applying per-app permission runtime state for %d package(s).",
            len(runtime_state_for_restore),
        )
    execu.exec_restore_path(plan, tar_index, local_tar=paths.temp_tar, runtime_state=runtime_state_for_restore)

    logger.info("Cleaning up host temp tar...")
    try:
        paths.temp_tar.unlink()
    except FileNotFoundError:
        pass

    logger.info("Restore-path complete.")
    return 0


def cmd_restore_app(args) -> int:
    cfg = ToolConfig.default(adb_serial=args.serial, verbose=args.verbose, snap_root=args.snap_root)
    paths = SnapshotPaths.for_snapshot(cfg.snap_root, args.snapshot)

    if not paths.archive_zst.is_file():
        raise SystemExit(f"[!] Archive not found: {paths.archive_zst}")

    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(cfg.verbose, log_file=paths.logs_dir / "restore-app.log")
    adb = AdbClient(logger=logger, serial=cfg.adb_serial)

    meta = _read_app_meta(paths.snap_dir / APP_META_FILE)
    package = args.package or meta.get("package")
    if not package:
        raise SystemExit(f"[!] Package not provided and missing {APP_META_FILE}; pass --package.")
    package = _validate_package(package)

    if args.user:
        user_ids = sorted(set(args.user))
    else:
        meta_users = meta.get("user_ids")
        if isinstance(meta_users, list) and meta_users:
            user_ids = sorted({int(u) for u in meta_users})
        else:
            state = AndroidStateReader(adb, logger)
            user_ids = _resolve_users_for_package(state, package, [])

    if not user_ids:
        raise SystemExit(f"[!] Could not resolve Android users for package: {package}")

    if args.with_account_db is None:
        include_account_db = bool(meta.get("include_account_db", True))
    else:
        include_account_db = bool(args.with_account_db)

    if args.auth_pkg is not None:
        auth_pkgs = [_validate_package(p) for p in args.auth_pkg]
    else:
        meta_auth = meta.get("auth_packages")
        if isinstance(meta_auth, list) and meta_auth:
            auth_pkgs = [_validate_package(p) for p in meta_auth]
        else:
            auth_pkgs = list(DEFAULT_AUTH_PKGS)
    auth_pkgs = _unique_keep_order(auth_pkgs)
    runtime_state = meta.get("runtime_state")
    if not isinstance(runtime_state, dict):
        runtime_state = {}
        logger.warning("Snapshot metadata has no runtime_state; runtime permission/appops replay will be skipped.")
    else:
        allowed_state_pkgs = set([package] + auth_pkgs)
        runtime_state = {
            pkg: state
            for pkg, state in runtime_state.items()
            if isinstance(pkg, str) and pkg in allowed_state_pkgs
        }

    logger.info("Using snapshot: %s", paths.snap_dir)
    logger.info("Decompressing zstd archive -> temp tar...")
    run_checked(["zstd", "-d", "-f", str(paths.archive_zst), "-o", str(paths.temp_tar)], logger)
    tar_index = TarIndexer(logger).build_from_tar(paths.temp_tar)

    # Safety for legacy/partial app snapshots: if the snapshot claims to include
    # keystore/locksettings but the tar lacks them, auto-disable AccountManager restore.
    # SDK 34+ snapshots intentionally omit keystore/locksettings (account_bundle="accounts-only")
    # — their absence is expected and should NOT trigger auto-disable.
    if include_account_db and args.with_account_db is None:
        account_bundle = meta.get("account_bundle")
        if account_bundle == "accounts-only":
            pass  # SDK 34+ policy: absence is intentional, keep AccountManager restore enabled
        elif account_bundle == "accounts+keystore+locksettings":
            has_keystore = _tar_member_exists(paths.temp_tar, "data/misc/keystore/persistent.sqlite")
            has_locksettings = _tar_member_exists(paths.temp_tar, "data/system/locksettings.db")
            if not (has_keystore and has_locksettings):
                logger.warning(
                    "Snapshot claims keystore/locksettings bundle but tar lacks them; "
                    "auto-disabling AccountManager DB restore. Use --with-account-db to force."
                )
                include_account_db = False
        else:
            # No account_bundle field (very old snapshot): fall back to tar-presence check
            has_keystore = _tar_member_exists(paths.temp_tar, "data/misc/keystore/persistent.sqlite")
            has_locksettings = _tar_member_exists(paths.temp_tar, "data/system/locksettings.db")
            if not (has_keystore and has_locksettings):
                logger.warning(
                    "Legacy snapshot lacks keystore/locksettings bundle; auto-disabling AccountManager DB restore. "
                    "Use --with-account-db to force."
                )
                include_account_db = False

    sdk_state = AndroidStateReader(adb, logger)
    sdk = sdk_state.get_sdk_version()
    execu = RestoreExecutor(adb, logger, ExecConfig(chunk_size=120, sdk_version=sdk))
    execu.exec_restore_app(
        package=package,
        user_ids=user_ids,
        local_tar=paths.temp_tar,
        auth_pkgs=auth_pkgs,
        include_account_db=include_account_db,
        present_roots=tar_index.present_roots,
        runtime_state=runtime_state,
    )

    logger.info("Cleaning up host temp tar...")
    try:
        paths.temp_tar.unlink()
    except FileNotFoundError:
        pass

    logger.info("Restore-app complete.")
    return 0


def cmd_restore_thirdparty(args) -> int:
    cfg = ToolConfig.default(adb_serial=args.serial, verbose=args.verbose, snap_root=args.snap_root)
    paths = SnapshotPaths.for_snapshot(cfg.snap_root, args.snapshot)

    if not paths.archive_zst.is_file():
        raise SystemExit(f"[!] Archive not found: {paths.archive_zst}")

    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(cfg.verbose, log_file=paths.logs_dir / "restore-thirdparty.log")
    adb = AdbClient(logger=logger, serial=cfg.adb_serial)

    meta = _read_json_dict(paths.snap_dir / APPS_META_FILE)
    if not meta:
        raise SystemExit(f"[!] Missing metadata: {paths.snap_dir / APPS_META_FILE}")
    if meta.get("type") != "apps-snapshot-v1":
        logger.warning("Unexpected snapshot type in %s: %r", APPS_META_FILE, meta.get("type"))

    if args.user:
        user_ids = sorted({int(u) for u in args.user})
    else:
        meta_users = meta.get("user_ids")
        if isinstance(meta_users, list) and meta_users:
            user_ids = sorted({int(u) for u in meta_users})
        else:
            state = AndroidStateReader(adb, logger)
            user_ids = state.get_all_user_ids()

    if not user_ids:
        raise SystemExit("[!] Could not resolve Android user ids for restore-thirdparty.")

    if args.auth_pkg is not None:
        auth_pkgs = [_validate_package(p) for p in args.auth_pkg]
    else:
        meta_auth = meta.get("auth_packages")
        if isinstance(meta_auth, list) and meta_auth:
            auth_pkgs = [_validate_package(p) for p in meta_auth]
        else:
            auth_pkgs = list(DEFAULT_AUTH_PKGS)
    auth_pkgs = _unique_keep_order(auth_pkgs)

    raw_tp = meta.get("thirdparty_by_user")
    thirdparty_by_user: dict[int, list[str]] = {}
    if isinstance(raw_tp, dict):
        for uid in user_ids:
            raw_list = raw_tp.get(str(uid)) or raw_tp.get(uid)  # tolerate int keys
            if isinstance(raw_list, list):
                thirdparty_by_user[uid] = [_validate_package(p) for p in raw_list if isinstance(p, str)]
            else:
                thirdparty_by_user[uid] = []
    else:
        thirdparty_by_user = {uid: [] for uid in user_ids}

    packages_by_user: dict[int, list[str]] = {}
    for uid in user_ids:
        packages_by_user[uid] = _unique_keep_order(thirdparty_by_user.get(uid, []) + auth_pkgs)

    allowed_pkgs = set(auth_pkgs) | {p for pkgs in thirdparty_by_user.values() for p in pkgs}

    runtime_state: dict = {}
    runtime_state_source = APPS_META_FILE
    permission_state_meta = _read_json_dict(paths.snap_dir / PERMISSION_STATE_FILE)
    if isinstance(permission_state_meta.get("runtime_state"), dict):
        runtime_state = permission_state_meta.get("runtime_state", {})
        runtime_state_source = PERMISSION_STATE_FILE
    elif isinstance(meta.get("runtime_state"), dict):
        runtime_state = meta.get("runtime_state", {})
    else:
        logger.warning(
            "Snapshot has no runtime_state in %s or %s; runtime permission/appops replay will be skipped.",
            PERMISSION_STATE_FILE,
            APPS_META_FILE,
        )

    if runtime_state:
        logger.info("Loaded runtime state from %s.", runtime_state_source)
        filtered: dict[str, dict[str, dict]] = {}
        for pkg, user_map in runtime_state.items():
            if not isinstance(pkg, str) or pkg not in allowed_pkgs:
                continue
            if not isinstance(user_map, dict):
                continue
            new_user_map: dict[str, dict] = {}
            for uid_s, state_map in user_map.items():
                try:
                    uid = int(uid_s)
                except Exception:
                    continue
                if uid not in user_ids or not isinstance(state_map, dict):
                    continue
                rp = state_map.get("runtime_permissions")
                ao = state_map.get("appops")
                new_user_map[str(uid)] = {
                    "runtime_permissions": rp if isinstance(rp, dict) else {},
                    "appops": ao if isinstance(ao, dict) else {},
                }
            if new_user_map:
                filtered[pkg] = new_user_map
        runtime_state = filtered

    logger.info("Using snapshot: %s", paths.snap_dir)
    logger.info("Decompressing zstd archive -> temp tar...")
    run_checked(["zstd", "-d", "-f", str(paths.archive_zst), "-o", str(paths.temp_tar)], logger)
    tar_index = TarIndexer(logger).build_from_tar(paths.temp_tar)

    # Restore only roots that are present in the snapshot tar.
    present = tar_index.present_roots
    requested_roots: list[str] = []
    for uid in user_ids:
        for pkg in packages_by_user.get(uid, []):
            requested_roots.extend(_pkg_paths_for_user(uid, pkg, include_external=True))

    requested_roots = [p for p in requested_roots if p in present]
    requested_roots = list(dict.fromkeys(requested_roots))

    media_paths = [p for p in requested_roots if p.startswith("data/media/")]
    app_paths = [p for p in requested_roots if not p.startswith("data/media/")]

    policy = RestorePolicy()
    plan = RestorePlan(
        user_ids=user_ids,
        media_paths=media_paths,
        app_paths=app_paths,
        photos_pkg=policy.photos_pkg,
        systemui_pkg=policy.systemui_pkg,
    )

    sdk_state = AndroidStateReader(adb, logger)
    sdk = sdk_state.get_sdk_version()
    execu = RestoreExecutor(adb, logger, ExecConfig(chunk_size=120, sdk_version=sdk))
    execu.exec_restore_path(
        plan,
        tar_index,
        local_tar=paths.temp_tar,
        runtime_state=runtime_state,
        runtime_apply_revokes=True,
    )

    logger.info("Cleaning up host temp tar...")
    try:
        paths.temp_tar.unlink()
    except FileNotFoundError:
        pass

    logger.info("Restore-thirdparty complete.")
    return 0


# def cmd_restore_full(args) -> int:
#     cfg = ToolConfig.default(adb_serial=args.serial, verbose=args.verbose, snap_root=args.snap_root)
#     paths = SnapshotPaths.for_snapshot(cfg.snap_root, args.snapshot)

#     if not paths.archive_zst.is_file():
#         raise SystemExit(f"[!] Archive not found: {paths.archive_zst}")

#     paths.logs_dir.mkdir(parents=True, exist_ok=True)
#     logger = setup_logging(cfg.verbose, log_file=paths.logs_dir / "restore-full.log")

#     adb = AdbClient(logger=logger, serial=cfg.adb_serial)

#     logger.warning("=== DANGER: FULL /data RESTORE ===")
#     if not args.yes:
#         answer = input("Type YES to continue: ")
#         if answer.strip() != "YES":
#             logger.info("Aborting restore-full.")
#             return 1

#     device_tar = "/data/local/tmp/restore-full.tar"
#     local_tar = paths.snap_dir / "restore-full.tar"

#     logger.info("Decompressing zstd archive -> temp tar...")
#     run_checked(["zstd", "-d", "-f", str(paths.archive_zst), "-o", str(local_tar)], logger)

#     logger.info("Pushing temp tar to device...")
#     adb.adb(["push", str(local_tar), device_tar], check=True)

#     execu = RestoreExecutor(adb, logger, ExecConfig(chunk_size=120))
#     execu.exec_restore_full(device_tar=device_tar)

#     adb.shell_root(f"rm -f {shlex.quote(device_tar)}", check=False)
#     try:
#         local_tar.unlink()
#     except FileNotFoundError:
#         pass

#     logger.info("restore-full complete. Reboot is strongly recommended.")
#     return 0


PAIRIP_ALTER_INSTALLER_ZIP = "AlterInstaller-2.3-release.zip"
PAIRIP_DEVICE_JSON = "/data/local/tmp/AlterInstaller.json"
PAIRIP_DEVICE_ZIP = "/data/local/tmp/AlterInstaller.zip"
PAIRIP_JSON_PAYLOAD = {
    "de.dm.meindm.android": {
        "installer": "com.android.vending",
        "updateOwner": "com.android.vending",
    },
    "com.kaufland.Kaufland": {
        "installer": "com.android.vending",
        "updateOwner": "com.android.vending",
    },
}


def _find_alter_installer_zip() -> Path | None:
    candidates = [
        Path(__file__).resolve().parents[1] / "assets" / PAIRIP_ALTER_INSTALLER_ZIP,
        Path.cwd() / "assets" / PAIRIP_ALTER_INSTALLER_ZIP,
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def cmd_pairip_fix(args) -> int:
    cfg = ToolConfig.default(adb_serial=args.serial, verbose=args.verbose, snap_root=args.snap_root)
    logger = setup_logging(cfg.verbose, log_file=None)
    adb = AdbClient(logger=logger, serial=cfg.adb_serial)

    zip_path = _find_alter_installer_zip()
    if zip_path is None:
        logger.error(
            "Could not find %s in ./assets/. Place the Magisk module zip there and retry.",
            PAIRIP_ALTER_INSTALLER_ZIP,
        )
        return 1
    logger.info("Using Magisk module: %s", zip_path)

    devs = adb.adb(["devices"], check=False)
    dev_lines = [
        ln for ln in (devs.stdout or "").splitlines()[1:]
        if ln.strip() and "device" in ln.split()
    ]
    if not dev_lines:
        logger.error("No ADB device detected. Run `adb devices` and ensure the phone is authorized.")
        return 1

    root_check = adb.shell_root("id", check=False)
    if root_check.rc != 0 or "uid=0" not in (root_check.stdout or ""):
        logger.error(
            "Root shell unavailable (su -c id failed). This command requires a rooted device with Magisk."
        )
        return 1

    magisk_check = adb.shell_root("command -v magisk >/dev/null 2>&1 && echo OK || echo MISSING", check=False)
    if "OK" not in (magisk_check.stdout or ""):
        logger.error("`magisk` binary not found on device. Install Magisk before running pairip-fix.")
        return 1

    logger.info("Pushing module to %s ...", PAIRIP_DEVICE_ZIP)
    adb.shell_root(f"rm -f {shlex.quote(PAIRIP_DEVICE_ZIP)}", check=False)
    adb.adb(["push", str(zip_path), PAIRIP_DEVICE_ZIP], check=True)

    verify = adb.shell_root(f"ls -l {shlex.quote(PAIRIP_DEVICE_ZIP)} 2>/dev/null || true", check=False)
    if PAIRIP_DEVICE_ZIP.split("/")[-1] not in (verify.stdout or ""):
        logger.error("Failed to push module zip to device.")
        return 1

    logger.info("Installing Magisk module (magisk --install-module) ...")
    install = adb.shell_root(f"magisk --install-module {shlex.quote(PAIRIP_DEVICE_ZIP)}", check=False)
    if install.rc != 0:
        logger.error("Magisk module installation failed (rc=%s).", install.rc)
        if (install.stdout or "").strip():
            logger.error("stdout:\n%s", install.stdout.strip())
        if (install.stderr or "").strip():
            logger.error("stderr:\n%s", install.stderr.strip())
        adb.shell_root(f"rm -f {shlex.quote(PAIRIP_DEVICE_ZIP)}", check=False)
        return 1

    adb.shell_root(f"rm -f {shlex.quote(PAIRIP_DEVICE_ZIP)}", check=False)

    logger.info("Writing %s ...", PAIRIP_DEVICE_JSON)
    json_payload = json.dumps(PAIRIP_JSON_PAYLOAD, indent=4)
    write_script = f"""
su
rm -f {shlex.quote(PAIRIP_DEVICE_JSON)}
cat > {shlex.quote(PAIRIP_DEVICE_JSON)} <<'PAIRIP_EOF'
{json_payload}
PAIRIP_EOF
chmod 644 {shlex.quote(PAIRIP_DEVICE_JSON)}
exit
exit
"""
    adb.shell_script(write_script, allow_fail=False)

    json_verify = adb.shell_root(f"ls -l {shlex.quote(PAIRIP_DEVICE_JSON)} 2>/dev/null || true", check=False)
    if PAIRIP_DEVICE_JSON.split("/")[-1] not in (json_verify.stdout or ""):
        logger.error("Failed to create %s on device.", PAIRIP_DEVICE_JSON)
        return 1

    logger.info("Rebooting device ...")
    adb.adb(["reboot"], check=False)

    logger.info("Pairip has been fixed successfully")
    print("Pairip has been fixed successfully")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Android /data backup & restore helper")
    parser.add_argument("--serial", help="adb device serial (optional)")
    parser.add_argument("--snap-root", help="Override snapshots directory (optional)")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_backup = sub.add_parser("backup", help="Create a snapshot")
    p_backup.add_argument("--name", help="Optional snapshot name")
    p_backup.set_defaults(func=cmd_backup)

    p_backup_tp = sub.add_parser(
        "backup-thirdparty",
        help="Create snapshot for all third-party apps across all users (+ account manager data)",
    )
    p_backup_tp.add_argument("--name", help="Optional snapshot name")
    p_backup_tp.add_argument(
        "--user",
        type=int,
        action="append",
        help="Android user id to include (repeatable). Default: all users detected on device",
    )
    p_backup_tp.add_argument(
        "--auth-pkg",
        action="append",
        default=[],
        help="Extra auth package data to include (repeatable), in addition to default Google Account Manager package",
    )
    p_backup_tp.add_argument("--no-account-db", action="store_true", help="Skip AccountManager DB data")
    p_backup_tp.set_defaults(func=cmd_backup_thirdparty)

    p_backup_app = sub.add_parser("backup-app", help="Create fast snapshot for one app (+ account manager data)")
    p_backup_app.add_argument("package", help="Target package name (e.g. com.example.app)")
    p_backup_app.add_argument("--name", help="Optional snapshot name")
    p_backup_app.add_argument(
        "--user",
        type=int,
        action="append",
        help="Android user id to include (repeatable). Default: auto-detect users where package is installed",
    )
    p_backup_app.add_argument(
        "--auth-pkg",
        action="append",
        default=[],
        help="Extra auth package data to include (repeatable), in addition to default Google Account Manager package",
    )
    p_backup_app.add_argument("--no-account-db", action="store_true", help="Skip AccountManager DB data")
    p_backup_app.set_defaults(func=cmd_backup_app)

    p_restore_path = sub.add_parser("restore-path", help="Restore selected paths (apps/media) from snapshot")
    p_restore_path.add_argument("snapshot", help="Snapshot name")
    p_restore_path.add_argument(
        "--pkg-scope",
        choices=["apps", "all", "system", "thirdparty"],
        default="apps",
        help="apps=installed minus overlays; all=all installed; system=system pkgs only; thirdparty=non-system pkgs only",
    )
    p_restore_path.set_defaults(func=cmd_restore_path)

    p_restore_app = sub.add_parser("restore-app", help="Restore one app snapshot (+ account manager data)")
    p_restore_app.add_argument("snapshot", help="Snapshot name")
    p_restore_app.add_argument("--package", help=f"Target package override (default from {APP_META_FILE})")
    p_restore_app.add_argument(
        "--user",
        type=int,
        action="append",
        help="Android user id to restore to (repeatable). Default: users from metadata",
    )
    p_restore_app.add_argument(
        "--auth-pkg",
        action="append",
        default=None,
        help="Auth package data to restore (repeatable). If omitted, uses snapshot metadata/defaults",
    )
    p_restore_app.add_argument(
        "--with-account-db",
        dest="with_account_db",
        action="store_true",
        default=None,
        help="Restore AccountManager DB files",
    )
    p_restore_app.add_argument(
        "--no-account-db",
        dest="with_account_db",
        action="store_false",
        help="Skip restoring AccountManager DB files",
    )
    p_restore_app.set_defaults(func=cmd_restore_app)

    p_restore_tp = sub.add_parser(
        "restore-thirdparty",
        help="Restore snapshot created by backup-thirdparty",
    )
    p_restore_tp.add_argument("snapshot", help="Snapshot name")
    p_restore_tp.add_argument(
        "--user",
        type=int,
        action="append",
        help="Android user id to restore to (repeatable). Default: users from metadata",
    )
    p_restore_tp.add_argument(
        "--auth-pkg",
        action="append",
        default=None,
        help="Auth package data to restore (repeatable). If omitted, uses snapshot metadata/defaults",
    )
    p_restore_tp.set_defaults(func=cmd_restore_thirdparty)

    p_pairip = sub.add_parser(
        "pairip-fix",
        help="Install AlterInstaller Magisk module, write AlterInstaller.json, and reboot",
    )
    p_pairip.set_defaults(func=cmd_pairip_fix)

    # p_restore_full = sub.add_parser("restore-full", help="Restore full /data from snapshot (DANGEROUS)")
    # p_restore_full.add_argument("snapshot", help="Snapshot name")
    # p_restore_full.add_argument("--yes", action="store_true")
    # p_restore_full.set_defaults(func=cmd_restore_full)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
