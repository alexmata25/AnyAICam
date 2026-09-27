#!/usr/bin/env bash
# Samsung Step 3 -- update /etc/anyaicam/vms.env in place for f5a6d875c5cc.
# Only the keys below are set/added. ANYAICAM_APP_SECRETS and
# ANYAICAM_CAMERA_CREDENTIAL_KEY are NEVER read, printed or changed.
# A timestamped backup is written first. Values mirror the Ryzen
# reference edge appliance on the same build, minus Ryzen-specific
# camera lists and with WebRTC/P2P deferred.
# Usage: sudo bash step3-vms-env.sh
set -euo pipefail
ENV_FILE="${STEP3_ENV_FILE:-/etc/anyaicam/vms.env}"  # override only for rehearsal
COMMIT=f5a6d875c5cc9bd313e85ac7a330359f39255150
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE" >&2; exit 1; }
grep -q '^ANYAICAM_APP_SECRETS=.\+' "$ENV_FILE" || { echo "ANYAICAM_APP_SECRETS missing -- stop" >&2; exit 1; }
grep -q '^ANYAICAM_CAMERA_CREDENTIAL_KEY=.\+' "$ENV_FILE" || { echo "ANYAICAM_CAMERA_CREDENTIAL_KEY missing -- stop" >&2; exit 1; }

backup="$ENV_FILE.pre-step3-$(date -u +%Y%m%dT%H%M%SZ)"
cp -p "$ENV_FILE" "$backup"
echo "backup: $backup"

upsert() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

upsert ANYAICAM_RUNTIME_ROLE edge
upsert ANYAICAM_ENV production
upsert ANYAICAM_CLOUD_URL https://portal-staging.anyaicam.com
upsert ANYAICAM_VMS_COMMIT "$COMMIT"
upsert ANYAICAM_BUILD_ID "$COMMIT"
upsert AWS_REGION us-east-1
upsert ANYAICAM_LPR_ENABLED true
upsert PEOPLE_COUNTING_ENABLED true
upsert ANYAICAM_FACIAL_RECOGNITION_ENABLED true
upsert ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED true
upsert ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED true
upsert ANYAICAM_ANALYTICS_SYNC_ENABLED true
upsert ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED true
upsert ANYAICAM_LIVE_RELAY_ENABLED true
upsert ANYAICAM_LIVE_P2P_ENABLED false
upsert ANYAICAM_RECORDING_UPLOAD_ENABLED false
upsert ANYAICAM_RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA 12
upsert ANYAICAM_LOCAL_STORAGE_MANAGEMENT_ENABLED true
upsert ANYAICAM_LOCAL_STORAGE_AUTO_DELETE_ENABLED true
upsert ANYAICAM_TALK_DOWN_DISCOVERY_ENABLED true

if id anyaicam >/dev/null 2>&1; then chown anyaicam:anyaicam "$ENV_FILE"; fi; chmod 0640 "$ENV_FILE"
echo "updated keys (secrets untouched):"
grep -E '^(ANYAICAM_|PEOPLE_|AWS_)' "$ENV_FILE" | grep -vE 'APP_SECRETS|CREDENTIAL_KEY' | sort
