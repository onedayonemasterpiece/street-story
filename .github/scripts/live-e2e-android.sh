#!/usr/bin/env bash
set -euo pipefail

PKG=com.onedayonemasterpiece.streetstory
ARTIFACT_DIR="${LIVE_E2E_ARTIFACT_DIR:-backend/live-e2e-artifacts}"
FIXTURE_DIR="${LIVE_E2E_FIXTURE_DIR:-live-android-fixtures}"
KEEP_PUBLICATION="${LIVE_E2E_KEEP_PUBLICATION:-false}"
IDENTITY_ONLY="${LIVE_E2E_IDENTITY_ONLY:-false}"
[[ "$KEEP_PUBLICATION" == true || "$KEEP_PUBLICATION" == false ]]
[[ "$IDENTITY_ONLY" == true || "$IDENTITY_ONLY" == false ]]
[[ "$IDENTITY_ONLY" != true || "$KEEP_PUBLICATION" != true ]]
export LIVE_E2E_ARTIFACT_DIR="$ARTIFACT_DIR"

# Check guest DNS before any story/provider operation. Emulator DNS is configured
# at launch with -dns-server; keep the real HTTPS hostname and normal TLS checks.
mkdir -p "$ARTIFACT_DIR"
BACKEND_HOST=$(python - "$FIXTURE_DIR/config.json" <<'PY'
import ipaddress
import json
import re
import sys
from urllib.parse import urlsplit

with open(sys.argv[1], encoding='utf-8') as source:
    parsed = urlsplit(json.load(source)['backend_url'])
host = parsed.hostname or ''
if parsed.scheme != 'https' or parsed.username or parsed.password or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
    raise SystemExit('E2E backend must use its real HTTPS hostname')
try:
    ipaddress.ip_address(host)
except ValueError:
    print(host)
else:
    raise SystemExit('E2E backend hostname must not be replaced by an IP address')
PY
)
DNS_READY=false
: > "$ARTIFACT_DIR/android-dns-readiness.txt"
for attempt in {1..8}; do
  probe=$(timeout 5s adb shell ping -4 -c 1 -W 1 "$BACKEND_HOST" 2>&1 || true)
  printf 'attempt=%s\n%s\n' "$attempt" "${probe:0:1600}" >> "$ARTIFACT_DIR/android-dns-readiness.txt"
  # Successful name resolution is sufficient; remote ICMP may be blocked.
  if [[ "$probe" == "PING $BACKEND_HOST ("* ]]; then DNS_READY=true; break; fi
  if [[ "$attempt" -lt 8 ]]; then sleep 2; fi
done
python - "$ARTIFACT_DIR/android-dns-readiness.json" "$BACKEND_HOST" "$DNS_READY" "$attempt" <<'PY'
import json
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_text(json.dumps({
    'schema_version': 1, 'hostname': sys.argv[2], 'guest_dns_resolved': sys.argv[3] == 'true',
    'attempts': int(sys.argv[4]), 'max_attempts': 8, 'real_hostname_preserved': True,
    'tls_verification_unchanged': True, 'story_creation_started_at_preflight': False,
}, indent=2) + '\n', encoding='utf-8')
PY
if [[ "$DNS_READY" != true ]]; then
  echo 'Emulator DNS not ready; check launch -dns-server before retrying E2E.' >&2
  exit 1
fi

adb install -r app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
adb shell pm path "$PKG" >/dev/null
if [[ "$IDENTITY_ONLY" == true ]]; then
  adb shell pm revoke "$PKG" android.permission.RECORD_AUDIO || true
else
  adb shell pm grant "$PKG" android.permission.RECORD_AUDIO || true
fi
adb shell pm grant "$PKG" android.permission.POST_NOTIFICATIONS || true
adb shell "run-as $PKG mkdir -p files/live-golden"
fixture_names=(config.json token.txt photo.jpg)
if [[ "$IDENTITY_ONLY" != true ]]; then fixture_names+=(voice-{1..6}.pcm); fi
for name in "${fixture_names[@]}"; do
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
import re

evidence = json.loads((pathlib.Path(os.environ['LIVE_E2E_ARTIFACT_DIR']) / 'android-golden-evidence.json').read_text())
if os.environ.get('LIVE_E2E_IDENTITY_ONLY', 'false') == 'true':
    if evidence.get('identity_only') is not True or evidence.get('automatic_identity') is not True:
        raise SystemExit('Android ordinary photo identity not proved')
    if (evidence.get('discovery_seeded') is not False or evidence.get('physical_mic') is not False
            or evidence.get('prepared_owner_photo') is not True or evidence.get('owner_name_hint') is not False
            or evidence.get('seed_urls_supplied') is not False):
        raise SystemExit('Android identity provenance is incorrect')
    if (evidence.get('identity_without_live') is not True
            or evidence.get('identity_transport') != 'headless_https_poll'
            or evidence.get('no_live_or_recording_started') is not True
            or evidence.get('prepared_pcm_after_capture_boundary') is not False
            or evidence.get('record_audio_permission_granted') is not False
            or evidence.get('legacy_voice_endpoint_used') is not False):
        raise SystemExit('Android identity requires Live, microphone or owner input')
    for name in ('local_voice_session_count', 'backend_live_message_count', 'backend_voice_message_count', 'completed_live_turns'):
        if evidence.get(name) != 0:
            raise SystemExit('Android identity unexpectedly created voice/Live evidence: ' + name)
    transport = evidence.get('transport_final') or {}
    if (transport.get('transport') != 'off' or int(transport.get('received_pcm_bytes') or 0) != 0
            or int(transport.get('output_audio_chunks') or 0) != 0 or int(transport.get('event_cursor') or 0) != 0):
        raise SystemExit('Android headless identity unexpectedly used a Live transport')
    identity = evidence.get('visual_identity') or {}
    scope = evidence.get('identity_scope') or {}
    if (identity.get('status') != 'match' or identity.get('visual_reference_verified') is not True
            or identity.get('photo_sha256') != evidence.get('fixture_photo_sha256')
            or identity.get('photo_sha256') != scope.get('photo_sha256')
            or type(scope.get('identity_generation')) is not int
            or identity.get('generation') != scope.get('identity_generation')):
        raise SystemExit('Android headless identity lacks current visual proof')
    for name in ('identity_reference_count', 'identity_model_image_count', 'identity_source_url_count', 'identity_http_read_count'):
        if type(evidence.get(name)) is not int or evidence[name] < 1:
            raise SystemExit('Android headless identity provenance count missing: ' + name)
    references = identity.get('reference_evidence') or []
    if (not isinstance(references, list) or any(not isinstance(item, dict)
            or not re.fullmatch('[0-9a-f]{64}', str(item.get('model_image_sha256') or ''))
            or not str(item.get('source_url') or '').startswith('https://')
            or item.get('subject_candidate_id', item.get('candidate_id')) != identity.get('candidate_id')
            for item in references)):
        raise SystemExit('Android headless identity decoded reference provenance is invalid')
    if (len(references) != evidence['identity_reference_count']
            or len({item['model_image_sha256'] for item in references}) != evidence['identity_model_image_count']):
        raise SystemExit('Android headless identity reference counts disagree with retained proof')
    source_urls = evidence.get('identity_source_urls') or []
    if (not isinstance(source_urls, list) or any(not isinstance(url, str) or not url.startswith('https://') for url in source_urls)
            or len(set(source_urls)) != evidence['identity_source_url_count']):
        raise SystemExit('Android headless identity source URL count disagrees with retained proof')
    if (evidence.get('identity_progress') or {}).get('visual_comparison_verified') is not True:
        raise SystemExit('Android headless identity progress lacks verified comparison')
    if not any(item.get('stage') == '02-object-identified' for item in evidence.get('stage_screenshots', [])):
        raise SystemExit('Android headless identity lacks current UI readback')
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
