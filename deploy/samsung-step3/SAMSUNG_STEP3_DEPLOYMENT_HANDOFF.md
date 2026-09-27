# SAMSUNG STEP 3 DEPLOYMENT HANDOFF

Prepared on the Dell (the authoritative development/control machine), 2026-09-27. Input: `DELL_HANDOFF_SAMSUNG_READONLY_FACTS.md`.
Nothing was deployed to, stopped on, or changed on the Samsung, the Ryzen, any camera, or the QIXIANG.

---

## 0. READY / BLOCKED (only items that need you)

**READY. Samsung Claude can execute these once you say go:**
- **Phase A:** verify the artifacts and load the image. Nothing running is affected.
- **Phase B:** cut the VMS over to the self-contained f5a6d87 image. About 1–2 minutes of VMS downtime, with a full rollback path.
- **Phase C:** upgrade the appliance agent to the authoritative f5a6d87 agent, and archive (not delete) the stale offline queue.

**BLOCKED. These need your decision or involvement:**
1. **Samsung's cloud identity no longer exists anywhere live.** See §5. Appliance `dead660d…` / cloud ID `66AA5864…` / customer `495badf7…` / site `89d7fa8eeb` are absent from the only live AnyAiCam cloud (`portal-staging.anyaicam.com`). They were issued by the legacy cloud at 32.197.193.3:8000, which is dead. Keeping the identity is therefore impossible. The only supported way to connect Samsung is a **claim**: `anyaicam-setup --claim`, which runs the agent's `coordinated_reenroll()` with automatic rollback. You told me not to re-enroll tonight, so it hasn't been done. **Decide:**
   - (a) whether to claim Samsung into staging (Phase D);
   - (b) **which customer and site** it belongs to (for example the "Alejandro Mata" customer that owns the Ryzen, or a separate demo customer/site).

   You must be signed in as that customer's **owner** to approve the claim code within 15 minutes.
2. **Physical LPR validation** with the real vehicle and camera. Only the software pipeline has been verified (§6).
3. **Talkdown physical validation is still open** from last night. The Bedroom camera is configured correctly, but the Talk press never reached AnyAiCam. The exact browser URL used is still needed. This isn't a Samsung dependency; it's listed so it isn't lost.

Until Phase D, Samsung keeps working **locally** on the new release: VMS, analytics and recording. Cloud calls keep failing, exactly as they do today.

---

## 1. Release identity

| Item | Value |
|---|---|
| Branch | `reconcile/golden-foundation-20260911` (never `main`) |
| VMS commit | `f5a6d875c5cc9bd313e85ac7a330359f39255150` (includes the Talkdown work) |
| Image tag | `anyaicam-vms:f5a6d87` |
| **Immutable image ID** | `sha256:d5cd5fc263445dd18ce75e06bbc053feec935bb31f45ec0f53ab934babe5054b` |
| Platform | linux/amd64, CPU only (torch 2.5.1+cpu) |
| Image tarball | `anyaicam-vms-f5a6d875c5cc-linux-amd64.tar.gz`, **sha256 `aaa33eebf12a7d1b0d34d6dbbcf7925b888fb95c60570832ed3ef96555eb4826`**, 796,614,699 bytes |
| Agent/installer artifact | `anyaicam-appliance-installer-1.1.0-vms-f5a6d875c5cc.tar.gz`, **sha256 `96e08e123f463033751ec0a20aa3a160aff4e18a8c6a93912cc27daa4d8f29b6`**. Only `payload/agent` is used. |
| Built | On the amd64 build host from a clean checkout (dirty=0), with the appliance `Dockerfile`. It is byte-identical to the image serving the staging cloud tonight. |

**How the self-contained requirement was solved:** the image is built on the build side and shipped as a checksummed tarball. Samsung only runs `docker load`, then Compose starts it by tag with `pull_policy: never`. The script checks the image ID before cutover and again in the smoke test. Samsung never builds anything and has no source mount. (A registry repo digest isn't used, because Samsung has no registry credentials. The image ID is the content hash of the image config, so it's equally immutable.)

**Why `Dockerfile` and not `Dockerfile.production`:** `Dockerfile.production` is the cloud image and runs 2 uvicorn workers. An edge appliance needs a single process for its camera workers. The appliance `Dockerfile` is exactly what the Ryzen reference runs, and it bakes the models outside `/app`.

## 2. Artifact locations

- **Dell:** `C:\Users\Alejandro Mata\OneDrive\Desktop\AnyAiCam-VMS-reconciliation\dist\samsung-step3\` holds both tarballs.
- **Thumbdrive:** `E:\samsung-step3\` holds both tarballs, plus everything in `deploy/samsung-step3/`.
- **Repo:** `deploy/samsung-step3/` on the authoritative branch holds this handoff, `docker-compose.samsung.yml`, `step3-verify-artifacts.sh`, `step3-vms-env.sh`, `step3-smoke.sh` and `RELEASE_MANIFEST.json`.
- **Private S3 backup copy:** `s3://anyaicam2026/releases/samsung-step3/`. The bucket blocks all public access.

## 3. Image contents (inspected, not assumed)

These were all verified inside the image with the network disabled:
- **OCR:** tesseract 5.5.0 (eng, osd) and pytesseract 0.3.13, connected to each other.
- **Libraries:** ultralytics 8.3.40, torch 2.5.1+cpu, OpenCV 4.10, fastapi 0.115.6, websockets 14.1, boto3.
- **Models (baked in, checksum-pinned at build):**
  - `/app/yolov8n.pt`
  - `/opt/anyaicam-ppe-model/yolov8n-ppe.pt`
  - YuNet face detection
  - SFace
  - ArcFace int8
- **App code:** present in `/app`, including `lpr.py`, `ppe.py`, `people_counting.py`, `smart_motion.py`, `detection_exclusion.py` and `talk_audio_relay.py`, with the f5a6d87 Talkdown markers.
- ffmpeg is present.
- YOLO inference runs offline.

## 4. Samsung migration findings

**Persistent volumes.** The new compose keeps the same host paths:

| Host | Container | Status |
|---|---|---|
| `/var/lib/anyaicam/vms/recordings` | `/app/recordings` | keep (includes `partner_portal.db`) |
| `/var/lib/anyaicam/vms/hls` | `/app/static/hls` | keep |
| `/var/lib/anyaicam/vms/data-config` | `/opt/anyaicam/data/config` | keep |
| `/var/lib/anyaicam` | `/var/lib/anyaicam` **ro** | keep (the agent's `credential.json`, used by the VMS uploaders) |
| `/opt/anyaicam/app` | `/app` | **removed**. The directory stays on disk untouched, for rollback. |

**Database.**
- f5a6d87 migrates the database **in place** at startup.
- This was tested on a database created by Samsung's own code (f613b2b). That code produces exactly Samsung's state: 61 tables, 16 migrations, latest `20260910_appliance_claims`.
- After upgrading: **41 migrations, 97 tables, `integrity_check = ok`.**
- Keep the existing database. Every data table is empty anyway, but keeping it preserves the schema history. A pre-cutover copy is taken for rollback.

**Environment (`/etc/anyaicam/vms.env`).**
- **Keep both secrets as they are.** `ANYAICAM_APP_SECRETS` must be at least 32 characters and not a known default. That rule is identical in 0.9.0 and f5a6d87, so Samsung's current value, which 0.9.0 accepts in production, will pass.
- `step3-vms-env.sh` changes only these keys, never the secrets:
  - `ANYAICAM_CLOUD_URL=https://portal-staging.anyaicam.com`
  - `ANYAICAM_VMS_COMMIT` and `ANYAICAM_BUILD_ID` = f5a6d87
  - `AWS_REGION=us-east-1`
  - The Ryzen-reference feature flags (LPR, People Counting, facial, analytics sync, event media, live relay, local storage management, talk-down discovery).
  - `ANYAICAM_LIVE_P2P_ENABLED=false` (deferred, see §10).
  - `ANYAICAM_RECORDING_UPLOAD_ENABLED=false`.
- The rehearsal confirmed by checksum, before and after, that the two secret lines are unchanged.

**Readiness.**
- For an edge appliance, f5a6d87 does **not** require the cloud items Samsung reported missing (database, S3, public URL, secrets manager). Those checks apply only to the cloud and combined roles.
- `/ready` returns `ready:false` (HTTP 503) until at least one camera is recording. That's the edge rule, and it's expected while Samsung has 0 cameras.
- **Pass criteria:** `/health` ok, plus `self_test.ok = true` with 0 critical configuration issues. That's the same bar the installer's `validate.sh` uses.

## 5. Cloud endpoint and identity (verified read-only)

- **Current authoritative endpoint: `https://portal-staging.anyaicam.com`.**
  - `app.anyaicam.com` is served by the same container (build f5a6d87, role cloud).
  - `portal.anyaicam.com` doesn't answer.
  - `http://32.197.193.3:8000` times out from the Dell too; it's dead.
  - The Ryzen reference appliance enrolls against portal-staging.
- **Samsung's identity is not in the authoritative cloud.**
  - Staging has exactly 4 appliances: the Ryzen `2f941627b4` (AIC-C814766E), two old offline records (`2d41ced390`, `5e76625989`), and an end-to-end test record. Leave all of them untouched.
  - There are no rows for appliance `dead660d…` or cloud ID `66AA5864…`, no credential row, and no customer `495badf7…` or site `89d7fa8eeb` (not even partial matches).
  - Samsung's 32-hex IDs are a different format from staging's 10-character IDs.
- **The supported repoint method.** `agent.json`'s `portal_url`, `cloud_id` and `mode` are *activation-scoped*: the agent's config loader deliberately ignores `agent.env` overrides once `agent.json` holds real values. So editing environment variables can't repoint an activated appliance, and hand-editing `agent.json` would leave a credential the new cloud doesn't recognise. The only supported path is `anyaicam-setup --claim`:
  1. It asks for the portal URL and mode.
  2. It opens a claim session and shows a claim code.
  3. A customer **owner** approves the code at `https://portal-staging.anyaicam.com/customer/claim-appliance`, choosing a site that belongs to that customer, within 15 minutes.
  4. The agent then runs `coordinated_reenroll()`. It detects the existing `agent.json`, credential and VMS identity, replaces all three atomically with rollback on failure, resets stale camera bindings, and restarts the services through the privileged watcher.

  This is Phase D, and it's **BLOCKED on your decision** (§0).

## 6. Agent compatibility

- **Samsung's agent is 0.1.0, installed from f613b2b source. The authoritative agent is also labelled 0.1.0, but it has 16 changes since f613b2b** (`git log f613b2b..f5a6d87 -- appliance-agent`). They include:
  - the local credential handoff for cloud-provisioned cameras;
  - the stale camera-binding lifecycle fix;
  - waiting for activation instead of crash-looping;
  - config precedence fix #3 (a stale environment value overriding a newer activation);
  - uninstall of drop-ins;
  - storage management;
  - WireGuard and LAN P2P.
- **Result: upgrade required.** The version label is the same, but the code is materially behind. The `credential.json` path and format are unchanged: the VMS uploaders read `$ANYAICAM_STATE_DIR/credential.json`, which defaults to `/var/lib/anyaicam/credential.json`, the same as today.
- **Upgrade artifact:** `payload/agent` from the verified installer tarball. It's installed with the agent's own `scripts/install.sh`, which:
  - reinstalls the venv package;
  - keeps `agent.env`, `agent.json` and the credential;
  - reinstalls the unit and the privileged watcher;
  - restarts the agent.

  The installer's top-level `install.sh` is **not** run, because it would rebuild the image on Samsung and restore the source bind mount.
- **Tests:** the agent suite passes **571/571 on Linux** (pinned python:3.12-slim, as a non-root user).

## 7. Test and build results

| Test | Result |
|---|---|
| Image offline dependency/model inspection (§3) | **PASS** |
| Isolated start: network none, Samsung-format database, no `/app` mount | **PASS**: `/health` ok, self_test ok, 0 critical issues, 0 tracebacks, 0 restarts, database 16→41 migrations |
| **Full cutover rehearsal on the build host** (sandboxed paths and port): load from tarball, image ID check, env script on a legacy-style `vms.env`, Samsung compose up, smoke test, down/up | **PASS**: 12/12 smoke checks, healthy after 30 s, secrets unchanged, database kept after `down` |
| Agent suite, Linux | **PASS**: 571/571 |
| Agent suite, Windows Dell | 568/571. The 3 failures are Windows-only: a venv `bin/` path and POSIX stat behaviour. They pass on Linux. |
| Talkdown suites, Dell | **PASS**: 155/155 (one intermittent timing failure passed on rerun) |
| Talkdown under a **real uvicorn server** inside the image (new regression test `test_talk_real_server.py`) | **PASS**, 3/3 runs. Local fallback: ready sent, audio forwarded, relay stopped, session stopped. 5 rapid press/release cycles: no orphaned sessions. |
| VMS suites inside the image (Talkdown, analytics, database/migration, edge sync/uploader) | 24 failures, **0 new regressions**: 19 are already in the Dell full-suite baseline (environmental). 3 read the repo's Dockerfiles, which aren't inside `/app` by design; the image was verified directly instead. 2 Talkdown tests fail identically in the **pre-change** c35d812 image, because the pinned starlette 0.41.3 test client cancels the server task. The real-server test above proves the production path. |
| Database/migration suites in the image | **PASS**: 22 passed, 2 skipped |
| Full VMS suite, Dell (commit f5a6d87) | 87 failed / 4166 passed / 124 skipped, identical to the baseline (plus 1 known intermittent test) |
| LPR synthetic OCR (software only) | **Pipeline functional**: capability enabled and available, and tesseract returns plate text with confidence. Accuracy is **not** validated, because synthetic text in a generic font gave 1-character errors. Physical LPR validation is pending. |

## 8. Exact cutover procedure (Samsung Claude)

Rules while doing this:
- Run each block, check its output, and stop on any FAIL.
- Never print secrets, RTSP URLs or credential values.
- Do not touch the QIXIANG, any camera, or the Ryzen.
- `ART=/home/alejandro-mata/samsung-step3`: copy `E:\samsung-step3\*` from the thumbdrive there.

### Phase A: preflight, artifacts and a fresh backup (0.9.0 keeps running)
```bash
ART=/home/alejandro-mata/samsung-step3; cd "$ART"
# 0.9.0 baseline must be healthy before starting
curl -fsS http://127.0.0.1:8000/health | grep -q '"build_id":"f613b2be' && echo "0.9.0 healthy"
df -h / | tail -1          # need >= 8 GB free (image load ~3.2 GB + backups)
sudo bash step3-verify-artifacts.sh "$ART"      # checksums, docker load, image ID, installer unpack -> must print PASS twice
# Fresh pre-step3 backup (in addition to ~/anyaicam-legacy-backup-20260926-204743)
TS=$(date -u +%Y%m%dT%H%M%SZ); B=/home/alejandro-mata/anyaicam-step3-backup-$TS; mkdir -p "$B"
sudo tar czf "$B/pre-step3-config.tgz" /opt/anyaicam/docker-compose.yml /etc/anyaicam /opt/anyaicam-agent /etc/systemd/system/anyaicam-agent.service /etc/systemd/system/anyaicam-vms.service /etc/systemd/system/anyaicam-privileged-watcher.* 2>/dev/null
sudo tar czf "$B/pre-step3-state.tgz" --exclude=/var/lib/anyaicam/vms/recordings/camera* --exclude=/var/lib/anyaicam/vms/recordings/clips --exclude=/var/lib/anyaicam/vms/recordings/media /var/lib/anyaicam
sudo sqlite3 /var/lib/anyaicam/vms/recordings/partner_portal.db ".backup '$B/partner_portal.db.pre-step3'" 2>/dev/null || sudo cp -p /var/lib/anyaicam/vms/recordings/partner_portal.db "$B/partner_portal.db.pre-step3"
( cd "$B" && sudo sha256sum * | sudo tee SHA256SUMS >/dev/null && sudo sha256sum -c SHA256SUMS )
docker tag anyaicam-vms:latest anyaicam-vms:0.9.0-legacy     # extra name for the legacy image; :latest itself is never changed
docker image inspect anyaicam-vms:0.9.0-legacy --format '{{.Id}}'   # expect sha256:4e813038b6a4...d2a3
echo "BACKUP=$B"
```

### Phase B: VMS cutover to the self-contained image (about 1–2 min of VMS downtime)
```bash
ART=/home/alejandro-mata/samsung-step3
sudo systemctl stop anyaicam-vms                        # compose down of 0.9.0
sudo cp -p /opt/anyaicam/docker-compose.yml /opt/anyaicam/docker-compose.yml.legacy-0.9.0
sudo install -m 0644 -o root -g root "$ART/docker-compose.samsung.yml" /opt/anyaicam/docker-compose.yml
sudo bash "$ART/step3-vms-env.sh"                       # prints its backup path; secrets untouched
sudo systemctl start anyaicam-vms                       # compose up -d with the pinned image (never builds/pulls)
for i in $(seq 1 24); do [ "$(docker inspect -f '{{.State.Health.Status}}' anyaicam-vms)" = healthy ] && break; sleep 10; done
sudo bash "$ART/step3-smoke.sh"                         # must end "12 passed, 0 failed"
```
Stamp the release marker; `validate.sh` and the operator both read it:
```bash
sudo tee /etc/anyaicam/vms_release.json >/dev/null <<'EOF'
{
  "vms_release_commit": "f5a6d875c5cc9bd313e85ac7a330359f39255150",
  "release_archive_sha256": "aaa33eebf12a7d1b0d34d6dbbcf7925b888fb95c60570832ed3ef96555eb4826",
  "installer_source_commit": "f5a6d875c5cc9bd313e85ac7a330359f39255150",
  "mediamtx_included": "false",
  "mediamtx_sha256": "",
  "installer_version": "1.1.0",
  "installed_at": "REPLACE_WITH_date_-u_+%Y-%m-%dT%H:%M:%SZ",
  "delivery": "self-contained image anyaicam-vms:f5a6d87 sha256:d5cd5fc263445dd18ce75e06bbc053feec935bb31f45ec0f53ab934babe5054b"
}
EOF
sudo sed -i "s/REPLACE_WITH_date_-u_+%Y-%m-%dT%H:%M:%SZ/$(date -u +%Y-%m-%dT%H:%M:%SZ)/" /etc/anyaicam/vms_release.json; sudo chmod 0644 /etc/anyaicam/vms_release.json
```
If any check fails, go to **Rollback R1** immediately.

### Phase C: agent upgrade and stale-queue archive (VMS not affected)
```bash
ART=/home/alejandro-mata/samsung-step3; B=<BACKUP dir from Phase A>
sudo systemctl stop anyaicam-agent
# Stale backlog (1,790 empty heartbeats/camera syncs for a dead endpoint): archive, never replay.
sudo mv /var/lib/anyaicam/offline_queue.db "$B/offline_queue.db.stale-legacy-endpoint"
sudo rsync -a --delete "$ART/anyaicam-release-f5a6d87/payload/agent/" /opt/anyaicam-agent/source/
sudo bash /opt/anyaicam-agent/source/scripts/install.sh     # reinstalls venv pkg + units + watcher; restarts the agent
sudo systemctl stop anyaicam-agent                           # keep it stopped until Phase D (its identity still points at the dead cloud)
/opt/anyaicam-agent/venv/bin/python -c "import anyaicam_agent,os;print(anyaicam_agent.__file__)"
sudo test -f /var/lib/anyaicam/offline_queue.db && sudo mv /var/lib/anyaicam/offline_queue.db "$B/offline_queue.db.after-upgrade" || true
```
Samsung then runs locally: the VMS is up and the agent is stopped. If Phase D isn't happening the same day, you can start the agent (`sudo systemctl start anyaicam-agent`); it will simply keep failing to reach the dead cloud, as today. Archive the queue again right before Phase D.

### Phase D: claim into the authoritative cloud (BLOCKED, needs your decision and your portal login)
```bash
sudo test -f /var/lib/anyaicam/offline_queue.db && sudo mv /var/lib/anyaicam/offline_queue.db "$B/offline_queue.db.pre-claim" || true
sudo -u anyaicam /opt/anyaicam-agent/venv/bin/anyaicam-setup --claim
#   Portal URL: https://portal-staging.anyaicam.com
#   Mode:       production
#   -> shows a claim code.
```
Within 15 minutes, you sign in to `https://portal-staging.anyaicam.com/customer/claim-appliance` as the **owner** of the chosen customer, enter the code, pick the site and confirm. The setup then prints the assigned customer/site, replaces the identity (with rollback on failure) and restarts the agent.

Then verify:
```bash
systemctl is-active anyaicam-agent anyaicam-vms
sudo journalctl -u anyaicam-agent --since "10 min ago" | grep -ciE "timed out|error"   # expect 0 / only transient
sudo bash "$ART/step3-smoke.sh"
```
On the Dell: staging should show the new Samsung appliance `online`, with a recent `last_check_in` and software_version reported.

## 9. Exact rollback procedure

The legacy image `anyaicam-vms:latest` (`sha256:4e813038…d2a3`) is **never re-tagged or deleted**, and it's also tagged `anyaicam-vms:0.9.0-legacy`. The `/opt/anyaicam/app` source tree and the Dockerfiles stay on disk untouched. The older `~/anyaicam-legacy-backup-20260926-204743` (sha256-verified) remains the last-resort backup.

**R1: VMS back to 0.9.0.** Use after a Phase B failure, or at any time.
```bash
B=<BACKUP dir from Phase A>
sudo systemctl stop anyaicam-vms
sudo cp -p /opt/anyaicam/docker-compose.yml.legacy-0.9.0 /opt/anyaicam/docker-compose.yml
sudo cp -p "$(ls -1t /etc/anyaicam/vms.env.pre-step3-* | head -1)" /etc/anyaicam/vms.env
sudo cp -p "$B/partner_portal.db.pre-step3" /var/lib/anyaicam/vms/recordings/partner_portal.db   # undo the 16->41 migration
sudo chown 997:983 /var/lib/anyaicam/vms/recordings/partner_portal.db
docker image inspect anyaicam-vms:latest --format '{{.Id}}'    # must be sha256:4e813038...d2a3 ; if missing: gunzip -c ~/anyaicam-legacy-backup-20260926-204743/vms-image.tar.gz | docker load
sudo systemctl start anyaicam-vms
curl -fsS http://127.0.0.1:8000/health    # expect "version":"0.9.0","build_id":"f613b2be..."
```
The legacy compose uses `build: .`. If Compose decides to rebuild because the image is missing, load the image from the backup first, as shown above, so that it never builds.

**R2: agent back to the 0.9.0 state.** Use after a Phase C problem.
```bash
B=<BACKUP dir from Phase A>
sudo systemctl stop anyaicam-agent
sudo tar xzf "$B/pre-step3-config.tgz" -C / opt/anyaicam-agent etc/systemd/system/anyaicam-agent.service
sudo tar xzf "$B/pre-step3-state.tgz" -C / var/lib/anyaicam/offline_queue.db var/lib/anyaicam/credential.json 2>/dev/null
sudo systemctl daemon-reload && sudo systemctl start anyaicam-agent
```

**R3: identity back to the legacy identity** (after Phase D). This isn't useful operationally, because the legacy cloud is dead, but it's exact:
```bash
sudo systemctl stop anyaicam-agent
sudo tar xzf "$B/pre-step3-config.tgz" -C / etc/anyaicam
sudo tar xzf "$B/pre-step3-state.tgz" -C / var/lib/anyaicam/credential.json var/lib/anyaicam/vms/recordings/appliance_identity.json
sudo systemctl start anyaicam-agent && sudo systemctl restart anyaicam-vms
```
The new staging appliance record would remain in staging. Leave it; revoking it is a separate, deliberate admin action.

## 10. Deferred (not part of Step 3)
- **WebRTC/P2P live view** needs:
  - the MediaMTX binary, which is in the installer payload and checksum-verified (`9fac297a…01ad`);
  - the `/opt/anyaicam/mediamtx:ro` mount and published UDP 8189;
  - the `anyaicam-webrtc-firewall` service.

  HLS Live View works without it. It's a separate, reversible follow-up.
- **Cameras:** Samsung has none. `/ready` becomes true once a camera records. Physical LPR, PPE and People Counting validation happen with real cameras.
- **Minor:** the legacy compose label `com.docker.compose.image=sha256:082fa294…` is stale metadata from the 0.9.0 era and goes away once the new compose recreates the container.
