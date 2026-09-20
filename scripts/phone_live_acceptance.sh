#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "usage: $0 --apk APP.apk --test-apk TEST.apk --token-file TOKEN --backend https://host [--serial SERIAL]" >&2
  exit 2
}

APK=
TEST_APK=
TOKEN_FILE=
BACKEND=
SERIAL=
while (($#)); do
  case "$1" in
    --apk) APK="${2:-}"; shift 2 ;;
    --test-apk) TEST_APK="${2:-}"; shift 2 ;;
    --token-file) TOKEN_FILE="${2:-}"; shift 2 ;;
    --backend) BACKEND="${2:-}"; shift 2 ;;
    --serial) SERIAL="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done

[[ -f "$APK" && -f "$TEST_APK" && -f "$TOKEN_FILE" ]] || usage
[[ "$BACKEND" == https://* ]] || { echo "backend must use https" >&2; exit 2; }

if [[ -z "$SERIAL" ]]; then
  mapfile -t DEVICES < <(adb devices | awk 'NR>1 && $2=="device" {print $1}')
  [[ ${#DEVICES[@]} -eq 1 ]] || {
    echo "expected exactly one authorized ADB device; found ${#DEVICES[@]}" >&2
    exit 3
  }
  SERIAL="${DEVICES[0]}"
fi
ADB=(adb -s "$SERIAL")
PKG=com.onedayonemasterpiece.streetstory
RUNNER="$PKG.test/androidx.test.runner.AndroidJUnitRunner"

TOKEN_LEN="$(tr -d '\r\n' < "$TOKEN_FILE" | wc -c | tr -d ' ')"
[[ "$TOKEN_LEN" -ge 32 && "$TOKEN_LEN" -le 256 ]] || {
  echo "token file length is outside the accepted range" >&2
  exit 4
}

"${ADB[@]}" get-state >/dev/null
"${ADB[@]}" install -r "$APK" >/dev/null
"${ADB[@]}" install -r "$TEST_APK" >/dev/null
"${ADB[@]}" shell pm grant "$PKG" android.permission.RECORD_AUDIO >/dev/null 2>&1 || true
"${ADB[@]}" shell pm grant "$PKG" android.permission.POST_NOTIFICATIONS >/dev/null 2>&1 || true

# Stage the token through stdin into app-private storage. It never appears in
# adb argv, shell history, process listings, or this script's stdout.
"${ADB[@]}" shell "run-as $PKG sh -c 'umask 077; cat > files/adb-device-token'" < "$TOKEN_FILE"

"${ADB[@]}" shell am start -W   -n "$PKG/.DebugProvisioningActivity"   --es street_story_backend_url "$BACKEND"   --ez street_story_device_token_staged true >/dev/null

# The provisioning activity must consume and delete the staged plaintext.
if "${ADB[@]}" shell "run-as $PKG test -e files/adb-device-token"; then
  echo "staged token was not consumed" >&2
  exit 5
fi

OUT="$("${ADB[@]}" shell am instrument -w   -e class "$PKG.PhysicalDeviceLiveSmokeTest"   "$RUNNER")"
printf '%s\n' "$OUT"
grep -q 'OK (1 test)' <<<"$OUT"

# Leave the owner at the actual application, not a test/activity shell.
"${ADB[@]}" shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null
echo "physical_phone_live_smoke=PASS serial=$SERIAL backend=$BACKEND"
