#!/usr/bin/env bash
set -euo pipefail

pkg="com.onedayonemasterpiece.streetstory"
current_apk="apks/street-story.apk"
test_apk="apks/street-story-debug-androidTest.apk"

: "$GH_TOKEN" "$GITHUB_REPOSITORY" "$RUNNER_TEMP" "$ANDROID_HOME"
: "$STREET_STORY_CANONICAL_CERT_SHA256"
previous_dir="$RUNNER_TEMP/street-story-previous"
mkdir -p "$previous_dir"
verified_current="$current_apk"
if [[ "$GITHUB_EVENT_NAME" == "pull_request" ]]; then
  # PR builds use a debug signer; verify the existing two owner-signed releases.
  tags="$(gh release list --repo "$GITHUB_REPOSITORY" --limit 2 --json tagName --jq '.[].tagName')"
  current_tag="$(printf '%s\n' "$tags" | sed -n '1p')"
  previous_tag="$(printf '%s\n' "$tags" | sed -n '2p')"
  mkdir -p "$previous_dir/current"
  gh release download "$current_tag" --repo "$GITHUB_REPOSITORY" -p street-story.apk -D "$previous_dir/current"
  verified_current="$previous_dir/current/street-story.apk"
else
  previous_tag="$(gh release view --repo "$GITHUB_REPOSITORY" --json tagName --jq '.tagName')"
fi
test -n "$previous_tag"
gh release download "$previous_tag" --repo "$GITHUB_REPOSITORY" -p street-story.apk -p BUILD.txt -D "$previous_dir"
previous_apk="$previous_dir/street-story.apk"
old_run="$(sed -n 's/^run_id=//p' "$previous_dir/BUILD.txt")"
old_code="$(sed -n 's/^version_code=//p' "$previous_dir/BUILD.txt")"
test -n "$old_run" && test -n "$old_code"
gh run download "$old_run" --repo "$GITHUB_REPOSITORY" -n "street-story-apk-$old_code" -D "$previous_dir/ci"
previous_test="$previous_dir/ci/street-story-debug-androidTest.apk"

for apk in "$previous_apk" "$previous_test" "$verified_current"; do
  actual="$("$ANDROID_HOME/build-tools/36.0.0/apksigner" verify --print-certs "$apk" |
    awk -F': ' '/Signer #1 certificate SHA-256 digest:/ {print $2; exit}' | tr '[:upper:]' '[:lower:]' | tr -d ':')"
  test "$actual" = "$STREET_STORY_CANONICAL_CERT_SHA256"
done
adb shell svc power stayon true
adb shell settings put system screen_off_timeout 1800000
adb shell input keyevent KEYCODE_WAKEUP
adb shell wm dismiss-keyguard
adb install "$previous_apk"
adb install "$previous_test"
adb shell am instrument -w -r -e street_story_fixture_sync true -e class \
  "$pkg.StoryLifecycleTest#photoDraftChunksChoicesAndCrashRecoverySurviveReopen" \
  "$pkg.test/$pkg.StreetStoryTestRunner" | tee upgrade-seed.txt
grep -q 'OK (1 test)' upgrade-seed.txt
adb shell am force-stop "$pkg"
mkdir -p "$previous_dir/before" "$previous_dir/after"
adb exec-out run-as "$pkg" tar -cf - databases shared_prefs | tar -xf - -C "$previous_dir/before"
story_id="$(python3 - "$previous_dir/before" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1] + "/databases/street-story.db")
rows = db.execute("SELECT client_story_id FROM stories WHERE client_story_id LIKE 'story-test-%'").fetchall()
assert len(rows) == 1, rows
print(rows[0][0])
PY
)"
before_code="$(adb shell dumpsys package "$pkg" | awk -F= '/versionCode=/{print $2; exit}' | awk '{print $1}')"
adb install -r "$verified_current"
after_code="$(adb shell dumpsys package "$pkg" | awk -F= '/versionCode=/{print $2; exit}' | awk '{print $1}')"
(( after_code > before_code ))
adb shell pm grant "$pkg" android.permission.POST_NOTIFICATIONS || true
adb shell pm grant "$pkg" android.permission.READ_MEDIA_IMAGES || true
adb shell pm grant "$pkg" android.permission.ACCESS_MEDIA_LOCATION || true
adb shell am start -W -n "$pkg/.MainActivity" --es open_story_id "$story_id"
mkdir -p ui-evidence
adb exec-out screencap -p > ui-evidence/upgrade-reopen.png
test -n "$(adb shell pidof "$pkg")"
adb shell am force-stop "$pkg"
adb exec-out run-as "$pkg" tar -cf - databases shared_prefs | tar -xf - -C "$previous_dir/after"
python3 - "$previous_dir/before" "$previous_dir/after" "$story_id" <<'PY' | tee upgrade-readback.txt
import sqlite3, sys, xml.etree.ElementTree as ET
from pathlib import Path
story_id = sys.argv[3]
def state(root):
    root = Path(root)
    db = sqlite3.connect(root / "databases/street-story.db")
    story = db.execute("SELECT stage,photo_sha256,draft_text FROM stories WHERE client_story_id=?", (story_id,)).fetchone()
    photos = db.execute("SELECT photo_id,uri,position FROM story_photo_choices WHERE story_id=? ORDER BY position", (story_id,)).fetchall()
    facts = db.execute("SELECT fact_id,selected FROM facts WHERE story_id=? ORDER BY fact_id", (story_id,)).fetchall()
    def pref(file, key):
        path = root / f"shared_prefs/{file}.xml"
        if not path.exists(): return None  # The old fixture has no active-topic preference yet.
        node = ET.parse(path).find(f".//string[@name='{key}']")
        return None if node is None else node.text
    return story, photos, facts, pref("street_story_draft_overrides", story_id), pref("street_story_topics_v1", "active_story_id")
before, after = state(sys.argv[1]), state(sys.argv[2])
assert before[0] is not None and before[1] and before[1][0][0] == "source", before
assert before[2] == [("supported", 0)] and before[3] == "Моя локальная правка", before
assert before[:4] == after[:4], (before, after)
assert after[4] == story_id, after
print(f"upgrade-local-readback=PASS story={story_id} old-and-new-stage={after[0][0]} photo-choice={len(after[1])} selected-facts={sum(row[1] for row in after[2])}")
PY
echo "upgrade-compat previous=$previous_tag before=$before_code after=$after_code"

if [[ "$GITHUB_EVENT_NAME" == "pull_request" ]]; then
  adb uninstall "$pkg.test"
  adb uninstall "$pkg"
  adb install -r "$current_apk"
fi

adb install -r "$test_apk"
adb shell pm grant "$pkg" android.permission.RECORD_AUDIO || true
adb shell pm grant "$pkg" android.permission.POST_NOTIFICATIONS || true
adb shell pm grant "$pkg" android.permission.READ_MEDIA_IMAGES || true
adb shell pm grant "$pkg" android.permission.ACCESS_MEDIA_LOCATION || true
# Preserve the emulator boundary independently of product assertions. A black
# frame with a populated hierarchy needs input/window/power and compositor logs,
# rather than another injected tap. These bounded diagnostics never restart the
# emulator, dismiss dialogs, or retry instrumentation.
capture_emulator_platform() {
  {
    timeout 5s adb shell dumpsys window windows | awk '/mCurrentFocus=|mFocusedApp=|mDisplayFrozen=/' || true
    timeout 5s adb shell dumpsys input | awk '/FocusedApplications:|FocusedWindows:/ { remaining=4 } remaining>0 { print; remaining--; next } /dispatchEnabled=|dispatchFrozen=|DispatchEnabled:|DispatchFrozen:/ { print }' || true
    timeout 5s adb shell dumpsys power | awk '/mWakefulness=|mInteractive=|mDisplayReady=|Display Power:/' || true
  } > emulator-platform-state.txt 2>&1
  timeout 5s adb logcat -d -t 500 -s InputDispatcher:I InputReader:W WindowManager:W SurfaceFlinger:W PowerManagerService:I DisplayPowerController:I "*:S" > emulator-platform-logcat.txt 2>&1 || true
}
trap 'capture_emulator_platform; adb logcat -d -s StreetStoryProvisioning:I StreetStoryPlatformE2E:W "*:S" > provisioning-logcat.txt; adb logcat -d -s AndroidRuntime:E "*:S" > instrumentation-crash-logcat.txt; adb logcat -d -s StreetStoryMapE2E:I StreetStoryMap:I "*:S" > owner-place-map-logcat.txt; adb logcat -d -s PhonePhotoIndex:I "*:S" > phone-photo-index-logcat.txt; mkdir -p ui-evidence; for evidence in owner-card-notes-before-visual.png reading-articles-checkbox.png multiphoto-selection.png multiphoto-puzzle.png multiphoto-reordered.png multiphoto-cards.png multiphoto-carousel.png owner-photo-gps-rejected.png original-picker-ui.xml original-picker-ui.png original-picker-launcher-anr.xml original-picker-launcher-anr.png owner-place-group-pending.png owner-place-group-identifying.png owner-place-group-review.png owner-place-group-reopen.png owner-place-group-failure.png owner-place-group-failure.xml owner-place-group-launcher-anr.png owner-place-group-launcher-anr.xml owner-place-group-window-blocked.png owner-place-group-window-blocked.xml owner-place-map-ui.png owner-place-map-reopen.png owner-place-map-failure.png owner-place-map-failure.xml owner-place-map-failure.txt owner-place-map-before-tap.xml owner-place-empty-facts-ui.png owner-place-empty-facts-reopen.png owner-place-empty-facts-failure.png owner-place-empty-facts-failure.xml owner-place-map-launcher-anr.png owner-place-map-launcher-anr.xml owner-place-map-window-blocked.png owner-place-map-window-blocked.xml owner-place-empty-facts-launcher-anr.png owner-place-empty-facts-launcher-anr.xml owner-place-empty-facts-window-blocked.png owner-place-empty-facts-window-blocked.xml owner-review-source-launcher-anr.png owner-review-source-launcher-anr.xml owner-review-source-window-blocked.png owner-review-source-window-blocked.xml owner-review-conflict-launcher-anr.png owner-review-conflict-launcher-anr.xml owner-review-conflict-window-blocked.png owner-review-conflict-window-blocked.xml; do adb pull "/sdcard/Android/data/$pkg/files/$evidence" "ui-evidence/$evidence" || true; done' EXIT
# Emulator power/input prepared before signed-upgrade seeding.
# Fixture tests drive SyncWorker explicitly; hold automatic constrained work.
# Live/physical acceptance uses the same runner without this test-only argument.
adb shell am instrument -w -r -e street_story_fixture_sync true \
  "$pkg.test/$pkg.StreetStoryTestRunner" | tee instrumentation.txt
grep -q 'OK (' instrumentation.txt
mkdir -p ui-evidence
adb pull "/sdcard/Android/data/$pkg/files/real-facts-ui.png" ui-evidence/real-facts-ui.png