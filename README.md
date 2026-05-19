# Android Snaptool

Android Snaptool is a host-side Python CLI (`recovery_tool.py`) for rooted Android backup/restore workflows.

Supported commands:

- `backup` - full `/data` snapshot (with built-in excludes)
- `backup-thirdparty` - backup third-party app state for all users (or selected users)
- `backup-app` - fast snapshot for one app
- `restore-path` - restore from full snapshot with package scope filtering
- `restore-app` - restore one app snapshot
- `restore-thirdparty` - restore a `backup-thirdparty` snapshot
- `recover-thirdparty` - re-run the snapshot named by the device's restore-state marker (recovery for a half-restored `restore-thirdparty`)
- `clear-restore-state` - delete the device-side restore-state marker (use only after verifying the device is healthy)
- `clear-bootloop-state` - wipe Android's RescueParty / PackageWatchdog escalation state and reboot (use when the boot-loop detector has been tripped)
- `pairip-fix` - install the AlterInstaller Magisk module, write `/data/local/tmp/AlterInstaller.json`, and reboot

## Requirements

- Rooted Android device with working `su`
- `adb`
- `python3`
- `tar`
- `zstd`

Quick dependency install on Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y adb zstd tar python3 python3-pip
```

## Global CLI Syntax

```bash
python3 recovery_tool.py [--serial SERIAL] [--snap-root PATH] [--verbose] <command> [command options]
```

Global options:

- `--serial SERIAL` - target specific ADB device
- `--snap-root PATH` - store/read snapshots from custom directory
- `--verbose` - verbose logging

Important: global options must be placed before the subcommand.

Correct:

```bash
python3 recovery_tool.py --verbose restore-app my-snap
```

Incorrect:

```bash
python3 recovery_tool.py restore-app my-snap --verbose
```

## Command Reference

### 1) `backup`

Creates a full `/data` snapshot (excluding volatile paths configured in the tool).

Usage:

```bash
python3 recovery_tool.py backup [--name NAME]
```

Examples:

```bash
python3 recovery_tool.py backup
python3 recovery_tool.py backup --name zero
python3 recovery_tool.py --verbose --serial <device_serial> backup --name before_update
```

### 2) `backup-thirdparty`

Creates one snapshot containing third-party app state across all users (or selected users).

Included by default:

- third-party app CE/DE data per selected user
- app external state (`Android/data`, `Android/media`, `Android/obb`)
- default auth package data (`com.google.android.gsf.login`), if installed
- account bundle (AccountManager DBs; on Android 13 and below also includes keystore/locksettings — excluded on Android 14+ as hardware-bound keys are not portable)
- permission/appops state needed for restore replay

Usage:

```bash
python3 recovery_tool.py backup-thirdparty [--name NAME] [--user USER ...] [--auth-pkg PKG ...] [--no-account-db]
```

Options:

- `--name NAME` - snapshot name
- `--user USER` - repeatable, include only selected Android users
- `--auth-pkg PKG` - repeatable, include extra auth package data
- `--no-account-db` - skip account bundle files

Examples:

```bash
python3 recovery_tool.py --verbose backup-thirdparty --name zero3party
python3 recovery_tool.py backup-thirdparty --name work_only --user 10
python3 recovery_tool.py backup-thirdparty --name u0_u10 --user 0 --user 10
python3 recovery_tool.py backup-thirdparty --name with_auth --auth-pkg com.example.auth
python3 recovery_tool.py backup-thirdparty --name no_accounts --no-account-db
```

### 3) `backup-app`

Creates a fast snapshot for a single package.

Included by default:

- target package data (all detected installed users unless constrained)
- optional extra auth package data
- account bundle (AccountManager DBs; on Android 13 and below also includes keystore/locksettings — excluded on Android 14+ as hardware-bound keys are not portable; unless disabled)
- per-package runtime permissions/appops metadata

Usage:

```bash
python3 recovery_tool.py backup-app <package> [--name NAME] [--user USER ...] [--auth-pkg PKG ...] [--no-account-db]
```

Options:

- `package` - required package id (example: `com.example.app`)
- `--name NAME` - snapshot name
- `--user USER` - repeatable, backup only selected users where package exists
- `--auth-pkg PKG` - repeatable, include extra auth package data
- `--no-account-db` - skip account bundle files

Examples:

```bash
python3 recovery_tool.py --verbose backup-app com.valuephone.vpnetto --name netto_fixed
python3 recovery_tool.py backup-app com.example.app --user 0
python3 recovery_tool.py backup-app com.example.app --user 0 --user 10
python3 recovery_tool.py backup-app com.example.app --auth-pkg com.example.auth
python3 recovery_tool.py backup-app com.example.app --no-account-db
```

### 4) `restore-path`

Restores from a full snapshot (`backup`) with package scope filtering.

Usage:

```bash
python3 recovery_tool.py restore-path <snapshot> [--pkg-scope {apps,all,system,thirdparty}] [--force-bootloop]
```

Scope values:

- `apps` (default): installed packages excluding overlays
- `all`: all installed packages
- `system`: system packages only
- `thirdparty`: non-system packages only

Options:

- `--force-bootloop` - proceed even if the device is at a non-zero RescueParty mitigation-count. Not recommended. See [Resilience & Recovery](#resilience--recovery).

Examples:

```bash
python3 recovery_tool.py --verbose restore-path zero
python3 recovery_tool.py --verbose restore-path zero --pkg-scope apps
python3 recovery_tool.py --verbose restore-path zero --pkg-scope all
python3 recovery_tool.py --verbose restore-path zero --pkg-scope system
python3 recovery_tool.py --verbose restore-path zero --pkg-scope thirdparty
```

### 5) `restore-app`

Restores a snapshot created by `backup-app`.

Usage:

```bash
python3 recovery_tool.py restore-app <snapshot> [--package PKG] [--user USER ...] [--auth-pkg PKG ...] [--with-account-db|--no-account-db] [--force-bootloop]
```

Options:

- `snapshot` - required snapshot name
- `--package PKG` - override package from `app_snapshot.json`
- `--user USER` - repeatable, restore to selected users
- `--auth-pkg PKG` - repeatable, override auth packages
- `--with-account-db` - force account DB restore
- `--no-account-db` - skip account DB restore
- `--force-bootloop` - proceed even if the device is at a non-zero RescueParty mitigation-count. Not recommended. See [Resilience & Recovery](#resilience--recovery).

Examples:

```bash
python3 recovery_tool.py --verbose restore-app netto_fixed
python3 recovery_tool.py restore-app netto_fixed --package com.valuephone.vpnetto --user 0
python3 recovery_tool.py restore-app netto_fixed --user 0 --user 10
python3 recovery_tool.py restore-app netto_fixed --auth-pkg com.example.auth
python3 recovery_tool.py restore-app netto_fixed --no-account-db
python3 recovery_tool.py restore-app netto_fixed --with-account-db
```

### 6) `restore-thirdparty`

Restores a snapshot created by `backup-thirdparty`.

Before starting, the command checks the device for a `/data/local/tmp/snaptool-restore.state` marker left by a previous aborted run. If a marker exists and references a *different* snapshot, the command refuses to start (exit code 3) and prints the operator's three recovery options. See [Resilience & Recovery](#resilience--recovery) below.

Usage:

```bash
python3 recovery_tool.py restore-thirdparty <snapshot> [--user USER ...] [--auth-pkg PKG ...] [--force-clean] [--force-bootloop]
```

Options:

- `snapshot` - required snapshot name
- `--user USER` - repeatable, limit restore to selected users
- `--auth-pkg PKG` - repeatable, override auth packages for restore
- `--force-clean` - ignore any existing restore-state marker on the device and proceed anyway (risky — may compound damage from a previously half-restored device)
- `--force-bootloop` - proceed even if the device is at a non-zero RescueParty mitigation-count. Not recommended. See [Resilience & Recovery](#resilience--recovery).

Examples:

```bash
python3 recovery_tool.py --verbose restore-thirdparty zero3party
python3 recovery_tool.py restore-thirdparty zero3party --user 0
python3 recovery_tool.py restore-thirdparty zero3party --user 0 --user 10
python3 recovery_tool.py restore-thirdparty zero3party --auth-pkg com.example.auth
python3 recovery_tool.py restore-thirdparty zero3party --force-clean
```

### 7) `recover-thirdparty`

Reads the device's restore-state marker and re-runs the snapshot it references (with `--force-clean`). Used when the previous `restore-thirdparty` aborted partway through — re-running the same snapshot converges the device to the snapshot's intended state regardless of how far the prior attempt got.

If no marker is present, the command does nothing and returns 0. If the marker references a snapshot whose archive is missing on the host, it errors out so the operator can restore the archive (or run `clear-restore-state` if the device is known to be fine).

Usage:

```bash
python3 recovery_tool.py recover-thirdparty
```

Examples:

```bash
python3 recovery_tool.py --verbose recover-thirdparty
python3 recovery_tool.py --serial <device_serial> recover-thirdparty
```

### 8) `clear-restore-state`

Deletes the device-side restore-state marker. Use this only after you have verified by hand that the device is in a healthy state — otherwise the next `restore-thirdparty` will run on top of a corrupt user space.

Usage:

```bash
python3 recovery_tool.py clear-restore-state
```

### 9) `clear-bootloop-state`

Wipes Android's RescueParty / PackageWatchdog escalation state on the device and reboots. Use when a previous restore (or anything else) tripped the boot-loop detector and the device is now sitting at `mitigation-count > 0`. From that point on, `restore-path` / `restore-app` / `restore-thirdparty` will refuse to start (exit code 4) until the escalation is cleared.

What it does:

1. Prints the current state for the audit trail (`package-watchdog.xml`, `mitigation_count.txt`, `crashrecovery-events.txt`, and the `crashrecovery.rescue_boot_*` runtime properties).
2. Requires a typed `YES` confirmation unless `--yes` is passed.
3. Deletes `/metadata/watchdog/mitigation_count.txt` (the durable file on the `/metadata` partition that survives `/data` wipes), `/data/system/package-watchdog.xml`, and `/data/system/crashrecovery-events.txt`.
4. Resets `crashrecovery.rescue_boot_count` and `crashrecovery.rescue_boot_start` to `0`.
5. Reboots the device, unless `--no-reboot` is set.

The reboot is what makes the cleared state stick — `system_server` holds the file contents in memory and will rewrite both `mitigation_count.txt` and `package-watchdog.xml` on the next observer event. Reboot before that happens.

Usage:

```bash
python3 recovery_tool.py clear-bootloop-state [--yes] [--no-reboot]
```

Options:

- `--yes` - skip the interactive `YES` confirmation
- `--no-reboot` - skip the post-wipe reboot (advanced — the cleared state may not stick until `system_server` is restarted, so reboot manually ASAP)

Examples:

```bash
python3 recovery_tool.py clear-bootloop-state
python3 recovery_tool.py --verbose clear-bootloop-state --yes
python3 recovery_tool.py clear-bootloop-state --yes --no-reboot
```

### 10) `pairip-fix`

Installs the bundled `assets/AlterInstaller-2.3-release.zip` Magisk module on the device, writes `/data/local/tmp/AlterInstaller.json` (replacing any existing file), and reboots the device.

The JSON written to the device is:

```json
{
    "de.dm.meindm.android": {
        "installer": "com.android.vending",
        "updateOwner": "com.android.vending"
    },
    "com.kaufland.Kaufland": {
        "installer": "com.android.vending",
        "updateOwner": "com.android.vending"
    }
}
```

Requirements (checked at runtime):

- `assets/AlterInstaller-2.3-release.zip` exists on the host
- An ADB device is connected and authorized
- The device is rooted (`su -c id` returns uid=0)
- The `magisk` binary is available on the device

Usage:

```bash
python3 recovery_tool.py pairip-fix
```

Examples:

```bash
python3 recovery_tool.py pairip-fix
python3 recovery_tool.py --verbose pairip-fix
python3 recovery_tool.py --serial <device_serial> pairip-fix
```

On success, the device reboots and the tool prints:

```
Pairip has been fixed successfully
```

## Resilience & Recovery

Backup and restore are long sequences of ADB commands. When the host↔device link blips mid-stream — bad USB cable, adbd restart, version-mismatch server bounce — naive tooling silently misses commands and continues, leaving the device with apps half-extracted, framework stopped, or keystore mid-swap. Stacking another restore on top of that state is what bricks devices. Separately, every restore briefly stops zygote to swap system DBs, and Android's PackageWatchdog counts that as a SYSTEM_RESTART event — back-to-back restores can trip the boot-loop detector, which escalates through `WARM_REBOOT` → settings reset → `FACTORY_RESET`.

This tool defends against both in four layers:

### Layer 1: Auto-retry on transient transport drops

Every adb call (critical and best-effort) is wrapped in a transport-drop retry loop. When the local `adb` binary reports `adb: device offline`, `adb: no devices/emulators found`, `adb: closed`, `error: protocol fault`, version-mismatch server kill, or similar, the tool:

1. Logs `[transport-retry] transport drop detected during phase '<phase>' (attempt N/3); waiting for device and retrying...`
2. Runs `adb wait-for-device` with a 30s bounded timeout.
3. Sends an `adb shell echo SNAPTOOL_PROBE_OK` probe to confirm the shell can actually execute (the probe does not depend on framework boot — works during the framework-stopped window).
4. Sleeps 1–2s (progressive backoff) and retries the same command.

Up to 2 retries (3 total attempts). Short USB blips are absorbed silently and the restore continues uninterrupted.

### Layer 2: Abort on persistent failure

If 3 attempts all fail, or if a critical adb command returns non-zero from the device shell, the tool aborts with:

```
restore-failed: <phase>: <reason> (lost adb connection | command failed, rc=<n>): <stderr excerpt>
backup-failed: ...
```

Exit code is `2`. The framework-stopped and keystore-stopped windows have `try/finally` safety nets that always attempt to restart `zygote` / `keystore2` / `credstore` before the abort propagates, so a mid-restore failure does not leave services dead.

### Layer 3: Device-side marker + recovery (restore-thirdparty only)

`restore-thirdparty` writes `/data/local/tmp/snaptool-restore.state` once the tar push succeeds and updates it as each major phase completes (media extracted, apps extracted, keystore swapped, accountmanager swapped, restorecon done, framework restarted, media owner fixed). On clean exit the marker is deleted; on abort it remains.

Every subsequent `restore-thirdparty` checks the marker:

- **No marker** → proceed normally.
- **Marker for the SAME snapshot** → treat as a recovery, log a warning, continue.
- **Marker for a DIFFERENT snapshot** → refuse with exit code 3 and the three options:
  1. `recover-thirdparty` — re-run the marked snapshot to completion (recommended).
  2. `restore-thirdparty --force-clean <snapshot>` — ignore marker and proceed anyway (risky).
  3. `clear-restore-state` — just delete the marker (only if you have verified the device is healthy).

The marker payload also includes a `transport_retries` counter (cumulative across the restore) so flaky devices are visible during recovery.

### Layer 4: Boot-loop detector defenses (all restore-* commands)

Each restore briefly stops `zygote` to swap system databases (keystore, AccountManager, permission state). Android's init writes a `SYSTEM_RESTART` dropbox entry every time, and PackageWatchdog's `BootThreshold` counts those against a hardcoded threshold of 5 events in 10 minutes — when tripped, it escalates through `ALL_DEVICE_CONFIG_RESET` → `WARM_REBOOT` → `RESET_SETTINGS_*` → `FACTORY_RESET`. A secondary trigger condition (`performedMitigationsDuringWindow() && count > 1`) means once a device is already at `mitigation-count > 0`, just **2** SYSTEM_RESTART events trip the detector instead of 5.

This tool defends against accumulating SYSTEM_RESTART events two ways:

1. **Atomic counter reset around the zygote stop.** `crashrecovery.rescue_boot_count` and `crashrecovery.rescue_boot_start` are reset to `0` in the *same* root shell script that issues the `ctl.stop zygote`. The new `system_server`'s `noteBoot()` then sees `start=0` → `now - 0 > 10-min trigger window` → takes the "reset" branch (`setCount(1); setStart(now); return false`) and never reaches the count-incrementing branch that could trip. A second reset runs after framework-ready as defense in depth, so the device's state stays clean between back-to-back restores too.

2. **Pre-flight refusal at `mitigation-count >= 1`.** Every restore-* command reads `rescue-party-observer`'s mitigation-count from `/data/system/package-watchdog.xml`. If non-zero, the command refuses to start with exit code `4` and points the operator at `clear-bootloop-state`. At any non-zero level the secondary trigger condition is live and a single failure of the atomic reset would escalate the device — easier to clear and retry than to risk it. Override with `--force-bootloop` if you really mean it.

When the detector has already been tripped, recover with [`clear-bootloop-state`](#9-clear-bootloop-state): it wipes `/metadata/watchdog/mitigation_count.txt` (the durable copy on the `/metadata` partition that survives `/data` wipes), `/data/system/package-watchdog.xml`, and `/data/system/crashrecovery-events.txt`, then reboots. After the reboot the device boots as if it had never tripped, and the next restore proceeds normally.

### Exit codes

- `0` — success.
- `1` — usage/configuration error (missing archive, bad arguments, no devices, etc.).
- `2` — restore/backup aborted (transport drop survived retries, or critical command failed). Marker remains on device for `restore-thirdparty`.
- `3` — preflight refusal (`restore-thirdparty` saw a marker for a different snapshot and `--force-clean` was not set).
- `4` — boot-loop preflight refusal (device is at `mitigation-count >= 1` and `--force-bootloop` was not set). Run `clear-bootloop-state` and retry.

### Recommended queue-runner pattern

For scripts that restore many snapshots back-to-back:

```bash
for snap in snap-1 snap-2 snap-3 snap-4 snap-5; do
  python3 recovery_tool.py restore-thirdparty "$snap"
  rc=$?
  if [ $rc -eq 2 ]; then
    # Restore aborted partway. Re-run the marked snapshot to completion
    # before moving on to the next one in the queue.
    python3 recovery_tool.py recover-thirdparty || exit 1
  elif [ $rc -eq 3 ]; then
    # Device was already half-restored from a prior run. Recover first,
    # then retry the current snapshot.
    python3 recovery_tool.py recover-thirdparty || exit 1
    python3 recovery_tool.py restore-thirdparty "$snap" || exit 1
  elif [ $rc -eq 4 ]; then
    # Device's boot-loop detector has already escalated. Wipe the
    # escalation state (reboots the device), then retry the snapshot.
    python3 recovery_tool.py clear-bootloop-state --yes || exit 1
    python3 recovery_tool.py restore-thirdparty "$snap" || exit 1
  elif [ $rc -ne 0 ]; then
    exit $rc
  fi
done
```

The same loop without these checks is what bricks devices when one restore in the middle of the queue aborts.

## Snapshot Layout

Snapshots are created under:

- `snapshots/<snapshot-name>/`

Common files:

- `data.tar.zst` - compressed archive
- `logs/` - command logs (when generated)

Metadata files by snapshot type:

- `backup-app` snapshots: `app_snapshot.json`
- `backup-thirdparty` snapshots: `apps_snapshot.json`
- full backup snapshots may include: `snapshot_state.json`

## Common Workflows

### A) Full device `/data` backup and restore

```bash
python3 recovery_tool.py --verbose backup --name zero
python3 recovery_tool.py --verbose restore-path zero --pkg-scope all
```

### B) Single app backup and restore

```bash
python3 recovery_tool.py --verbose backup-app com.example.app --name app_snap
python3 recovery_tool.py --verbose restore-app app_snap
```

### C) All users' third-party apps backup and restore

```bash
python3 recovery_tool.py --verbose backup-thirdparty --name tp_all
python3 recovery_tool.py --verbose restore-thirdparty tp_all
```

### D) Recovering from an aborted `restore-thirdparty`

```bash
# Previous run aborted with exit 2 (or a queued run exited 3 on refusal).
# Re-run the marked snapshot to completion:
python3 recovery_tool.py --verbose recover-thirdparty

# If you know the device is fine and just want to dismiss the marker:
python3 recovery_tool.py clear-restore-state
```

See [Resilience & Recovery](#resilience--recovery) for details and the queue-runner pattern.

### E) Recovering from a boot-loop escalation

```bash
# A restore refused to start with exit 4, or the device just booted with
# sys.boot.reason=reboot,rescueparty. Wipe the escalation state, reboot,
# and the next restore proceeds normally.
python3 recovery_tool.py --verbose clear-bootloop-state

# Non-interactive variant for queue runners:
python3 recovery_tool.py clear-bootloop-state --yes

# Then retry the restore:
python3 recovery_tool.py --verbose restore-thirdparty <snapshot>
```

## Help Commands

```bash
python3 recovery_tool.py -h
python3 recovery_tool.py backup -h
python3 recovery_tool.py backup-thirdparty -h
python3 recovery_tool.py backup-app -h
python3 recovery_tool.py restore-path -h
python3 recovery_tool.py restore-app -h
python3 recovery_tool.py restore-thirdparty -h
python3 recovery_tool.py recover-thirdparty -h
python3 recovery_tool.py clear-restore-state -h
python3 recovery_tool.py clear-bootloop-state -h
python3 recovery_tool.py pairip-fix -h
```

## Troubleshooting Quick Checks

ADB/device:

```bash
adb devices
adb shell su -c id
```

Crash logs:

```bash
adb logcat -b crash -d | tail -n 200
```

Tool logs:

```bash
ls -la snapshots/<snapshot>/logs/
tail -n 200 snapshots/<snapshot>/logs/*.log
```

### Common error messages

- `restore-failed: <phase>: <reason> (lost adb connection, rc=1): adb: ...`
  Transport drop survived 3 retries. The device is unreachable. Re-seat USB, run `adb devices`, then `recover-thirdparty`.

- `restore-failed: <phase>: <reason> (command failed, rc=1): ...`
  The device shell returned non-zero from a critical command. Inspect the stderr excerpt and `snapshots/<name>/logs/restore-thirdparty.log`.

- `Refusing to restore: device is in a half-restored state from a previous run.`
  A `restore-thirdparty` marker is on the device from a prior aborted run. Run `recover-thirdparty` (recommended), or `restore-thirdparty --force-clean <snap>`, or `clear-restore-state` if the device is verified healthy.

- `Refusing restore-...: device is at RescueParty mitigation-count=<N>.`
  PackageWatchdog's boot-loop detector has already escalated this device. Run `clear-bootloop-state` (reboots the device), then retry the restore. To override against the warning, pass `--force-bootloop` — not recommended.

- `[transport-retry] transport drop detected during phase '...' (attempt N/3); waiting for device and retrying...`
  Normal — the auto-retry loop absorbed a short USB blip. Counts are accumulated in the restore-state marker's `transport_retries` field; a high count over a single restore means the cable or device is flaky.

### Inspecting the restore-state marker

The device-side marker, if present:

```bash
adb shell su -c 'cat /data/local/tmp/snaptool-restore.state'
```

Output is a single-line JSON with `snapshot`, `cmd`, `started_at`, `last_phase`, `phase_count`, and `transport_retries`. To clear it manually after verifying the device is healthy:

```bash
python3 recovery_tool.py clear-restore-state
```
