#!/usr/bin/env bash
set -euo pipefail

PKG=com.onedayonemasterpiece.streetstory
ARTIFACT_DIR=backend/live-e2e-artifacts
FIXTURE_DIR=live-android-fixtures

adb install -r app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
adb shell pm path "$PKG" >/dev/null

adb shell pm grant "$PKG" android.permission.RECORD_AUDIO || true
adb shell pm grant "$PKG" android.permission.POST_NOTIFICATIONS || true
adb shell "run-as $PKG mkdir -p files/live-golden"

for name in config.json token.txt photo.jpg voice-{1..10}.pcm; do
  test -s "$FIXTURE_DIR/$name"
  adb exec-in "run-as $PKG sh -c 'cat > files/live-golden/$name'" < "$FIXTURE_DIR/$name"
done

mkdir -p "$ARTIFACT_DIR"
adb shell am instrument -w \
  -e class com.onedayonemasterpiece.streetstory.LiveGoldenInstrumentedTest \
  "$PKG.test/androidx.test.runner.AndroidJUnitRunner" \
  | tee "$ARTIFACT_DIR/android-instrumentation.txt"

# Preserve the sanitized fixture evidence even when an assertion fails.
# Never export token.txt or the application's credential store.
adb exec-out "run-as $PKG cat files/live-golden/evidence.json" > "$ARTIFACT_DIR/android-golden-evidence.json" || true
grep -q 'OK (1 test)' "$ARTIFACT_DIR/android-instrumentation.txt"
test -s "$ARTIFACT_DIR/android-golden-evidence.json"

adb shell am start -W -n "$PKG/.MainActivity" >/dev/null
sleep 3
adb exec-out screencap -p > "$ARTIFACT_DIR/android-preview.png"
test -s "$ARTIFACT_DIR/android-preview.png"

python - <<'PY'
import json
import pathlib

evidence = json.loads(
    pathlib.Path("backend/live-e2e-artifacts/android-golden-evidence.json").read_text()
)
if evidence.get("cancel_confirmed") is not True:
    raise SystemExit("Android golden run did not confirm native cancellation cleanup")
if (
    evidence.get("physical_mic") is not False
    or evidence.get("prepared_pcm_after_capture_boundary") is not True
):
    raise SystemExit("Android golden evidence misrepresents prepared PCM acceptance")
if evidence.get("legacy_voice_endpoint_used") is not False:
    raise SystemExit("Android golden run unexpectedly used the legacy voice endpoint")
PY
