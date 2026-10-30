# ADB log session — 2026-10-30

Device-log capture of `com.onedayonemasterpiece.streetstory` while the app was driven
manually on a physical device. Captured with `adb logcat` (main + system + crash buffers)
in `threadtime` format; the buffers were cleared right before capture started, so
`logcat-session.log` contains only this session.

## Environment

| Item | Value |
|---|---|
| Device | Samsung SM-G998B (p3sxser), Android 15 / SDK 35 |
| Serial | `R5CNC1DQ23H` |
| App | `com.onedayonemasterpiece.streetstory` v0.1.1615 (versionCode 1615) |
| App source commit | **`de75352`** — CI run #1615, workflow "Street Story Android MVP", 2026-10-22 |
| Capture window | 2026-10-30 09:19:44 → 09:26:39 (+02:00), ~7 min |
| Raw volume | 22 071 lines / 3.2 MB |

### Provenance caveat

`versionCode`/`versionName` are `GITHUB_RUN_NUMBER`, so the installed build maps to a CI
run, not to a git tag. Run **#1615** is a `push` to `main` at **`de75352`** (2026-10-22).

The machine that captured these logs had a checkout sitting on the old
`work/street-story-mvp-20260908` branch at `2c87618` (2026-09-08), **1124 commits behind
`main`**. `device-info.txt` records that local HEAD; it is *not* the source of the APK.
Investigate against `de75352`, not against `2c87618`.

## Files

| File | Content |
|---|---|
| `logcat-session.log` | Full raw capture, `threadtime`, all PIDs, unfiltered |
| `logcat-app-filtered.log` | Same stream reduced to app-relevant tags (app process, audio, WebRTC, SQLite, notifications, network, runtime) |
| `crashes-anr.txt` | FATAL / ANR / native-signal extract — **empty result set this session** |
| `device-info.txt` | Device, OS, APK, git metadata and capture markers |
| `screenshot-final.png` | Device screen at the stop marker (facts/chat stage) |

The pre-session buffer dump (38 MB raw) was preserved locally only and is not committed.

## Timeline (device clock)

| Time | Event |
|---|---|
| 09:19:44 | Capture started, log buffers cleared |
| 09:20:12 | Start marker `I StreetStoryLogCapture: === capture started ===` |
| 09:22:08.139 | App process spawned: `pid 31615` |
| 09:22:08.172 | `START u0 ... .MainActivity` (launcher) |
| 09:22:09.552 | `Displayed .../.MainActivity for user 0: +1s390ms` |
| 09:22:09.266 | `SecurityException: com.onedayonemasterpiece.streetstory has no access to content://media/external/images/media/1000150444` (photo URI access, repeated at 09:22:29 and 09:22:30) |
| 09:23:12.051 | Live voice session 1 admitted: `hello_ack`, `transport=wss`, `session_id=live_275e358e58ad4a52bea9ca5a80ff14dc` |
| 09:23:14 – 09:24:21 | PCM frames streamed in ~1.6 s batches, `ack_seq` trailing `frame_seq` by 2–4 frames, `capture_age_ms` 60–225 |
| 09:24:41.116 – 09:24:42.450 | `diagnostic_send_failed ... SocketTimeoutException` for `speech_admission`, `speech_started`, `capture_activity`, then `transport_failure` and `live_failed` |
| 09:25:37.988 | Second start admitted: `attempt_e15f7d62b71446e58317ac741ee69c39_retry_1`, new `session_id=live_68748df26b9d4aa491359bfe515346d5` |
| 09:25:39 – 09:26:07 | Audio streaming resumes on the retry attempt, `capture_age_ms` up to 245 |
| 09:26:39 | Stop marker, capture terminated |

## Observations worth a look

1. **Media permission gap (app-side).** Three `SecurityException`s from `mediaprovider`
   on `content://media/external/images/media/1000150444` right after the app was shown.
   The picked photo URI was queried without a persisted read grant, so the app cannot
   read the selected image. The screen at stop time shows a facts card, so the flow
   partly recovered, but the grant handling is wrong.
   Relevant code on `de75352`: `PhotoIntake.kt` (uses `PickVisualMedia`), and
   `PhotoImporter.kt:21`, where `takePersistableUriPermission` is wrapped in
   `runCatching { … }.isSuccess` — its result is discarded, and `PickVisualMedia` does not
   contribute a persistable grant in the first place. `PhotoAssets.kt` and
   `PhotoIntakeInstrumentedTest.kt` are the other touch points.
2. **One live-session transport failure; recovery went through admission retry.** The first
   WSS attempt died with `SocketTimeoutException` after ~89 s of streaming and the client
   emitted `transport_failure` / `live_failed`. The follow-up session carried a `_retry_1`
   attempt id — that suffix is produced by `LiveStartAdmissionPolicy` via
   `LiveSessionController.kt` (`attempt + "_retry_" + retry`), i.e. it is a retried
   `/live/start` admission, **not** an OkHttp transport reconnect. The 56 s gap between
   `live_failed` (09:24:42) and the retry `hello_ack` (09:25:37) fits repeated admission
   rejections at the policy's default 5 s pacing; the exact rejection codes are not in the
   log because of item 3 (diagnostics never left the device) — check the backend's
   `/live/start` log for those attempts.
3. **Diagnostics could not be delivered.** All five failure diagnostics hit the same
   socket timeout, so nothing about the failure reached the backend. Those payloads carry
   `source_sha` (`BuildConfig.SOURCE_SHA`) and `app_version`, which would have pinned the
   build unambiguously — the provenance in this document was reconstructed from CI instead.
4. **Notifications suppressed.** 23× `NotificationService: Suppressing notification from
   package com.onedayonemasterpiece.streetstory by user request.` — the app posts
   foreground-service/recording notifications that the user has blocked at channel level.
5. **No crash, no ANR, no native signals** anywhere in the capture.

Unrelated system noise (not app bugs): `batterystats` `SQLiteLog` insert error on
`table all has no column named screen_doze_discharge` (Samsung system component),
`pageboostd`/`FreecessController` activity.

## Investigation starting points

```bash
# photo grant failures
grep -n 'SecurityException\|has no access to content://media' logcat-app-filtered.log

# live voice lifecycle
grep -n 'StreetStoryLive' logcat-app-filtered.log
grep -o '"event":"[a-z_]*"' logcat-app-filtered.log | sort | uniq -c

# crash / ANR sweep over the full capture
grep -nE 'FATAL EXCEPTION|ANR in |AndroidRuntime:' logcat-session.log
```

Code on `de75352`: `LiveSessionController.kt` (start/retry, `diagnostic()`,
`transport_failure`), `LiveStartAdmissionPolicy.kt` (admission pacing),
`PhotoIntake.kt` / `PhotoImporter.kt` / `PhotoAssets.kt` (photo intake).

## Reproducing

```powershell
adb logcat -c
adb logcat -v threadtime -b main -b system -b crash > logs/<session>/logcat-session.log
# ... drive the app ...
# stop, then extract:
#   Select-String -Pattern 'FATAL EXCEPTION|ANR in |AndroidRuntime:' logcat-session.log
```
