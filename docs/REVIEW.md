# ClipBridge independent review

Read-only review of `protocol/PROTOCOL.md`, all of `windows/src/clipbridge/` and all of
`android/app/src/main/java/com/clipbridge/`, plus the manifest, `setup.ps1`, `docs/PLAN.md` and `docs/FOOTPRINT.md`.
`pytest windows/tests`: **86 passed** (12.5 s). The Android unit tests were not run (gradle skipped).

None of the findings below has been reproduced yet. Each one was found by reading the code. The section
"How to verify" gives a concrete test for each, for the next agent.

Overall: the crypto core is sound. It uses separate client and server MAC inputs (`c`/`s`), fresh 16-byte nonces
from both sides in the HKDF salt, one key per direction, a strict counter check, constant-time MAC comparison,
and strict base64. The weak points are around it: how the PSK is stored, shared and entered; exposure on
untrusted networks (`PLAN.md` itself names campus Wi-Fi as the threat); missing resource limits; and a few
file-transfer edge cases. The link/feature layering is clean.

Severity: **H** high, **M** medium, **L** low.

---

## A. Security and privacy

### A1. H: Android PSK is stored in plain SharedPreferences and is included in Android backups (fixed)
- **Status**: Fixed in step 11. `allowBackup=false` and `data_extraction_rules.xml` exclude the prefs from cloud backup and device transfer. Test: `ProtocolTest.backupIsDisabledAndPairingKeyExcluded`.
- `android/app/src/main/java/com/clipbridge/Settings.kt:16-23`; `AndroidManifest.xml:16-19` (no `allowBackup`/`dataExtractionRules`)
- **What**: `allowBackup` defaults to true, so the PSK and the `device` id in `clipbridge.xml` are uploaded by Auto Backup
  (Google Drive) and copied by device-to-device transfer.
- **Failure**: Anyone with the backup holds the key (see A2 for what that allows). A new phone restored from the
  backup comes up with the **same device id**, so the old and new phones kick each other off with 4409 in a loop.
- **Fix**: Set `android:allowBackup="false"` and add `dataExtractionRules` that exclude `sharedpref/clipbridge.xml` for both
  `cloud-backup` and `device-transfer` (targetSdk 35 needs both). Optionally wrap the PSK with a non-exportable
  Android Keystore key.

### A2. H: One PSK is shared by every client: device names are not authenticated, and any client can impersonate the server
- `PROTOCOL.md:28-31,42-45`; `windows/src/clipbridge/config.py:43-53`; `link/server.py:158-165`
- **What**: Every phone gets the same PSK. Any key holder can claim any `device` name. The server MAC proves only
  that the other side knows the PSK.
- **Failure**:
  - Phone B, or anyone holding a leaked backup (A1), connects as `device=<phone A>`. A's control connection is closed with 4409
    (`server.py:128-130`). A's queued outgoing files go to the impostor, because the queue is looked up by name
    (`transfer.py:402-409`). The impostor can also discard A's partial uploads (`transfer.py:216-218`).
  - Phone B advertises `_clipbridge._tcp` (`Discovery.kt:53` uses the last service it resolved). Phone A then completes
    the handshake with B and sends B every clip it copies.
  - A single lost phone cannot be revoked without re-keying every device.
- **Fix**: Issue a per-device PSK at pairing. The server keeps a DPAPI-protected `{device: psk}` map, checks the client MAC
  with the key for the claimed device, and closes with 4401 for an unknown device. This is a change to spec §2/§3. If you keep the
  shared key, state in `PROTOCOL.md` that every paired device is fully trusted, including to impersonate the server.

### A3. M: Server is exposed on public networks; mDNS is advertised on every interface (fixed)
- **Status**: Fixed in step 11. Firewall rule is `profile=private remoteip=localsubnet` and replaces any older rule on reinstall; mDNS advertises only private and link-local addresses. Tests: `test_shell.py`, `test_discovery.py`.
- `windows/src/clipbridge/install.py:184-187` (`profile=private,public`, no `remoteip`); `link/discovery.py:19-22,35-42`; `server.py:39` (`0.0.0.0`)
- **What**: On hotel or campus Wi-Fi, which `PLAN.md:11` names as the threat environment, port 8765 is open to every
  host. The laptop also announces `ClipBridge-<hostname>` there, and on VPN, Hyper-V and WSL adapters.
- **Failure**: Strangers on the network can see the laptop's hostname and that it runs ClipBridge. They can also
  reach the pre-auth attack surface (A4).
- **Fix**: Change the firewall rule to `profile=private remoteip=localsubnet`. Advertise only on the interface that holds the
  default route, or on private-profile networks. Tell the user once when the active network is Public.

### A4. M: No pre-auth resource limits on Windows (fixed)
- **Status**: Fixed in step 11. At most 16 handshakes at once (more get 1013), 10 s auth timeout, 4 KiB per-frame limit until `auth_ok` (PROTOCOL.md sections 3, 8). Tests in `test_server.py`.
- `link/server.py:90-100,146-151`
- **What**: `max_size` is 16 MiB from the first byte. The 4 KiB handshake limit (§8) is checked only after the whole
  message has been buffered. The websockets library also queues up to `max_queue` (16) messages per connection
  before the server reads them, so one unauthenticated socket can hold about 256 MiB. There is no cap on concurrent handshakes.
- **Failure**: Any LAN host can open many sockets and push large text frames inside the 10 s window, which can use gigabytes of laptop RAM.
- **Fix**: A semaphore on pre-auth handshakes (globally and per IP), `max_queue=1` and a small `max_size` until auth succeeds,
  and a per-IP connection rate limit.

### A5. M: Android client has no challenge timeout and no inbound size limit, so a LAN host can hang or crash it without the PSK (fixed)
- **Status**: Fixed in step 11. Handshake timer starts on open; handshake text capped at 4096, frames at the protocol max; a hung handshake falls through to the next endpoint. No unit test (needs a fake server).
- `android/.../link/SecureSocket.kt:85-96` (timer starts only after `auth` is sent); `BridgeClient.kt:35-39` (`readTimeout(0)`); `SecureSocket.kt:75-83,109-113`
- **What**: A server that accepts the WebSocket and never sends `challenge` holds the client forever. OkHttp answers the
  pings, so liveness never fails. OkHttp also has no limit on inbound message size.
- **Failure**: An attacker advertises `_clipbridge._tcp` (manual > mDNS > cached order, `Endpoint.kt:21-22`). The phone then sits
  at "Connecting" indefinitely and never falls back to the cached real PC. Alternatively, the attacker sends a 500 MB text frame
  and the service is OOM-killed on every reconnect.
- **Fix**: Start the 10 s handshake timer in `onOpen`. Close if a handshake text is longer than 4096 bytes (this does not stop
  the buffering, so document the §8 deviation or use a reader with a frame cap). When the mDNS endpoint fails the handshake,
  try the cached endpoint next, and do not let a failing mDNS result win again.

### A6. M: Windows clipboard privacy hints are only partly honoured, so passwords can reach the phone (fixed)
- **Status**: Fixed in step 11. `CanIncludeInClipboardHistory` and `CanUploadToCloudClipboard` with value 0 are skipped. Tests in `test_clipboard_win32.py`.
- `windows/src/clipbridge/features/clipboard/win32.py:30,139-142`
- **What**: Only `ExcludeClipboardContentFromMonitorProcessing` is honoured. The standard Windows formats
  `CanIncludeInClipboardHistory` (DWORD 0) and `CanUploadToCloudClipboard` (DWORD 0) are ignored. Some password managers and
  apps set only those, and they are the exact signals Windows' own cloud clipboard obeys.
- **Failure**: The user copies a secret marked "no history / no cloud". ClipBridge sends it to the phone, where IME
  clipboard history (for example Gboard) keeps it.
- **Fix**: Skip a clip when either format is present with value 0. Optionally add `"sensitive": true` to the clip header and set
  `ClipDescription.EXTRA_IS_SENSITIVE` on Android 13+ when writing it.

### A7. M: Received files are auto-accepted and carry no Mark-of-the-Web
- `windows/src/clipbridge/features/files/feature.py:55-57`; `link/transfer.py:354-356`
- **What**: Any key holder (A2), or any app on the phone through the exported share target (A12), can put files into
  `Downloads\ClipShare` without asking the user. No `Zone.Identifier` stream is written, so SmartScreen and Office Protected View do not apply.
- **Failure**: A `.lnk`, `.js` or macro document arrives with the same trust as a local file. The balloon then invites a click.
- **Fix**: After the move, write `Zone.Identifier` (`[ZoneTransfer]\nZoneId=3`) to the received file. Optionally confirm
  executable extensions before saving.

### A8. M: Image decompression bomb on Windows
- `win32.py:228-241` (`png_to_dib`), reached from `features/clipboard/feature.py:149-150`
- **What**: An incoming PNG is fully decoded, converted to RGBA and RGB, and re-encoded as BMP. Pillow only raises above
  ~179 MP, so a few-KB PNG of 13000×13000 pixels passes.
- **Failure**: One image clip from a peer allocates about 1.5–2 GB on the laptop's clipboard thread.
- **Fix**: Read `Image.open(...).size` before `load()` and reject anything over ~40 MP. Lower `Image.MAX_IMAGE_PIXELS`.

### A9. L: The PSK leaks through the console and clipboard during manual pairing
- `windows/src/clipbridge/__main__.py:39-42` (prints the PSK); `android/.../MainActivity.kt:85-90` (plain text field)
- **What**: The manual path is: copy the key from the console, then paste it on the phone. That puts the key in Windows clipboard
  history, in cloud clipboard if enabled, and in ClipBridge's own sync. The Android field is a normal text field, so the keyboard may learn it.
- **Fix**: Pair with a QR code shown on the PC and scanned in the app. Print the key only with `--show`. On Android use
  `KeyboardType.Password`, no autocorrect, and a masked field.

### A10. L: The handshake does not bind the connection kind (control or data)
- `PROTOCOL.md:42-45`
- **What**: The subprotocol is negotiated in plaintext and is not in the MAC input. An on-path attacker can swap it.
  The result is only a disruption (4409 on the real control connection, 4400 elsewhere), but it is a free hardening step.
- **Fix**: Add the subprotocol, or one kind byte, to both MAC inputs and to the HKDF `info`.

### A11. L: Python dependencies are unpinned
- `windows/pyproject.toml:11-15` (`>=` only, no lock file)
- **Failure**: A reinstall pulls a new major version of `websockets` with API changes (the `process_request` and `respond`
  signatures have changed before). This is also a supply-chain risk.
- **Fix**: Pin exact versions (or use a lock or constraints file) and list them in `FOOTPRINT.md`.

### A12. L: The exported share target lets any phone app set the PC clipboard silently
- `AndroidManifest.xml:38-51`; `ShareTargetActivity.kt:17-31`
- **What**: Any app on the phone that can start an activity can send text or files to the PC with no user step.
- **Fix**: Accept that this is how share targets work, but document it. Optionally ignore shares whose `referrer` is our
  own package or empty.

---

## B. Correctness and robustness

### B1. M: An invalid manual host crashes the Android app in a loop (fixed)
- **Status**: Fixed in step 11. `Endpoint.parse` rejects invalid hosts (spaces, IPv6, bad ports); `connectOnce` catches; one `CoroutineExceptionHandler` reaches the service, link and transfer scopes. Tests: `ProtocolTest` endpoint cases.
- `android/.../link/Endpoint.kt:10-18` (no host validation); `SecureSocket.kt:48-53`; `BridgeClient.kt:89-125`; `BridgeService.kt:38` (scope with no `CoroutineExceptionHandler`)
- **What**: Take `"pc name"` (a space) or `"fe80::1"`. `Endpoint.parse` accepts it, then `Request.Builder().url("ws://pc name:8765/")` throws
  `IllegalArgumentException` inside the `loop()` coroutine. Nothing catches it, so the process crashes. `START_STICKY` and
  `MainActivity.onCreate` restart the service, and it crashes again. The user cannot open the app long enough to fix the host
  and has to clear the app's data.
- **Fix**: Validate the host in `Endpoint.parse` (IPv4 or a DNS label pattern) and reject invalid input in the UI. Wrap
  `connectOnce` in `try/catch`, and give the service scope a `CoroutineExceptionHandler` that logs.

### B2. M: A finished transfer whose `xfer_done` is lost is received again as a duplicate (both sides) (fixed)
- **Status**: Fixed in step 11. Both sides keep completed transfer ids on disk for 24 h; a re-offer gets a full `have` and `xfer_done ok:true` without a data connection (PROTOCOL.md section 6.2). Tests: `test_files.py`, `CompletedIdsTest`.
- Windows `link/transfer.py:313-351` (`_finish` with `peer is None`, state deleted); Android `link/Transfer.kt:211-219` (`complete` deletes state, then sends `done`)
- **What**: If the control connection drops between the last range ending and `xfer_done`, the receiver has already moved or committed
  the file and forgotten the id. The sender re-offers the same id after reconnecting, as the spec says. The receiver treats it as new
  and receives the whole file again into `name (1).ext`.
- **Failure**: A Wi-Fi blip at the end of a 2 GB video leaves two copies and 4 GB moved.
- **Fix**: Keep a small "completed ids" record (id, size, final path, timestamp; prune it like partials). On a re-offer of a
  completed id, reply `xfer_accept` with every range having `have == length`, then `xfer_done ok:true` straight away. Add this to spec §6.2.

### B3. M: Disk errors on a Windows data connection leave the transfer stuck, and the Android sender waits forever (fixed)
- **Status**: Fixed in step 11. Disk errors send `xfer_cancel` before closing the data connection; both sides cancel a transfer with no progress for 60 s (hashing counts as progress). Tests: `test_files.py`, `TransferCoreTest`.
- Windows `link/transfer.py:508-546` (`serve_data` does not catch `OSError`); Android `Transfer.kt:153-165` (`reply.await()`/`done.await()` with no timeout)
- **What**: If the disk is full or the `.part` is locked during `_write_at`, the error escapes to `_handle`. The data connection dies,
  `inc` stays in `_incoming`, and no `xfer_done` or `xfer_cancel` is sent. Android waits on `done` with no timeout, and the whole
  outbox (a FIFO, one file at a time) stays blocked until the link drops. After a reconnect the same thing happens again.
- **Fix**: On Windows, catch `OSError` in `_receive_range`, then `_discard`, send `xfer_cancel`, and close with 1000. On Android, add an
  inactivity timeout to `offerOnce`, measured from the last progress rather than total time.

### B4. M: Relaying clips blocks the Windows receive loop for the sending phone (fixed)
- **Status**: Reproduced on devices 2026-11-12 (a 4 MB image from one phone was reported "not delivered" while a
  second phone's connection was half-open). Worse than described: the keepalive ping waits behind the same full
  write buffer, so the dead peer was never closed and every later relay queued behind it. Fixed: `broadcast` no
  longer waits for peers, and `Peer.transmit` drops a connection that accepts no bytes for 20 s (PROTOCOL.md
  section 9). Tests: `test_relay_to_stalled_peer_does_not_delay_sender`, `test_stalled_peer_is_dropped`.
- **Seen on devices** (2026-11-12, 22:48–22:51): phone .52 shared a 3 999 548-byte image four times. Each send logged
  `image delivered=false` after 10 s, followed by `clipbridge.v1 failure: SocketException` / `SocketTimeoutException`
  (OkHttp got no pong). On the PC, port 8765 still showed an `Established` connection from phone .33, but .33 did not
  answer ping ("Destination host unreachable"): a half-open peer that keepalive never closed. A server restart cleared it.
- **Present since** `1952a62` (first Windows server: relay awaited every peer); images used the same path from `eac6443`.
  It stayed hidden until it hit all three conditions at once: a second client connected, that client gone without
  closing, and a message larger than the socket buffers (short text fits in the buffer, so its send completes even
  to a dead peer).
- `features/clipboard/feature.py:131-154`; `link/server.py:71-75,167-181`
- **What**: `on_message` awaits `broadcast`, and each `ws.send` waits for that peer's TCP drain. The sender's `_serve_peer` loop does not
  read its next message, or send its receipt, until every other phone has accepted the bytes.
- **Failure**: Phone B is in a dead zone (half-open TCP until the ≤20 s ping timeout). Phone A sends a 10 MB screenshot. The receipt
  arrives after A's 10 s deadline, so A shows "not delivered" even though the PC has the image. File-transfer control messages from A wait as well.
- **Fix**: Give each peer an outbound queue and a writer task, and have `broadcast` enqueue without awaiting the drain. Send the receipt
  before the relay.

### B5. L: Large back-to-back sends on Android can close the control connection
- `SecureSocket.kt:56-60`; `BridgeClient.kt:76-79`; `ClipboardFeature.kt:84`
- **What**: OkHttp **closes the WebSocket** (1001) when `queueSize + message > 16 MiB`. One image is capped at
  `MAX_BYTES - 1024`, so it fits alone. A second large image sent while the first is still queued goes over the limit and drops the link.
- **Fix**: In `send`, return false when `frame + queueSize() > 16 MiB`. Also consider sending images over 8 MiB as a file transfer.

### B6. L: Replacing a control connection blocks the new connection for up to 10 s
- `link/server.py:128-133`
- **What**: `await old.ws.close(4409)` waits for a close handshake that a stale socket never answers. The wait lasts until
  `close_timeout` (10 s) before `on_peer_connected` and transfer resume run.
- **Fix**: `asyncio.create_task(old.ws.close(4409))`, or abort the old transport.

### B7. L: Windows records `have` before the data is durable
- `link/transfer.py:303-305`
- **What**: No `flush`/`fsync` before `inc.save()` (Android syncs, `Transfer.kt:232-236`). After a power cut the state claims bytes that
  are zeros. The hash then mismatches and the whole file is re-sent, using up the only retry.
- **Fix**: `f.flush(); os.fsync(f.fileno())`, then save, and checkpoint about every 32 MiB as Android does instead of every 4 MiB.

### B8. L: Partial files are pruned only at startup, and a cancel during a write can leak them
- `link/transfer.py:134-145` (called only from `server.py:89`); `_discard` at `transfer.py:367-379`
- **What**: The server runs for weeks, so an abandoned multi-GB `.part` stays that long. `_discard` can also run while `_receive_range` still
  has the `.part` open. On Windows the unlink then fails, and the next `inc.save()` re-creates the `.json`.
- **Fix**: Prune hourly. In `_discard`, wait for the stream task to finish before deleting, or open with `FILE_SHARE_DELETE`.

### B9. L: Uncaught exceptions in Android transfer coroutines crash the app
- `Transfer.kt:85` (scope with no handler); `Transfer.kt:192-209` (`store.save` is not wrapped in `onOffer`)
- **Failure**: An `IOException` (app storage full) or an unexpected `RuntimeException` from MediaStore kills the process.
- **Fix**: Add a `CoroutineExceptionHandler` that logs and fails the transfer, and wrap `store.save`.

### B10. L: The Android wakelock ends after 1 h during long transfers
- `TransferLock.kt:19-27`; `Transfer.kt:278-281`
- **What**: `hold(true)` runs only on the 0→1 busy edge. With `STREAMS = 1` the whole file is one range, so the comment "each range re-acquires"
  does not hold. A transfer longer than 1 h with the screen off loses the CPU wakelock.
- **Fix**: Re-acquire on every progress callback, or hold the lock without a timeout and release it in `finally`.

### B11. L: Heavy image decode on Android's main thread
- `ShareTargetActivity.kt:38-49`; `ImageUtil.kt:20-26`
- **What**: `readPng` reads the whole file and decodes it at full size on the UI thread (no `inSampleSize`). A 50 MP photo shared
  as a single image means a bitmap of about 200 MB and risks an ANR or OOM before falling back to a file transfer.
- **Fix**: Check bounds first (`inJustDecodeBounds`) and send large images straight as files. Do the work on a background thread.

### B12. L: Receipts are sent before the PC clipboard write happens
- `features/clipboard/feature.py:149-154`; `win32.py:64-70,188-208`
- **What**: `set_text`/`set_image` only queue the write, so the phone shows "delivered" even if `SetClipboardData` then fails after its retries.
- **Fix**: Return a future from the clipboard thread and await it in `on_message`, with a short timeout.

### B13. L: Smaller issues
- `Settings.kt:12-14`: two threads can generate two different device ids on first run. Fix: generate under a lock with `commit()`.
- Multi-phone assumptions: there is one global pending clip (`feature.py:108-112`), and `FilesFeature.send` uses `peers[0]` (`features/files/feature.py:49-53`).
  Fix: make these per device, or document "one phone" in `PLAN.md`.
- `feature.py:142-148`: every received image is saved forever to `Pictures\ClipBridge`. Fix: dedupe by hash (already computed) or cap the folder.

---

## C. Spec and doc drift, dead code
- §8 receive limits and the 4 KiB handshake cap are not enforced on Android (A5). There is no client timeout before `challenge`.
- §6.2 does not say what happens on a re-offer of a completed id (B2). Android sends `xfer_done ok:false` when commit fails, but §6.2
  defines `ok:false` only for a hash mismatch.
- `PLAN.md:106,114-115,119,125-126` describe `data/Settings.kt`, `ui/MainActivity.kt`, a logcat-based `clipboard/LogcatTrigger.kt`,
  a `READ_LOGS` permission and `pm grant READ_LOGS`. None of these exist: `Settings.kt` and `MainActivity.kt` are in the root package,
  there is no logcat trigger, and `setup.ps1` does not grant `READ_LOGS`. `FOOTPRINT.md:36` also lists a `READ_LOGS` grant.
- Dead code: `ClipTrigger`/`ReaderActivityTrigger.requestRead()` (`ClipTrigger.kt:12-25`) is never called. Only `readerIntent` is used.
- Probably unneeded privilege: `SYSTEM_ALERT_WINDOW` (`AndroidManifest.xml:11`, `setup.ps1:12`, `MainActivity.kt:97`, which says "needed to read
  clipboard in background"). Every clipboard read is now started by the user from a notification, tile or share, and none of those needs it.
  If no background trigger is planned, remove it along with the checklist item, the setup line and the FOOTPRINT row.

## D. Improvements and quality of life (not bugs)
1. **QR pairing**: show a QR on the PC (`clipbridge pair --qr`) and scan it in the app. This removes adb and copy-paste, and fixes A9.
2. **Feedback when sending from Windows**: `app.py:88-90` throws away the result of `files.send`. Explorer "Send to phone" gives no progress and no
   "delivered" or "failed". Reuse the balloon for "Sent X to phone" and "Could not send X".
3. **Cancel transfers**: neither side has a cancel control. Add a "Cancel" action to the Android progress notification (the protocol already has
   `xfer_cancel`) and to the Windows balloon.
4. **`clipbridge status`** should list connected phones and active transfers. Today it only shows whether the process is running. A small IPC
   over the existing pipe would do it.
5. **Persist the Android outbox**: shares queued while offline live only in memory (`Transfer.kt:87`), so they are lost silently if the process is
   killed. Persist URIs and their grants (or copy small files), or tell the user they were dropped.
6. **Tray presence on Windows**: there is no way to see link state or pause sync. A minimal tray icon (connected / paused / quit) would help.
7. **Pause sync / per-direction toggle** on both sides, for sensitive sessions such as screen sharing or password entry.
8. **Explorer verb with many files**: check whether `MultiSelectModel=Player` with `"%1"` (`install.py:84-91`) starts one Python process per file.
   If it does, selecting 50 files starts 50 interpreters, and the SendTo shortcut, which gets all files at once, is the better default.
9. **Receive folder settings** (Windows `Downloads\ClipShare`, `Pictures\ClipBridge`; Android Downloads) are hard-coded. One config entry each.
10. **Transfer code shape**: `transfer.py` (546 lines) and `Transfer.kt` (474 lines) each mix queueing, the offer state machine and streaming.
    Pulling the sender and receiver state machines out as pure logic, as `transfer_core` did for ranges, would make B2 and B3 unit-testable.

---

## E. How to verify (for the next agent)
Each item says what to build and what result shows the bug. The Windows tests can use `windows/tests/tclient.py` as the phone.

| # | Test | Bug confirmed if |
|---|---|---|
| B2 | pytest in `test_files.py`: phone sends a file. Close the **control** connection after the last `xfer_data` but before the server sends `xfer_done` (keep the data connection open long enough to send `xfer_end`). Reconnect and re-offer the same id. | Server answers with fresh ranges (`have=0`) and the destination has `name.ext` and `name (1).ext` |
| B3 | pytest: monkeypatch `transfer._write_at` to raise `OSError` on the 2nd chunk | No `xfer_cancel`/`xfer_done` reaches the client, and `link._transfers._incoming` still holds the id |
| B4 | pytest: two clients. B stops reading (pause its transport). A sends a ~10 MB image with a `mid`, then a text with a `mid` | A's ack arrives after more than 10 s, or only after B is closed |
| B6 | Extend `test_duplicate_device_replaces_older_4409`: the old client never answers the close (pause its reader). Time until the new client gets its pending clip | About 10 s instead of under 1 s |
| A4 | Script: open 20 WebSocket connections with subprotocol `clipbridge.v1`, each sends a 16 MiB text frame, then idle for 9 s. Watch the server's RSS | RSS grows by hundreds of MB |
| A5 | Script: a websockets server on 8765 that accepts `clipbridge.v1` and never sends anything. Set it as the phone's manual host | Phone shows "Connecting" for more than 30 s and never tries the cached PC |
| B1 | JVM unit test: `Endpoint.parse("pc name")` is not null, and `Request.Builder().url("ws://pc name:8765/")` throws `IllegalArgumentException`. On a device: save host `pc name` | App crash loop in logcat (`FATAL EXCEPTION`) |
| A6 | pywin32 script: open the clipboard and set `CF_UNICODETEXT` plus the registered format `CanIncludeInClipboardHistory` = DWORD 0, with tclient connected | tclient receives the clip |
| A7 | Receive any file, then `Get-Item <file> -Stream *` | No `Zone.Identifier` stream |
| A8 | pytest: `png_to_dib` on a 13000×13000 single-colour PNG (a few KB). Measure peak memory | Over 1 GB, and no rejection |
| A1 | `adb shell dumpsys package com.clipbridge \| findstr ALLOW_BACKUP`, or check the merged manifest | `ALLOW_BACKUP` flag set |
| B5 | On a device: send two ~12 MB screenshots back to back over slow Wi-Fi | Control connection closes (log shows `1001`/failure) and the second image is reported as not delivered |
| B7 | Manual: pull power, or kill -9 plus filesystem cache drop, mid-transfer, then resume | Resume ends with a hash mismatch and a full re-send |

---

## Top 5 to fix first
1. **Stop backing up the PSK and device id**: `allowBackup=false` plus `dataExtractionRules` for cloud and device transfer (A1).
2. **Per-device PSKs**, so device names are authenticated, the server is authenticated per device, and a lost phone can be revoked (A2).
3. **Close the network exposure**: firewall `profile=private remoteip=localsubnet`, cap pre-auth handshakes and their memory on Windows,
   and add a handshake timeout and size cap on Android (A3, A4, A5).
4. **Fix the Android crash loop on an invalid manual host**: validate the host and catch exceptions in the connect loop (B1).
5. **Honour all Windows clipboard privacy formats** (`CanIncludeInClipboardHistory`, `CanUploadToCloudClipboard`) so secrets do not reach the
   phone (A6).

Next in line: B2 (duplicate file after a lost `xfer_done`), B3 (stuck transfer on a disk error, with no timeouts), A7 (Mark-of-the-Web on received files),
B4 (relays blocking the receive loop), A8 (image bomb).
