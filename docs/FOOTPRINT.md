# What ClipBridge puts on your devices

Every file, setting and dependency ClipBridge adds, and how to remove it. Anything new must be
listed here (see `CLAUDE.md`).

## Laptop (Windows)

| Item | Where | Removed by |
|---|---|---|
| Source code + Python dependencies (`websockets`, `zeroconf`, `pywin32`, `cryptography`, `Pillow`, dev: `pytest`, `pytest-asyncio`) | this repo folder; dependencies only in `windows\.venv` | deleting the repo folder |
| Scheduled task `ClipBridge` (starts the server at logon / unlock) | Task Scheduler | `clipbridge uninstall` |
| Firewall rule `ClipBridge` (inbound TCP 8765, Private profile, local subnet) | Windows Defender Firewall | `clipbridge uninstall` (one UAC prompt) |
| Context-menu verb "Send to phone" | `HKCU\Software\Classes\*\shell\ClipBridge` | `clipbridge uninstall` |
| SendTo shortcut "Phone (ClipBridge)" | `shell:sendto` (`%APPDATA%\Microsoft\Windows\SendTo`) | `clipbridge uninstall` |
| Device registry (paired devices and their DPAPI-encrypted keys) | `%APPDATA%\ClipBridge` | `clipbridge uninstall --purge` |
| Logs + unfinished transfers | `%LOCALAPPDATA%\ClipBridge` | `clipbridge uninstall --purge` |
| Android build cache (only if you build the APK), including the test-only library `kotlinx-coroutines-test` (unit tests only, never in the APK) | `%USERPROFILE%\.gradle` (shared with other Android projects) | delete manually if nothing else uses it; drop the library by removing its `testImplementation` line in `android/app/build.gradle.kts` together with the tests that use it |
| **Your data**: received files, received clipboard images | `Downloads\ClipShare`, `Pictures\ClipBridge` | never deleted automatically |

Not installed by ClipBridge (pre-existing tools it uses): Python 3.10, Android Studio + SDK, adb.

### Full removal
Run this from the repo, using the venv's Python:

```
windows\.venv\Scripts\python.exe -m clipbridge uninstall --purge
```

Then delete the repo folder. Keep or delete `Downloads\ClipShare` and `Pictures\ClipBridge` yourself.

## Phone (Android)

| Item | Removed by |
|---|---|
| App `com.clipbridge`, its settings, key and unfinished transfers | uninstalling the app |
| adb grants from setup (`READ_LOGS`, overlay app-op, battery-optimization whitelist) | uninstalling the app |
| Debug builds only: the device-test share hook `E2eShareActivity` and its staging folder `Android/data/com.clipbridge/files/e2e` (emptied by `windows/e2e/device.py` after every run) | uninstalling the app; release builds never contain it |
| **Your data**: received files in `Downloads/ClipShare` | never deleted automatically |
