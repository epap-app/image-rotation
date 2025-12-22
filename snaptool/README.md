# Android Snaptool

A host-side Python tool (`recovery_tool.py`) to **backup** and **restore** Android **`/data`** using **ADB + root (`su`)**.

- **backup** → creates `snapshots/<name>/data.tar.zst`
- **restore-path (recommended)** → restores app + media state in a staged way designed to preserve app login sessions

> Requires a rooted **Pixel 7** device (working `su`). Tested on Pixel 7 **Android 13**.

---

## Host dependencies

You need these tools installed on your computer:

- Python **3.8+** (3.10+ recommended)
- `adb` (Android platform-tools)
- `tar`
- `zstd`

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y adb zstd tar python3 python3-pip
```

### Verify the tools

```bash
python3 --version
adb version
zstd --version
tar --version
```

---

## Phone setup (Pixel 7)

### Enable USB debugging

1. **Settings → About phone** → tap **Build number** 7 times
2. **Settings → System → Developer options** → enable **USB debugging**
3. Plug in the USB cable
4. Unlock the phone and accept the **Allow USB debugging** prompt

### Verify ADB connection

```bash
adb devices
```

Expected output:

```text
<serial>    device
```

If you see `unauthorized`:

```bash
adb kill-server
adb start-server
adb devices
```

Then unlock the phone and accept the prompt again.

### Verify root (`su`)

```bash
adb shell su -c id
```

Expected output includes:

```text
uid=0(root)
```

---

## How to run the tool

Run commands from the project folder (where `recovery_tool.py` exists).

### Show help

```bash
python3 recovery_tool.py -h
```

### Show help for specific commands

```bash
python3 recovery_tool.py backup -h
python3 recovery_tool.py restore-path -h
```

---

## 1) Backup (create a snapshot)

Create a snapshot:

```bash
python3 recovery_tool.py backup --name initial
```

This creates:

- `snapshots/initial/data.tar.zst`

You can choose any name:

```bash
python3 recovery_tool.py backup --name before_update
python3 recovery_tool.py backup --name snap-20251220-1400
```

---

## 2) Restore: `restore-path`

This is the safer restore mode and usually preserves login sessions.

### Usage

```bash
python3 recovery_tool.py restore-path <snapshot-name> --pkg-scope <scope>
```

### `--pkg-scope` options

- `apps` (**default**): installed packages excluding overlays
- `thirdparty`: third-party apps only
- `system`: system apps only
- `all`: all installed packages (includes overlays)

### Examples

```bash
python3 recovery_tool.py restore-path initial --pkg-scope apps # Will restore all of the apps (system + thirdparty)
python3 recovery_tool.py restore-path initial --pkg-scope thirdparty # Will restore only thirdparty apps (non-system apps)
python3 recovery_tool.py restore-path initial --pkg-scope system # Will restore only system apps (third party apps not included)
```

### Optional reboot after `restore-path`

Some apps behave better after a reboot:

```bash
adb reboot
```

---

## Where snapshots are stored

Snapshots are stored under:

- `snapshots/<snapshot-name>/`

A snapshot contains:

- `data.tar.zst` (your compressed `/data` backup)
- optional `logs/` folder (if your tool writes logs)

---

## Logs: how to view and debug

### A) Tool logs (if present)

Many versions of this tool write logs under:

- `snapshots/<snapshot-name>/logs/`

Examples:

```bash
ls -la snapshots/initial/logs/
cat snapshots/initial/logs/restore-path.log
tail -n 200 snapshots/initial/logs/restore-path.log
```

If your project does not create `logs/` yet, the console output is still the primary log.

### B) ADB logs (device logs)

These help when an app crashes or MediaProvider/Photos misbehaves.

Crash buffer (best for app crashes):

```bash
adb logcat -b crash -d | tail -n 200
```

Filter for Photos / MediaProvider:

```bash
adb logcat -d | grep -iE "photos|MediaProvider|providers\.media|media_store" | tail -n 200
```

Save logs to files (recommended when reporting issues):

```bash
adb logcat -b crash -d > crash.log
adb logcat -d > full.log
```

---

## Common troubleshooting

### `adb: no devices/emulators found`

Check:

```bash
adb devices
```

Then:

- reconnect the cable
- ensure **USB debugging** is enabled

### Device shows `unauthorized`

```bash
adb kill-server
adb start-server
adb devices
```

Then accept the prompt on the phone.

### Root not working (`su` fails)

```bash
adb shell su -c id
```

If this fails, the tool cannot work.

---

## Recommended workflow

1. Install host dependencies (`adb`, `zstd`, `tar`, `python3`)
2. Connect phone and verify:

   ```bash
   adb devices
   adb shell su -c id
   ```

3. Create a snapshot:

   ```bash
   python3 recovery_tool.py backup --name initial
   ```

4. Restore when needed:

   ```bash
   python3 recovery_tool.py restore-path initial --pkg-scope apps
   ```

5. Optionally reboot once:

   ```bash
   adb reboot
   ```
