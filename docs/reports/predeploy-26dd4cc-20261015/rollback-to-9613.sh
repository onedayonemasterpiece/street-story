#!/usr/bin/env bash
# Manual rollback of the Street Story backend to 9613cbd after a failed or
# rejected 26dd4cc activation. The installer cannot do this: its exact-source
# gate requires the published branch tip to equal --expected-sha.
#
# Restores the exact 9613 unit/service/provider configuration captured at
# 14:49 UTC, restarts the service and verifies /healthz.source_sha. It never
# touches the SQLite database: added indexes stay (additive, readable by 9613),
# stories/jobs/attempts/external operations are not rewound. The qualification
# cache backup is restored only by an explicit --restore-qualification-cache.
set -euo pipefail

ROLLBACK_SHA=9613cbde2b10e1977d2f1a84c4576bc6fcc3b9b0
REVIEW_6083=/home/dev/artifacts/street-story/20261014T104149Z-research-unblocking-20261014/paired-deployment-review-6083cd3
BACKUP=$REVIEW_6083/rollback-sensitive
RELEASES=/home/dev/.local/share/street-story/releases
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"

declare -A TARGET=(
  [service.env]=/home/dev/.local/state/street-story/service.env
  [providers.env]=/home/dev/.local/state/street-story/providers.env
  [street-story.service]=/home/dev/.config/systemd/user/street-story.service
)

# Expected backup digests live only in the local rollback plan captured at
# 14:49 UTC, so credential-file hashes are never copied into this script.
declare -A EXPECTED
while read -r name digest; do
  EXPECTED[$name]=$digest
done < <(python3 -I -c '
import json, os, sys
for row in json.load(open(sys.argv[1]))["rollback_sensitive_files"]:
    print(os.path.basename(row["backup"]), row["source_sha256"])
' "$REVIEW_6083/rollback-plan.json")
[[ ${#EXPECTED[@]} -eq ${#TARGET[@]} ]] || { echo "rollback plan does not cover all files" >&2; exit 2; }

for name in "${!TARGET[@]}"; do
  actual=$(sha256sum "$BACKUP/$name" | cut -d' ' -f1)
  [[ "$actual" == "${EXPECTED[$name]:-}" ]] || { echo "backup digest mismatch: $name" >&2; exit 2; }
done
[[ -x "$RELEASES/$ROLLBACK_SHA/venv/bin/uvicorn" ]] || { echo "9613 environment unavailable" >&2; exit 2; }

for name in "${!TARGET[@]}"; do
  install -m 600 "$BACKUP/$name" "${TARGET[$name]}"
done
systemctl --user daemon-reload
systemctl --user restart street-story.service

deadline=$((SECONDS + 90))
until curl -fsS --max-time 5 http://127.0.0.1:8188/healthz 2>/dev/null \
    | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('ok') is True and d.get('source_sha')=='$ROLLBACK_SHA' else 1)"; do
  (( SECONDS < deadline )) || { echo "rollback health gate failed" >&2; exit 3; }
  sleep 1
done
ln -sfn "$RELEASES/$ROLLBACK_SHA" /home/dev/.local/share/street-story/.current-rollback
mv -T /home/dev/.local/share/street-story/.current-rollback /home/dev/.local/share/street-story/current

if [[ "${1:-}" == "--restore-qualification-cache" ]]; then
  echo "qualification cache restore is manual: see rollback-plan.json in paired-deployment-review-6083cd3" >&2
fi
echo "{\"status\":\"rolled_back\",\"source_sha\":\"$ROLLBACK_SHA\",\"at_utc\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
