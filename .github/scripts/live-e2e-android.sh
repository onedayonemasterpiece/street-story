#!/usr/bin/env bash
set -euo pipefail

PKG=com.onedayonemasterpiece.streetstory
ARTIFACT_DIR=backend/live-e2e-artifacts
FIXTURE_DIR=live-android-fixtures
KEEP_PUBLICATION="${LIVE_E2E_KEEP_PUBLICATION:-false}"
IDENTITY_ONLY="${LIVE_E2E_IDENTITY_ONLY:-false}"
[[ "$KEEP_PUBLICATION" == true || "$KEEP_PUBLICATION" == false ]]

adb install -r app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
adb shell pm path "$PKG" >/dev/null
adb shell pm grant "$PKG" android.permission.RECORD_AUDIO || true
adb shell pm grant "$PKG" android.permission.POST_NOTIFICATIONS || true
adb shell "run-as $PKG mkdir -p files/live-golden"
for name in config.json token.txt photo.jpg voice-{1..6}.pcm; do
  test -s "$FIXTURE_DIR/$name"
  adb exec-in "run-as $PKG sh -c 'cat > files/live-golden/$name'" < "$FIXTURE_DIR/$name"
done
mkdir -p "$ARTIFACT_DIR"
adb shell am instrument -w \
  -e class com.onedayonemasterpiece.streetstory.LiveGoldenInstrumentedTest \
  -e keepPublication "$KEEP_PUBLICATION" \
  -e identityOnly "$IDENTITY_ONLY" \
  "$PKG.test/androidx.test.runner.AndroidJUnitRunner" \
  | tee "$ARTIFACT_DIR/android-instrumentation.txt"
# Preserve bounded evidence on failure; never export token/configuration stores.
adb exec-out "run-as $PKG cat files/live-golden/stage-progress.json" > "$ARTIFACT_DIR/android-stage-progress.json" || true
adb exec-out "run-as $PKG cat files/live-golden/evidence.json" > "$ARTIFACT_DIR/android-golden-evidence.json" || true
mkdir -p "$ARTIFACT_DIR/stage-screenshots"
for name in $(adb shell "run-as $PKG ls files/live-golden/screenshots" 2>/dev/null | tr -d '\r'); do
  [[ "$name" =~ ^[0-9][0-9]-[a-z-]+\.png$ ]] || continue
  adb exec-out "run-as $PKG cat files/live-golden/screenshots/$name" > "$ARTIFACT_DIR/stage-screenshots/$name"
done
grep -q 'OK (1 test)'  "$ARTIFACT_DIR/android-instrumentation.txt"
test -s "$ARTIFACT_DIR/android-golden-evidence.json"
adb shell am start -W -n "$PKG/.MainActivity" >/dev/null
sleep 3
adb exec-out screencap -p > "$ARTIFACT_DIR/android-preview.png"
test -s "$ARTIFACT_DIR/android-preview.png"
python - <<'PY'
import json
import os
import pathlib

evidence = json.loads(pathlib.Path('backend/live-e2e-artifacts/android-golden-evidence.json').read_text())
if os.environ.get('LIVE_E2E_IDENTITY_ONLY', 'false') == 'true':
    if evidence.get('identity_only') is not True or evidence.get('automatic_identity') is not True:
        raise SystemExit('Android ordinary photo identity not proved')
    if evidence.get('discovery_seeded') is not False or evidence.get('physical_mic') is not False:
        raise SystemExit('Android identity provenance is incorrect')
    transport = evidence.get('transport_final') or {}
    if transport.get('transport') != 'wss' or transport.get('http_audio_fallback') is not False:
        raise SystemExit('Android identity did not use WSS')
    raise SystemExit(0)
keep = os.environ.get('LIVE_E2E_KEEP_PUBLICATION', 'false') == 'true'
if evidence.get('publication_kept') is not keep:
    raise SystemExit('Android golden publication mode mismatch')
if keep:
    if evidence.get('destination_alias') != 'street_story_e2e_20260928_tg' or not evidence.get('publication_id'):
        raise SystemExit('Owner-visible mode lacks exact test-group publication receipt')
elif evidence.get('cancel_confirmed') is not True:
    raise SystemExit('Android golden run did not confirm native cancellation cleanup')
if evidence.get('physical_mic') is not False or evidence.get('prepared_pcm_after_capture_boundary') is not True:
    raise SystemExit('Android golden evidence misrepresents prepared PCM acceptance')
if evidence.get('legacy_voice_endpoint_used') is not False:
    raise SystemExit('Android golden run unexpectedly used the legacy voice endpoint')
transport = evidence.get('transport_final') or {}
if transport.get('transport') != 'wss' or transport.get('http_audio_fallback') is not False or transport.get('event_polling') is not False:
    raise SystemExit('Android golden run did not prove WSS transport')
if int(transport.get('received_pcm_bytes') or 0) <= 0:
    raise SystemExit('Android golden run received no model audio')
PY
