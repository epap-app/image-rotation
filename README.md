# Android Snaptool

Android Snaptool is a host-side Python CLI (`recovery_tool.py`) for rooted Android backup/restore workflows.

Supported commands:

- `backup` - full `/data` snapshot (with built-in excludes)
- `backup-thirdparty` - backup third-party app state for all users (or selected users)
- `backup-app` - fast snapshot for one app
- `restore-path` - restore from full snapshot with package scope filtering
- `restore-app` - restore one app snapshot
- `restore-thirdparty` - restore a `backup-thirdparty` snapshot
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
python3 recovery_tool.py restore-path <snapshot> [--pkg-scope {apps,all,system,thirdparty}]
```

Scope values:

- `apps` (default): installed packages excluding overlays
- `all`: all installed packages
- `system`: system packages only
- `thirdparty`: non-system packages only

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
python3 recovery_tool.py restore-app <snapshot> [--package PKG] [--user USER ...] [--auth-pkg PKG ...] [--with-account-db|--no-account-db]
```

Options:

- `snapshot` - required snapshot name
- `--package PKG` - override package from `app_snapshot.json`
- `--user USER` - repeatable, restore to selected users
- `--auth-pkg PKG` - repeatable, override auth packages
- `--with-account-db` - force account DB restore
- `--no-account-db` - skip account DB restore

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

Usage:

```bash
python3 recovery_tool.py restore-thirdparty <snapshot> [--user USER ...] [--auth-pkg PKG ...]
```

Options:

- `snapshot` - required snapshot name
- `--user USER` - repeatable, limit restore to selected users
- `--auth-pkg PKG` - repeatable, override auth packages for restore

Examples:

```bash
python3 recovery_tool.py --verbose restore-thirdparty zero3party
python3 recovery_tool.py restore-thirdparty zero3party --user 0
python3 recovery_tool.py restore-thirdparty zero3party --user 0 --user 10
python3 recovery_tool.py restore-thirdparty zero3party --auth-pkg com.example.auth
```

### 7) `pairip-fix`

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

## Help Commands

```bash
python3 recovery_tool.py -h
python3 recovery_tool.py backup -h
python3 recovery_tool.py backup-thirdparty -h
python3 recovery_tool.py backup-app -h
python3 recovery_tool.py restore-path -h
python3 recovery_tool.py restore-app -h
python3 recovery_tool.py restore-thirdparty -h
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
