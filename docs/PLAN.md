# ClipBridge — Windows ⇄ Android local clipboard sync

## Context
Empty project folder. Goal: zero-cloud, LAN-only, two-way text clipboard sync between Windows laptop and Android phone, surviving reboots/sleep/network changes. Source = Gemini discussion summary; adapted where it was wrong or fragile.

Environment found: Python 3.10.11 (websockets 16 installed), Android SDK (platforms 35, 37) + Android Studio JBR (JDK 21 for Gradle; system Java 26 is too new for AGP), adb works. Phone connected: **vivo V2502, Android 16 (API 36)** — aggressive OEM battery killer, newest clipboard/logcat rules.

## Corrections to the Gemini plan (why the design differs)
| Gemini | Problem | This plan |
|---|---|---|
| 32-bit token in WS header, plaintext | Brute-forceable; clipboard (passwords) readable by anyone on campus Wi-Fi; phone would trust *any* mDNS advertiser and leak clipboard to it | 256-bit PSK, **mutual** HMAC challenge-response, per-session HKDF keys, AES-256-GCM frames with counter nonces (replay-proof). ~120 lines/side, no TLS/cert mgmt |
| `dataSync` FGS type | Android 15+: 6h/day cap, and cannot start from BOOT_COMPLETED | `connectedDevice` type |
| LOCKED_BOOT_COMPLETED + directBootAware | Key lives in credential storage, unreadable before unlock; clipboard useless before unlock anyway | BOOT_COMPLETED + MY_PACKAGE_REPLACED only |
| 1×1 overlay window to steal focus | Overlay windows don't reliably count as "focused app" | Transparent no-UI Activity (proven KDE Connect technique); SYSTEM_ALERT_WINDOW is what permits the background activity start |
| logcat "ClipboardService copy event" | Copies aren't logged. What *is* logged: "Denying clipboard access to <our pkg>" when our registered clip listener is denied. Android 13+ also gates logcat behind a consent dialog | Use the denial line as trigger; **verify on this vivo/Android 16 first (Phase 0)**; manual fallbacks always present |
| "On unlock" trigger relaunch | Spawns duplicate daemons | Named mutex single-instance + Task Scheduler `IgnoreNew`; unlock trigger then only revives a dead daemon |
| zeroconf registered once | IP changes after sleep/network switch → stale record | Re-register on IP change + on resume (WM_POWERBROADCAST) |
| hash cache of received items | Unbounded / blocks legit re-copies | One `last_synced_hash` state per side (see Sync rules) |
| — | Password managers mark secrets | Windows: skip clipboard carrying `ExcludeClipboardContentFromMonitorProcessing` |
| — | mDNS blocked on many campus/hotel networks (client isolation, multicast filtering) | Cache last good host:port; manual host override in app |

## Architecture
Laptop = WebSocket server (`0.0.0.0:8765`), advertises `_clipbridge._tcp.local.`. Phone(s) = client(s). Server supports N clients and relays (tablet later = free).

### Two layers: Link vs Features (the expansion point)
The project is really a **general secure device link**; clipboard is just the first feature on it.
- **Link layer** (written once, never touched by features): discovery, connect/reconnect, handshake, encryption, liveness, peer registry, and general services any feature can use: delivery receipts and file transfer. After handshake the link is symmetric — features don't know or care which side is server.
- **Feature layer**: each feature = one module implementing a tiny interface, registered by name:
  ```
  Feature { name: str
            on_peer_connected(peer) / on_peer_disconnected(peer)
            on_message(peer, header: dict, body: bytes)
            on_file_offer(peer, offer) -> destination | None     (only features that take files)
            on_file_received(peer, offer, path) }
  Link gives each feature: send(peer|all, header, body=b"")
                           send_file(peer, feature, file, meta) -> delivered | failed
  ```
  Link routes by `header.ch` (= feature name). Unknown `ch` → ignored + debug log, so either side can be upgraded first.
- Adding "remote shenanigans" (notifications mirror, media keys, run command, battery status, etc.) = new feature file on each side + register line. Zero edits to link/crypto/transport.
- Emojis: already covered — text is Unicode end to end (UTF-8 on wire, `CF_UNICODETEXT` UTF-16 on Windows, surrogate pairs preserved). Tested explicitly.
- Images: clipboard feature gets `kind:"image"` with PNG bytes in `body` (no base64 bloat). Windows: `CF_DIB`/`PNG` format ⇄ PNG via Pillow; Android: FileProvider URI. Not built in v1, but envelope already carries binary.

### Protocol (single source of truth: `protocol/PROTOCOL.md` + `protocol/test_vectors.json`)
1. Client connects `ws://host:8765`, subprotocol `clipbridge.v1`.
2. Server → `{"type":"challenge","v":1,"sn":b64(16B)}` (plaintext).
3. Client → `{"type":"auth","cn":b64(16B),"device":"<id>","mac":b64(HMAC-SHA256(PSK,"c"‖sn‖cn‖device))}`.
4. Server verifies (constant-time) → `{"type":"auth_ok","mac":b64(HMAC(PSK,"s"‖sn‖cn))}`; client verifies → server proven genuine. Failure = close 4401, no detail.
5. Keys: `HKDF-SHA256(PSK, salt=sn‖cn, info="c2s"|"s2c")` → 2×32B.
6. Every later frame: binary `nonce(12B = u64 counter LE ‖ 4×0) ‖ AES-GCM(ct)`. Receiver requires counter strictly increasing; any decrypt/counter failure closes connection.
7. Decrypted plaintext = envelope: `u32 header_len (BE) ‖ header JSON (UTF-8) ‖ body bytes`.
   Header: `{"ch":"clipboard","type":"clip","kind":"text","data":"…","ts":<ms>}` (text inline in header; binary in body). Unknown `ch`/`type`/`kind` ignored (forward compat). Max frame 16 MiB (room for images); oversize skipped + logged. Anything bigger goes through file transfer (item 10).
8. Liveness: server ping 10s/timeout 10s; client (OkHttp) ping 15s. Client backoff capped at 10s so PC resume reconnects fast.
9. Delivery receipts (link service, any feature): sender adds `"mid"` to a header; receiver's link replies `{"ch":"link","type":"ack","mid":…}` once the feature handled it. No receipt within 10s or link lost = not delivered. Used by explicit image sends (screenshot tile, share target) to confirm the PC really got the image.
10. File transfer (link service, any feature; PROTOCOL.md 6.2): offer / accept-with-ranges / done on the control connection; bytes on separate **data connections** (subprotocol `clipbridge.data.v1`, same handshake), always dialed by the phone, so a 2 GB file never delays a clip. Resume = receiver reports how many bytes of each range it already has (tus idea); each range ends with its SHA-256 and the receiver renames `.part` to the final name only after every hash matches (Syncthing idea). One data connection carries many files back to back (LocalSend session idea).

### File transfer design
- **Queue**: sender keeps an in-memory FIFO of files (id → path/URI). Unfinished items are re-offered with the same `id` after a reconnect. Up to `STREAMS` data connections at once; separate files run in parallel and one large file is split into up to `STREAMS` ranges written at their offsets into a preallocated `.part` (aria2/IDM segmented download). `STREAMS` starts at 1 and is raised only if the benchmark shows a gain (one TCP stream usually fills a LAN).
- **Speed**, in order of value: phone holds a `WIFI_MODE_FULL_LOW_LATENCY` Wi-Fi lock + partial wakelock while transferring (Wi-Fi power-save is the main throughput killer); hashing happens while streaming, never as a pre-pass; disk reads run ahead on a thread (2 × 4 MiB chunks) so the socket never waits on disk; 4 MiB frames and large socket buffers. No compression (media is already compressed). Transfer logs print MB/s.
- **Partial files**: Windows keeps `<name>.part` plus a small state file under `%LOCALAPPDATA%\ClipBridge\partial\` and drops entries older than 24 h; Android keeps a MediaStore row with `IS_PENDING=1` and its range state in app-private storage.

### Files feature (`ch = "files"`, uses only file transfer)
- Scope: one phone. No device picker; PC sends to the connected phone.
- PC receives into `%USERPROFILE%\Downloads\ClipShare\` (name clash → ` (1)`, ` (2)`, …); tray balloon "Received <name>", click opens Explorer with the file selected.
- PC sends from Explorer: classic context menu verb "Send to phone" (`HKCU\Software\Classes\*\shell\ClipBridge`, `MultiSelectModel=Player`) and a `shell:sendto` shortcut. Both run `pythonw -m clipbridge send <file>`, which hands the path to the running server over named pipe `\\.\pipe\ClipBridge` and exits; each Explorer-spawned process adds one file to the queue. Files only, not folders.
- Phone receives automatically into `Downloads/ClipShare` via MediaStore; progress notification, then tap to open plus a Share action. Runs inside `BridgeService`, so screen lock does not stop it.
- Phone sends from the share sheet: `ShareTargetActivity` takes `*/*` and `ACTION_SEND_MULTIPLE`; images keep going through the clipboard image path, every other file goes through `send_file`. Same "Sending…" toast, then the delivered/failed result.

`test_vectors.json` (fixed PSK/nonces → expected MACs, keys, ciphertexts) is consumed by **both** Python and Kotlin unit tests → guarantees interop.

### Sync rules (identical both sides, in `SyncEngine`)
- State: `last_hash` (SHA-256 of current shared text), `pending` (single slot: latest unsent local clip).
- Local change: hash == last_hash → drop (echo of what we just wrote, or no-op). Else last_hash = hash; send if connected, else `pending = clip`.
- Remote clip: hash == last_hash → drop. Else last_hash = hash, write local clipboard (server also relays to other clients).
- On (re)connect: flush `pending` only. No full-state push (would clobber the other side).
- Conflicts: last writer wins. Pure logic, no I/O → fully unit-tested.

## Windows side — `windows/` (Python ≥3.10, package `clipbridge`)
Deps: `websockets`, `zeroconf`, `pywin32`, `cryptography`. Dev: `pytest`, `pytest-asyncio`.
```
windows/pyproject.toml
windows/src/clipbridge/
  __main__.py     CLI: run | pair | send | install | uninstall [--purge] | status
  config.py       %APPDATA%\ClipBridge\config.json; PSK stored DPAPI-encrypted (win32crypt)
  protocol.py     message dataclasses, encode/decode, size limits
  crypto.py       handshake MACs, HKDF, SecureChannel(encrypt/decrypt, counters)
  sync.py         SyncEngine (pure)
  clipboard.py    ClipboardThread: message-only HWND, AddClipboardFormatListener,
                  WM_CLIPBOARDUPDATE → callback; set_text() via WM_APP post (all clipboard
                  I/O on that one thread); OpenClipboard retry w/ backoff; WM_POWERBROADCAST resume → callback
  discovery.py    zeroconf advertiser; re-registers on IP set change (30s check) or resume signal
  server.py       websockets server, handshake, client registry, broadcast/relay
  app.py          wiring, asyncio loop, thread→loop bridging (call_soon_threadsafe),
                  named mutex `Local\ClipBridge`, rotating file log %LOCALAPPDATA%\ClipBridge\logs
windows/tests/    test_crypto (vectors), test_sync, test_protocol, test_server (in-process handshake + echo/relay)
```
- `pair`: generate PSK if absent → push to phone via `adb shell am start -n <pkg>/.ui.PairActivity --es psk <b64> --es host <ip>`; also prints key for manual paste.
- `install`: register Task Scheduler task from XML via `schtasks /Create /XML`: triggers Logon + SessionUnlock (current user), InteractiveToken (must be user session — clipboard is per-desktop, SYSTEM/session 0 can't see it), `IgnoreNew`, restart every 1 min ×999, no time limit, run on battery. Action: `pythonw.exe -m clipbridge run`. Adds firewall inbound rule for TCP 8765 (Private+Public profiles, needs one UAC prompt), the "Send to phone" context-menu verb and the SendTo shortcut (both HKCU / per-user, no UAC). `uninstall` reverses all of them; `uninstall --purge` also deletes `%APPDATA%\ClipBridge` and `%LOCALAPPDATA%\ClipBridge`. Received files are never deleted. Everything ClipBridge leaves on either device is listed in `docs/FOOTPRINT.md`.
- Never exits on network errors; only fatal init errors exit non-zero (→ Task Scheduler restarts).

## Android side — `android/` (Kotlin, single `app` module, minSdk 29, target/compile 35)
Deps: OkHttp (WS + pings), kotlinx-coroutines, kotlinx-serialization, Compose (one screen). Crypto = platform `javax.crypto` (HMAC, AES/GCM); HKDF hand-written (~15 lines, covered by vectors).
Package: `com.clipbridge` (personal sideload).
```
app/src/main/java/com/clipbridge/
  ClipBridgeApp.kt
  data/Settings.kt            PSK, device id, last good host:port, manual host (SharedPreferences, app-private)
  protocol/Messages.kt        @Serializable messages
  crypto/Handshake.kt, crypto/SecureChannel.kt
  sync/SyncEngine.kt          same rules as Python
  net/Discovery.kt            NsdManager → Flow<Endpoint>; serialized resolves; MulticastLock not needed
  net/BridgeClient.kt         connect loop: endpoint order = manual > mDNS > cached; backoff 1→30s + jitter,
                              reset on success; NetworkCallback.onAvailable → immediate retry; exposes StateFlow<Status>
  clipboard/ClipboardWriter.kt     setPrimaryClip (background writes are allowed)
  clipboard/LogcatTrigger.kt       registers OnPrimaryClipChangedListener; runs `logcat -T 1 ClipboardService:E *:S`,
                                   matches "Denying clipboard access to com.clipbridge", debounced; restarts logcat if it dies
  clipboard/ClipReadActivity.kt    transparent, no animation; onWindowFocusChanged(true) → read clip → hand to service → finish()
  service/BridgeService.kt    FGS type connectedDevice, silent low-importance channel; owns client+engine+trigger
  service/BootReceiver.kt     BOOT_COMPLETED, MY_PACKAGE_REPLACED → startForegroundService
  ui/MainActivity.kt          status, host override, PSK paste, permission checklist (battery exemption, overlay, READ_LOGS, notifications) with fix buttons
  ui/PairActivity.kt          exported but `android:permission="android.permission.DUMP"` → only adb shell can invoke
  ui/SendClipboardTile.kt     Quick Settings tile → ClipReadActivity (manual fallback)
  ui/ShareTargetActivity.kt   ACTION_SEND text/plain → send (manual fallback)
app/src/test/                 crypto vectors, SyncEngine, protocol
```
Permissions: INTERNET, ACCESS_NETWORK_STATE, CHANGE_NETWORK_STATE (connectedDevice prerequisite), FOREGROUND_SERVICE(+_CONNECTED_DEVICE), POST_NOTIFICATIONS, RECEIVE_BOOT_COMPLETED, REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, SYSTEM_ALERT_WINDOW, READ_LOGS (adb-granted).
Setup script `android/setup.ps1`: build+install APK, `pm grant READ_LOGS`, `appops set SYSTEM_ALERT_WINDOW allow`, `dumpsys deviceidle whitelist +com.clipbridge`, then `clipbridge pair`. README lists vivo manual steps (Background power → allow high usage; Auto-start on; lock in recents).

Code layout follows the layers on both sides: `link/` (transport, crypto, discovery, peers, receipts, transfer) and one package per feature: `features/clipboard/` (SyncEngine, platform clipboard I/O), `features/files/` (destination, notifications, send entry points). Python: `link/` + `features/` subpackages under `clipbridge/`; Kotlin: `com.clipbridge.link.*` + `com.clipbridge.features.<name>.*`. File transfer splits pure range/resume logic (unit-tested) from the socket and disk I/O.

## Expected latency (same Wi-Fi)
| Path | Typical | Worst normal | Dominated by |
|---|---|---|---|
| PC copy → phone clipboard | 20–80 ms | ~300 ms | phone Wi-Fi power-save wake (DTIM beacon) |
| Phone copy → PC clipboard | 200–500 ms | ~800 ms | logcat line + launching transparent read-activity on vivo |
| Phone copy via QS tile/share | ~same as tap + 50 ms | — | user tap |
| Reconnect after Wi-Fi drop / PC wake | 1–3 s | ~10 s | DHCP + backoff cap |
Crypto/JSON cost <1 ms. Spike (Phase 0) measures the real phone→PC number.

## Keeping sessions small (usage limits)
- Repo carries its own context so fresh sessions start cheap: short `CLAUDE.md` (layout, commands, conventions, ≤40 lines) + `protocol/PROTOCOL.md`. No session needs this chat.
- Each build step below = **one fresh session** with a short brief ("implement step N per PLAN.md"), ending in passing tests. This plan copied to `docs/PLAN.md` in repo as step 1.
- Steps 2 and 3 are independent after step 1 (both work from spec + vectors) → can run in parallel sessions.
- No subagent fan-out; sessions read only the files their step touches.

## Build order
0. **Spike (de-risk, throwaway, on the vivo)**: minimal APK with FGS + listener + logcat reader + transparent activity. Confirm on Android 16: (a) denial line format, (b) logcat consent dialog behavior from a FGS and after reboot, (c) background activity start + clipboard read works. Outcome decides: auto-sync as designed / auto-sync needing "re-enable after reboot" notification tap / manual-only (tile+share). Report result before continuing.
1. Repo scaffold: `CLAUDE.md`, `docs/PLAN.md`, `protocol/PROTOCOL.md` + test vectors (generated by Python reference crypto).
2. Windows package + tests → runnable standalone (test with a Python test client).
3. Android app: crypto/protocol/sync + tests against vectors → client/service → clipboard trigger (per spike) → fallbacks → UI.
4. Persistence: Task Scheduler + firewall install; boot receiver; setup script.
5. End-to-end + soak.
6. File transfer spec: `PROTOCOL.md` 6.2, this plan, `CLAUDE.md`, `docs/FOOTPRINT.md`.
7. Windows: `link/transfer_core.py` (pure range/resume logic) + `link/transfer.py` (data-connection and disk I/O), `clipbridge.data.v1` in `link/server.py`, `features/files/` (destination, name clash, tray balloon); tests drive it with `tests/tclient.py` acting as the phone.
8. Windows shell: `clipbridge send` + named pipe, context-menu verb, SendTo shortcut, `uninstall --purge`.
9. Android: `link/Transfer.kt` (data connections over OkHttp, Wi-Fi lock), `features/files/`, share target for any file. Independent of 7–8 (works from the spec) → can run in parallel.
10. File transfer end to end on the vivo + speed benchmark (`STREAMS` 1 vs 3 against a raw-socket ceiling).

## Verification
- `pytest windows/tests` and `gradlew :app:testDebugUnitTest` — both pass identical vectors.
- E2E: copy on PC → appears on phone (<1s); copy in any phone app → appears on PC; repeat copies, no ping-pong (check logs show drops).
- Security: wrong PSK client rejected (4401); rogue server (wrong PSK) rejected by phone before any clip sent; tampered/replayed frame closes connection (Python test).
- Resilience: toggle phone Wi-Fi; PC sleep 10 min → resume; switch PC networks (IP change → re-announce); reboot phone (auto-start); reboot PC/log off-on (task starts, single instance); kill pythonw (restarts ≤1 min); `adb shell am kill` / force background 1h (vivo killer).
- Password manager copy on Windows not synced.
- Files: right-click a 1 GB file → appears in phone `Downloads/ClipShare`; share 5 photos + a PDF from the phone → land in `Downloads\ClipShare`; phone Wi-Fi off/on mid-transfer → resumes, not restarts; corrupted range → `ok:false` then one clean retry; clipboard keeps syncing during a transfer; `uninstall --purge` leaves nothing from `docs/FOOTPRINT.md` but received files.

## Out of scope v1 (designed to slot in)
Done since v1: image clips both ways, share target (text + images), "Send screenshot" QS tile, file transfer (steps 6–10).
Still out: tray UI, clipboard history, sending folders, choosing among several phones, Windows 11 compact context menu (needs a packaged app).
- TODO: AccessibilityService auto-trigger for phone→PC (detect Copy action / "Copied" toast, then `ReaderActivityTrigger(this).requestRead()`); sideloaded apps need "Allow restricted settings". Untested on the V2502; the `ClipTrigger` seam is already in place.
