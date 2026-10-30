#!/usr/bin/env bash
set -euo pipefail

pkg="com.onedayonemasterpiece.streetstory"
current_apk="apks/street-story.apk"
test_apk="apks/street-story-debug-androidTest.apk"

if [[ "${GITHUB_EVENT_NAME:-}" == "pull_request" ]]; then
  adb install -r "$current_apk"
else
  : "${GH_TOKEN:?GH_TOKEN is required for canonical upgrade verification}"
  : "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
  : "${RUNNER_TEMP:?RUNNER_TEMP is required}"
  : "${ANDROID_HOME:?ANDROID_HOME is required}"
  : "${STREET_STORY_CANONICAL_CERT_SHA256:?canonical certificate digest is required}"

  previous_dir="$RUNNER_TEMP/street-story-previous"
  rm -rf "$previous_dir"
  mkdir -p "$previous_dir"

  previous_tag="$(gh release view --repo "$GITHUB_REPOSITORY" --json tagName --jq '.tagName')"
  [[ -n "$previous_tag" ]]
  gh release download "$previous_tag" \
    --repo "$GITHUB_REPOSITORY" \
    --pattern 'street-story.apk' \
    --dir "$previous_dir"

  previous_apk="$previous_dir/street-story.apk"
  [[ -s "$previous_apk" ]]
  previous_cert="$("$ANDROID_HOME/build-tools/36.0.0/apksigner" verify --print-certs "$previous_apk" \
    | awk -F': ' '/Signer #1 certificate SHA-256 digest:/ {print $2; exit}' \
    | tr '[:upper:]' '[:lower:]' | tr -d ':')"
  [[ "$previous_cert" == "$STREET_STORY_CANONICAL_CERT_SHA256" ]]

  adb install "$previous_apk"
  before_code="$(adb shell dumpsys package "$pkg" | awk -F= '/versionCode=/{print $2; exit}' | awk '{print $1}')"
  [[ -n "$before_code" ]]

  adb install -r "$current_apk"
  after_code="$(adb shell dumpsys package "$pkg" | awk -F= '/versionCode=/{print $2; exit}' | awk '{print $1}')"
  [[ -n "$after_code" ]]
  (( after_code > before_code ))
  echo "upgrade-compat previous=$previous_tag before=$before_code after=$after_code"
fi

adb install -r "$test_apk"
adb shell pm grant "$pkg" android.permission.RECORD_AUDIO || true
adb shell pm grant "$pkg" android.permission.POST_NOTIFICATIONS || true
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
trap 'capture_emulator_platform; adb logcat -d -s StreetStoryProvisioning:I StreetStoryPlatformE2E:W "*:S" > provisioning-logcat.txt; adb logcat -d -s AndroidRuntime:E "*:S" > instrumentation-crash-logcat.txt; adb logcat -d -s StreetStoryMapE2E:I StreetStoryMap:I "*:S" > owner-place-map-logcat.txt; mkdir -p ui-evidence; for evidence in owner-photo-gps-rejected.png original-picker-ui.xml original-picker-ui.png original-picker-launcher-anr.xml original-picker-launcher-anr.png owner-place-group-pending.png owner-place-group-identifying.png owner-place-group-review.png owner-place-group-reopen.png owner-place-group-failure.png owner-place-group-failure.xml owner-place-group-launcher-anr.png owner-place-group-launcher-anr.xml owner-place-group-window-blocked.png owner-place-group-window-blocked.xml owner-place-map-ui.png owner-place-map-reopen.png owner-place-map-failure.png owner-place-map-failure.xml owner-place-map-failure.txt owner-place-map-before-tap.xml owner-place-empty-facts-ui.png owner-place-empty-facts-reopen.png owner-place-empty-facts-failure.png owner-place-empty-facts-failure.xml owner-place-map-launcher-anr.png owner-place-map-launcher-anr.xml owner-place-map-window-blocked.png owner-place-map-window-blocked.xml owner-place-empty-facts-launcher-anr.png owner-place-empty-facts-launcher-anr.xml owner-place-empty-facts-window-blocked.png owner-place-empty-facts-window-blocked.xml owner-review-source-launcher-anr.png owner-review-source-launcher-anr.xml owner-review-source-window-blocked.png owner-review-source-window-blocked.xml owner-review-conflict-launcher-anr.png owner-review-conflict-launcher-anr.xml owner-review-conflict-window-blocked.png owner-review-conflict-window-blocked.xml; do adb pull "/sdcard/Android/data/$pkg/files/$evidence" "ui-evidence/$evidence" || true; done' EXIT
# This emulator is disposable. Keep its display awake throughout the suite;
# package accessibility alone does not prove that physical input can be routed.
# No product tap or failed instrumentation is repeated here.
adb shell svc power stayon true
adb shell settings put system screen_off_timeout 1800000
adb shell input keyevent KEYCODE_WAKEUP
adb shell wm dismiss-keyguard
# Fixture tests drive SyncWorker explicitly; hold automatic constrained work.
# Live/physical acceptance uses the same runner without this test-only argument.
adb shell am instrument -w -r -e street_story_fixture_sync true \
  "$pkg.test/$pkg.StreetStoryTestRunner" | tee instrumentation.txt
grep -q 'OK (' instrumentation.txt
mkdir -p ui-evidence
adb pull "/sdcard/Android/data/$pkg/files/real-facts-ui.png" ui-evidence/real-facts-ui.png
