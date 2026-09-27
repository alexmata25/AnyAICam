#!/usr/bin/env bash
# Samsung Step 3 -- post-cutover smoke test (read-only; no camera contact).
# Usage: sudo bash step3-smoke.sh
set -uo pipefail
COMMIT=f5a6d875c5cc9bd313e85ac7a330359f39255150
IMAGE_ID=sha256:d5cd5fc263445dd18ce75e06bbc053feec935bb31f45ec0f53ab934babe5054b
C="${STEP3_CONTAINER:-anyaicam-vms}"; URL="${STEP3_BASE_URL:-http://127.0.0.1:8000}"  # overrides only for rehearsal
pass=0; fail=0
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS  $name"; pass=$((pass+1)); else echo "FAIL  $name"; fail=$((fail+1)); fi; }

check "container $C runs image $IMAGE_ID" test "$(docker inspect $C --format '{{.Image}}')" = "$IMAGE_ID"
check "no /app source bind mount" sh -c "! docker inspect $C --format '{{range .Mounts}}{{println .Destination}}{{end}}' | grep -qx /app"
check "docker health is healthy" test "$(docker inspect $C --format '{{.State.Health.Status}}')" = healthy
check "/health ok and build $COMMIT" sh -c "curl -fsS $URL/health | grep -q '\"build_id\":\"$COMMIT\"'"
check "/health runtime_role edge" sh -c "curl -fsS $URL/health | grep -q '\"runtime_role\":\"edge\"'"
check "/ready self_test ok with 0 critical config issues" sh -c "curl -sS $URL/ready | python3 -c 'import json,sys;d=json.load(sys.stdin);s=d[\"self_test\"];sys.exit(0 if s[\"ok\"] and not [i for i in s.get(\"configuration_issues\",[]) if i[\"severity\"]==\"critical\"] else 1)'"
check "database migrated to >= 41 migrations, integrity ok" docker exec $C python3 -c "
import sqlite3,sys;c=sqlite3.connect('/app/recordings/partner_portal.db')
sys.exit(0 if c.execute('select count(*) from schema_migrations').fetchone()[0]>=41 and c.execute('pragma integrity_check').fetchone()[0]=='ok' else 1)"
check "tesseract binary + pytesseract" docker exec $C python3 -c "import pytesseract;assert str(pytesseract.get_tesseract_version()).startswith('5')"
check "analytics models present" docker exec $C sh -c "test -s /app/yolov8n.pt && test -s /opt/anyaicam-ppe-model/yolov8n-ppe.pt && test -s /opt/anyaicam-aac-models/face_detection_yunet_2023mar.onnx && test -s /opt/anyaicam-aac-models/face_recognition_sface_2021dec.onnx && test -s /opt/anyaicam-aac-models/arcfaceresnet100-11-int8.onnx"
check "analytics modules import (lpr, ppe, people_counting, smart_motion, detection_exclusion)" docker exec -w /app $C python3 -c "import lpr, ppe, people_counting, smart_motion, detection_exclusion"
check "YOLO inference runs offline on a blank frame" docker exec -w /app $C python3 -c "
from ultralytics import YOLO; import numpy as np; YOLO('/app/yolov8n.pt')(np.zeros((640,640,3),dtype='uint8'),verbose=False)"
check "no Python tracebacks in the last 10 minutes of logs" sh -c "! docker logs --since 10m $C 2>&1 | grep -q Traceback"
echo "---- $pass passed, $fail failed"
echo "note: /ready 'ready' stays false until at least one camera records (edge rule); that is expected with 0 cameras."
[ "$fail" -eq 0 ]
