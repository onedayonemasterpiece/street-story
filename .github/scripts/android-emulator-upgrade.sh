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
trap 'adb logcat -d -s StreetStoryProvisioning:I "*:S" > provisioning-logcat.txt; adb logcat -d -s AndroidRuntime:E "*:S" > instrumentation-crash-logcat.txt; mkdir -p ui-evidence; for evidence in original-picker-ui.xml original-picker-ui.png; do adb pull "/sdcard/Android/data/$pkg/files/$evidence" "ui-evidence/$evidence" || true; done' EXIT
adb shell am instrument -w -r "$pkg.test/androidx.test.runner.AndroidJUnitRunner" | tee instrumentation.txt
grep -q 'OK (' instrumentation.txt
mkdir -p ui-evidence
adb pull "/sdcard/Android/data/$pkg/files/real-facts-ui.png" ui-evidence/real-facts-ui.png
