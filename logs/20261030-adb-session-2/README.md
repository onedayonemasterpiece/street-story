# ADB capture — 2026-10-30, session 2

Raw device log capture during a manual Street Story app session. This directory contains captured artifacts only; no incident analysis is included.

## Capture metadata

- Device: Samsung SM-G998B, Android 15 / SDK 35
- App package: `com.onedayonemasterpiece.streetstory`
- Installed app version at capture start: `0.1.1630` (versionCode 1630)
- Device serial: recorded in `device-info.txt`
- Capture window: recorded in `device-info.txt` (local device time)
- Capture: `adb logcat -v threadtime -b main -b system -b crash`
- Raw stream: `logcat-session.log`

## Artifacts

- `logcat-session.log` — unmodified session stream, all captured buffers
- `logcat-app-filtered.log` — mechanically selected lines mentioning the app package, `StreetStory`, AndroidRuntime, or crash/ANR markers; not an interpretation
- `device-info.txt` — device, app and capture metadata
- `screenshot-final.png` — screen captured when logging stopped

The pre-session device buffer was saved locally for safety but is intentionally excluded from this repository upload.
