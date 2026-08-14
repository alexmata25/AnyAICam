# AnyAiCam — AI Handoff / Architecture Source of Truth

Maintainers: update this file at the end of every significant session, before context is lost. This file — plus committed code and Git history — is the source of truth. Do not reconstruct architecture decisions from chat memory; read this file first.

Last updated: 2026-08-13 (Claude Code audit session, took over from Codex)
Repository: `alexmata25/AnyAICam` (GitHub)
Local working copy audited: `C:\Users\Alejandro Mata\OneDrive\Desktop\AnyAiCam-VMS`
Branch at time of audit: `build/v1.2-modular-foundation` (HEAD, clean working tree, in sync with `origin/build/v1.2-modular-foundation`)

Related docs (do not duplicate, read alongside this file):
- `docs/GITHUB_MIGRATION_REPORT.md` — history of what was untracked/cleaned when the repo was prepared for GitHub (2026-08-06). Explains why many `main_before_*` / `*.backup.py` snapshots exist locally but are gitignored.
- `docs/MODULARIZATION.md` — rules and status for extracting `app/main.py` into `app/api`, `app/services`, `app/edge`, `app/drivers`, `app/platform_core`. Extraction has not materially started; those packages are still near-empty scaffolding.
- `deploy/AWS-READINESS.md` — staging/production checklist (secrets manager, S3, HTTPS, Postgres). Written before the appliance-to-cloud media transport gap below was identified; does not yet describe it.

---

## 1. Project goal (do not re-derive this from memory — this is the standing target)

AnyAiCam is a VMS with two runtime roles that share one codebase:

1. **Customer appliance / edge VMS** — runs on a customer-owned host on the customer's LAN (brand/model irrelevant). Only the appliance talks to cameras (ONVIF/RTSP). Camera IPs and credentials must never leave the LAN. The appliance pushes video/events/health/recordings to AWS over an outbound connection; no inbound port-forwarding into the customer LAN.
2. **AWS/cloud VMS application** — users (customer/technician/salesperson/admin/partner) authenticate against AWS, never against the appliance directly. AWS applies roles/tenant/customer/site permissions, associates each account with its appliance(s), and serves video/data received from the correct appliance. Cloudflare + `app.anyaicam.com` currently front the public app but must not define the core appliance architecture.

Cloud ID / activation-token appliance enrollment exists today and is **not deleted**, but it is explicitly **not** the target end-state. The target is automatic, secure machine-identity-based appliance enrollment with no customer-facing manual ID entry. Treat the current Cloud ID implementation as legacy/reference until specific pieces are confirmed reusable.

---

## 2. What already exists (audited 2026-08-12) — do not rebuild these

The codebase is a single FastAPI app (`app/main.py`, ~16,200 lines, one file) plus a separate `appliance-agent/` Python package. Both are far more developed than "just camera streaming" — there is a full multi-tenant SaaS platform here.

**Confirmed working / reusable:**
- Local camera pipeline: ONVIF/RTSP discovery → FFmpeg → local HLS (`/static/hls/cameraN.m3u8`) → browser playback via hls.js, plus segmented `.mkv` recording, retention cleanup, motion detection, YOLO-based person detection, snapshots.
- **`ANYAICAM_RUNTIME_ROLE` (`edge` / `cloud` / `combined`)** already exists in `app/main.py` (introduced by Codex, not yet documented anywhere else). When `RUNTIME_ROLE=cloud`, the FFmpeg camera-pulling supervisors, motion detection, and AI detection tasks are **not started at all** — the app already knows it should not try to reach LAN cameras when running in AWS. `ecs-task-definition.json` (tracked, committed) already sets `ANYAICAM_RUNTIME_ROLE=cloud` for the ECS deployment. **This means the "AWS tries to pull 192.168.x.x directly" bug reported in production is not how the tracked code is configured to run** — it indicates the live EC2 instance is either not running from this ECS task definition, or has `CAMERA*_HOST` / role env vars set out-of-band directly on the instance, diverging from what's committed. This drift needs to be confirmed against the actual EC2 environment before any further conclusion.
- **Recording → S3 upload pipeline is fully implemented**, not a stub: `app/main.py` has `cloud_upload_worker()` (an asyncio task, started regardless of `RUNTIME_ROLE`) that scans `RECORDINGS_FOLDER` for finished `.mkv`/`.mp4` files, queues them (`cloud_upload_queue.json`, durable across restarts), and uploads via `boto3` with retry/backoff, multipart transfer, SSE-S3 or SSE-KMS, configurable storage class, a local `cloud_recording_index.json` of what was uploaded, and optional local deletion after upload. Gated by `ANYAICAM_CLOUD_UPLOAD_ENABLED`. **This is real appliance→AWS media transport — for finished recordings only, not live view.**
- `app/object_storage.py`: clean `StorageBackend` abstraction (`LocalStorage` / `S3Storage`) for snapshots/thumbnails/clips/documents/partner-materials, with presigned URL support. Reusable as-is.
- `app/cloud_config.py`: single `Settings` dataclass reading all `ANYAICAM_*` env vars, with environment-aware validation (rejects insecure prod config). Reusable, well-structured.
- `app/cloud_security.py`: `ProductionSecurityMiddleware` (CORS/CSRF/CSP/HSTS/security headers), login lockout, password reset. Reusable.
- **Appliance identity/auth model in `app/appliance_cloud.py` is solid and reusable even if Cloud ID as a customer workflow goes away**: appliances are rows keyed by `cloud_id`, activated via a one-time hashed activation token into a long-lived hashed bearer credential; every appliance API call requires `X-Appliance-ID` + `X-Request-Timestamp` + `X-Request-Nonce` + `Authorization: Bearer <credential>`, checked against a replay-nonce table and a ±300s time window. This mutual-auth pattern (nonce+timestamp+hashed bearer secret) is the right shape for the future automatic machine-identity mechanism — only the *enrollment* step (customer typing a Cloud ID) needs replacing, not the request-authentication mechanics.
- Cloud-side DB model already has `appliances`, `appliance_credentials`, `appliance_activation_tokens`, `appliance_request_nonces`, `appliance_health_history`, `appliance_camera_status`, `appliance_events`, `cameras`, `customers`, `sites`, `partner_id` relations (`partner_db.py` / `db_migrations.py`). Tenant/customer/site/appliance/partner modeling already exists — reusable.
- `appliance-agent/` (separate Ubuntu-installable Python package, `anyaicam_agent`): confirmed **control-plane only**. It POSTs heartbeat/metrics, camera status JSON, discovery scan results, and polls for admin commands (`restart_service`, `refresh_cameras`, `run_diagnostics`, `install_update`) over plain HTTP(S) JSON to `portal_url`, using the same bearer/nonce auth as above. It explicitly strips `username`/`password`/`rtsp_url`/`credentials`/`secret` keys from every outbound payload (`FORBIDDEN` set in both `appliance-agent/anyaicam_agent/portal.py` and `app/appliance_protocol.py`). Offline requests queue in a local SQLite queue with exponential backoff. **It never transports video/media in any form.** It is a separate process from the FastAPI VMS app itself (which is what actually runs FFmpeg).
- User accounts, partner/customer/admin roles and permissions, pricing/billing (Stripe), notifications, PWA/service-worker, and a large HTML/JS UI — all implemented inside `app/main.py` and its sibling modules (`partner_portal.py`, `customer_platform.py`, `pricing_portal.py`, `notification_engine.py`, etc.).
- Test coverage exists under `tests/`, `app/tests/`, and `appliance-agent/tests/` (deployment security, partner/customer flows, PWA, appliance protocol, cloud readiness, HTTP range parsing, module registry).
- Recent committed Codex fixes (verify before assuming stale): `app/cloud_security.py` logout CSRF exemption (commit `8f72b36`, 2026-08-08), plus the large repo-hygiene pass in commit `45cabb4` (2026-08-07) that deleted 780k+ lines of duplicated `main_before_*`/`.backup` snapshots from tracking (kept locally, gitignored) and consolidated `app/main.py` into the single canonical file. HLS/hls.js/CSP-worker/service-worker playback fixes mentioned by the user were not isolated to a single commit in this audit — verify current playback behavior against a running instance before further edits, since large parts of the frontend are inline JS strings inside `app/main.py`.

**What does NOT exist yet (this is the real gap):**
- **No live-media relay from appliance to AWS.** The camera-detail and dashboard pages always request `/static/hls/cameraN.m3u8` (or `/hls/cameraN.m3u8`) from whatever host rendered the page — there is no per-appliance routing, no cloud-side proxy, no WebRTC/RTMP/SRT ingest, nothing that lets a browser on `app.anyaicam.com` watch a live camera whose FFmpeg process is running on the customer's appliance. When `RUNTIME_ROLE=cloud`, the cloud process correctly refuses to pull cameras directly, but nothing replaces that live stream — the cloud-rendered pages would simply have no live video.
- **No secure outbound tunnel/connection primitive** (e.g., persistent mTLS/WebSocket/MQTT/QUIC channel, or a managed AWS IoT-style channel) for the appliance to push live media or receive commands without inbound port-forwarding. The only outbound channel that exists today is the `appliance-agent`'s periodic HTTP polling (control-plane) and the FastAPI app's own S3 upload worker (finished recordings only).
- **No automatic/machine-identity appliance enrollment.** Enrollment today is manual: an admin (or the appliance) generates/receives a Cloud ID + activation token, and either an operator types it into `/cloud-id` in the customer portal or the agent's `anyaicam-setup` wizard accepts it (manual paste or QR scan). This is exactly the legacy workflow the target architecture wants to eliminate — but the underlying credential/auth mechanics (hashed bearer token + nonce + timestamp) are reusable for whatever replaces the enrollment UX.

---

## 3. The broken network assumption, precisely

`app/main.py`:
```python
def camera_url(camera_number: int) -> str:
    host = os.environ[f"CAMERA{camera_number}_HOST"]
    ...
    return f"rtsp://{username}:{password}@{host}:554{path}"
```
`start_live_stream()` and `start_recording()` both call this and shell out to `ffmpeg` directly against that RTSP URL. This is correct **only** when the process running it is physically on the customer LAN. `RUNTIME_ROLE=cloud` prevents these functions from ever being scheduled in AWS (see §2), so the tracked code already avoids the literal crash/hang described — but it does so by simply not streaming, not by routing to the appliance. Confirm which `RUNTIME_ROLE` and `CAMERA*_HOST` values the live EC2 instance is actually running with; the reported symptom (EC2 trying to reach `192.168.x.x`) implies the deployed instance is not on `RUNTIME_ROLE=cloud`, or has camera env vars injected in a way this audit could not see (outside the repo).

**Production check result (2026-08-13) — drift CONFIRMED, not fixed yet:** the user ran the read-only drift-check checklist from §8 against the live instance directly.

- **EC2 instance:** `i-0f0fb6a78871b20d4`.
- **Confirmed:** the running container has `ANYAICAM_RUNTIME_ROLE=edge` — **not** `cloud`, contradicting the tracked `ecs-task-definition.json` (which sets `ANYAICAM_RUNTIME_ROLE=cloud`) and confirming this instance is not actually running from that committed task definition, or that env var is being overridden out-of-band. This is exactly the drift §3/§6 flagged as "possible" — it is now **confirmed**, not hypothetical.
- **Confirmed:** the container has `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, and `ANYAICAM_S3_BUCKET` set as environment variables (values not captured/recorded here — no secret values were shared in this session). The presence of `AWS_SESSION_TOKEN` alongside the access key suggests these may be temporary credentials that were manually obtained and injected, rather than a permanent IAM user's static keys — but since no IAM instance profile is attached (next point), nothing rotates these automatically; someone would need to manually refresh them before expiry. This is the same class of concern as the existing Security Hardening TODO in §8 (recording uploader relying on credentials present directly in the box's environment), now confirmed present on this specific production instance.
- **Confirmed:** `http://169.254.169.254/latest/meta-data/iam/security-credentials/` returned HTTP 404 from the instance itself — **no IAM instance profile is currently attached to this EC2 instance.** AWS CLI is not installed on the instance, so no AWS-side `describe-instances`/`describe-iam-instance-profile-associations` call has been run yet to independently confirm this from outside the box.

**This drift is flagged for a later, separate, explicitly-authorized fix. Nothing about production was changed while gathering or recording this information.**

---

## 4. Target flow (approved direction, not yet implemented)

```
Cameras (ONVIF/RTSP, LAN-only)
  → customer appliance (FFmpeg local HLS/recording — already works, keep as-is)
  → [NEW] secure outbound appliance-to-AWS media/data connection
  → AWS VMS/backend (already has: tenant/customer/site/appliance DB model,
    S3 recording pipeline, auth middleware, billing, roles)
  → authenticated/authorized AnyAiCam web app user (already works for
    everything except live camera view when appliance ≠ AWS host)
```

The only architecturally missing piece is the **[NEW]** link: a deliberately designed, outbound-initiated, no-inbound-port-forwarding channel that carries (a) live media (or low-latency near-live segments) from appliance to AWS, and (b) can eventually replace/extend the existing control-plane polling and Cloud ID enrollment with automatic machine identity. Recordings already have a working (non-live) path via the S3 upload worker; live view does not.

This flow, the phase plan below, and the "keep vs. legacy" lists are **pending user approval** before any implementation begins.

---

## 5. Files audited and their status

**Will likely need changes for the new transport:**
- `app/main.py` — `camera_url()`, `start_live_stream()`, `start_recording()`, the `RUNTIME_ROLE` branch in `lifespan()`, and every inline JS block that hardcodes `/static/hls/cameraN.m3u8` (grep found >8 occurrences, several deep inside f-string HTML). Given the file's size, plan to touch this surgically, not wholesale — it is the single biggest risk in this codebase.
- `app/appliance_cloud.py` / `app/appliance_protocol.py` — extend or replace to carry a media/session-negotiation endpoint alongside the existing heartbeat/events/commands endpoints.
- `appliance-agent/anyaicam_agent/service.py` / `portal.py` — currently control-plane only; the appliance-side piece that will need to open/maintain the new outbound media channel likely lives here or in a sibling module, not by turning this control-plane agent into a media relay wholesale.
- `deploy/*.env*.example`, `ecs-task-definition.json` — new transport config (endpoints, ports, protocol) once decided.

**Should not need changes (reuse as-is):**
- `app/object_storage.py`, `app/cloud_config.py`, `app/cloud_security.py`, `app/pwa_routes.py`, `partner_db.py`/`db_migrations.py` schema, the recording→S3 `cloud_upload_worker` pipeline, billing/pricing/notification modules, existing tests.

---

## 6. Risks noted during audit

- `app/main.py` is a single 920KB / ~16,200-line file. Any change here is high-blast-radius; the `app/api`/`app/services`/`app/edge`/`app/drivers` extraction described in `docs/MODULARIZATION.md` has barely started (only `app/api/http_range.py` and `app/platform_core/module_registry.py` exist, and per that doc are "intentionally not wired into production yet"). Recommend extracting the camera/streaming responsibility into `app/edge` and `app/drivers` as part of (not before) building the new transport, per the doc's own migration rules (one responsibility at a time, characterization tests first, preserve routes/env var names).
- 14 tracked historical "phase snapshot" copies of `main.py` still live under `app/` (`main_phase6f_single_override.py` … `main_v1_1_1_camera_verification_override.py`). They are committed, not imported by anything live, and add confusion/repo weight. Not touched during this audit; flag for a future cleanup decision (separate from architecture work).
- ~~Possible~~ **CONFIRMED (2026-08-13, see §3)** drift between the committed `ecs-task-definition.json` (`ANYAICAM_RUNTIME_ROLE=cloud`) and the live EC2 instance (`i-0f0fb6a78871b20d4`), which is actually running `ANYAICAM_RUNTIME_ROLE=edge` with AWS credentials injected as plain environment variables and no IAM instance profile attached. Flagged for a later, separate, explicitly-authorized fix — not corrected now.
- Cloud ID is wired fairly deep into customer-facing UI (`/cloud-id` page, onboarding checklist copy, admin review workflow for `cloud_id_status`) inside `app/main.py`. Removing/replacing it is a UX and DB-migration change, not just a backend swap — scope it explicitly when the new enrollment mechanism is chosen.

---

## 7. Proposed implementation phases (not started — awaiting approval)

1. **Confirm production drift.** Verify what `ANYAICAM_RUNTIME_ROLE` and `CAMERA*_HOST` values the live EC2 instance actually runs with today, and how they're set (ECS task def vs. manual instance `.env`). Do not change production during this step.
2. **Decide the transport primitive** for appliance→AWS live media (candidates to evaluate: WebRTC/SFU ingest, RTMP/SRT push to a cloud media server, MediaMTX/mediasoup-style relay, AWS Kinesis Video Streams, or a custom mTLS/WebSocket tunnel). This is the key decision to bring back to the user before writing code.
3. **Design appliance identity/enrollment replacement** on top of the existing nonce+timestamp+hashed-bearer-credential mechanics in `app/appliance_cloud.py`/`app/appliance_protocol.py`, keeping Cloud ID code intact but no longer the primary customer-facing path.
4. **Extract streaming responsibility** out of `app/main.py` into `app/edge`/`app/drivers` per `docs/MODULARIZATION.md` rules, in lockstep with adding the new transport (not as a separate big-bang refactor).
5. **Wire the cloud-side player** to the new transport instead of the hardcoded `/static/hls/cameraN.m3u8` paths, with per-appliance/per-camera routing.
6. **Update deployment config** (`ecs-task-definition.json`, `deploy/*.example`) and re-verify `RUNTIME_ROLE=cloud` behavior end-to-end against the new transport.
7. **Regression pass** against existing tests plus the recent HLS/hls.js/CSP/service-worker local-playback fixes to confirm nothing local regresses.

---

## 8. Architecture decision — appliance-to-AWS live media (APPROVED ARCHITECTURE — IMPLEMENTATION NOT YET AUTHORIZED — 2026-08-12)

**Status: APPROVED ARCHITECTURE — IMPLEMENTATION NOT YET AUTHORIZED.** The direction in this section is approved, following a code-level validation pass that corrected two errors in the original proposal (media container format, and a FastAPI-proxy data path that would have bottlenecked on API infrastructure). Implementation is a separate authorization — do not begin Phase 0 or any other code change against this section until the user explicitly authorizes implementation.

Seven transport options were compared (WebRTC/SFU, AWS KVS+WebRTC, SRT push, RTMP/RTMPS push, HLS segment relay, custom mTLS WebSocket/QUIC tunnel, AWS IVS) against latency, audio, browser playback, NAT friendliness, appliance CPU/bandwidth, AWS complexity/cost, multi-tenant routing, auth, recording integration, transcoding need, operational complexity, reconnect behavior, security, scale to hundreds/thousands of appliances, and — weighted heavily per explicit product priority — how much existing AnyAiCam code each option reuses.

### Recommended primary architecture

**Authenticated MPEG-TS HLS segment relay from appliance directly to S3**, with FastAPI acting as the control-plane authority (authentication, authorization, short-lived credential issuance) but explicitly removed from the media-byte data path. This is deliberately an *extension* of code that already exists, not a new media stack:

- **Media format, corrected against code:** the existing, tracked `start_live_stream()` (`app/main.py:1790–1799`) produces standard MPEG-TS HLS segments (`-f hls` with no `-hls_segment_type fmp4` override — FFmpeg's default container). The original proposal incorrectly called this "CMAF/HLS" / `.m4s`. **V1 keeps the existing `.ts` format as-is — the working FFmpeg pipeline is not modified.** CMAF/fMP4 + Low-Latency HLS is documented below as a possible *future* optimization, not part of this decision.
- **Data path, corrected against code:** segment bytes travel appliance → S3 → CloudFront → browser directly. FastAPI is never in the path of a video/audio byte; it only ever issues short-lived, narrowly-scoped upload credentials and receives small JSON control/metadata calls. This replaces the originally proposed `appliance → FastAPI POST → FastAPI writes → S3` proxy design, which would have coupled FastAPI/ECS scaling to aggregate concurrent live-viewer video bandwidth instead of to normal API load.
- The appliance-side uploader still mirrors the already-implemented `scan_recordings_for_cloud_upload()` / `cloud_upload_worker()` pattern (§2) in shape (watch a folder, queue, upload with retry), but uploads with short-lived STS-scoped credentials instead of the long-lived default-credential-chain pattern that pipeline currently uses.
- `ANYAICAM_CLOUDFRONT_URL` already exists in `cloud_config.py`/`main.py` and is already used to build a `cloudfront_url` for uploaded recordings — CloudFront-in-front-of-S3 is an already-anticipated, half-wired piece of this codebase, not a new idea.
- Appliance authentication for the new control-plane calls reuses `authenticate_appliance()` (bearer + nonce + timestamp + replay table) unchanged.
- Multi-tenant routing maps directly onto the existing `customers`/`sites`/`appliances`/`cameras` schema via an S3 key prefix (`live/{customer_id}/{site_id}/{appliance_id}/{camera_id}/...`), which now also doubles as the exact IAM policy condition scoping each issued credential — no new provisioning system.
- Recording upload (`cloud_upload_worker`) is completely untouched for this implementation — the live relay is a parallel path off the same local FFmpeg output, not a replacement. See the security hardening TODO below for a related, separate future improvement.
- ~6–10s latency (tunable toward 4–6s) is normal for VMS live view, not a compromise — sub-second WebRTC latency buys nothing customers will notice here, at the cost of running/scaling a media-server fleet or adopting a non-hls.js player SDK.
- **Publish model: hybrid, on-demand.** The appliance only uploads live segments for a camera while ≥1 authorized viewer has it open. See "Continuous vs. on-demand publishing" below for the exact sequence.
- AWS IVS (managed RTMPS ingest + auto fan-out) was the strongest alternative considered and is the documented future upgrade path if a customer segment later needs sub-second latency (e.g. PTZ control) — it can be layered in later behind the same authorization model without disturbing this recommendation. It was not chosen as primary because it introduces a whole new managed product surface (channel-per-camera provisioning, its own recording feature, ingest-hour billing that doesn't taper for unwatched cameras) that runs parallel to, rather than reusing, the S3 pipeline already built and tested.
- The `appliance-agent` control-plane package stays control-plane only, unchanged — the media relay lives in the VMS app process itself (the one already running FFmpeg on the appliance), not the separate lightweight agent.

### Control plane vs. media data plane (explicit separation)

```
CONTROL PLANE (small, authenticated JSON/HTTPS — always through FastAPI)

  customer appliance  <──────────────────────────────►  AWS FastAPI
                        - appliance authentication
                          (authenticate_appliance():
                          bearer + nonce + timestamp)
                        - tenant/customer/site/appliance/
                          camera authorization
                        - start_live_relay / stop_live_relay
                          (over the existing command-poll channel)
                        - relay session authorization
                        - short-lived media-upload credential
                          issuance (STS AssumeRole, scoped to
                          live/{customer}/{site}/{appliance}/{camera}/*)
                        - health / status / events / commands
                          (unchanged, existing endpoints)
                        - small manifest / segment-availability
                          metadata notifications (tiny JSON only)


MEDIA DATA PLANE (raw video/audio bytes — never touches FastAPI/EC2)

  camera ──(RTSP/ONVIF, LAN-only)──► appliance ──(FFmpeg,
  unchanged local HLS pipeline)──► local .ts segments
      │
      └──(direct authenticated PUT, short-lived STS credential)──► S3 (live/ prefix)
                                                                        │
                                                                        └──► CloudFront ──(signed URL/cookie)──► browser (hls.js, unchanged)
```

Raw video/audio segment bytes must not, and in this design do not, pass through FastAPI/EC2 at any point. FastAPI's only involvement with the media plane is deciding *whether* to issue a credential and *what prefix* it's scoped to — never touching the bytes it authorizes.

### Full media path

**Video path:**
1. Camera → ONVIF/RTSP → appliance. *(unchanged, LAN-only, credentials never leave the appliance)*
2. Appliance FFmpeg (existing `start_live_stream()`, unmodified) produces local MPEG-TS HLS video segments + `.m3u8`, exactly as it does today. *(unchanged — local viewing keeps working even if the cloud relay is degraded or disabled)*
3. **New, control plane:** when relay is active for a camera, the appliance requests/renews a short-lived, camera-scoped upload credential from FastAPI (which has already authenticated the appliance and authorized that camera).
4. **New, data plane:** an appliance-side segment-watcher task (same shape as `scan_recordings_for_cloud_upload()`) notices each newly-completed local `.ts` segment and, only while that camera's relay is active, `PutObject`s it **directly to S3** at `live/{customer_id}/{site_id}/{appliance_id}/{camera_id}/segment_%d.ts`, using the short-lived credential from step 3. FastAPI is not involved in this transfer.
5. **New, control plane (small):** the appliance (or, as a later optimization, an S3 Event Notification) sends a tiny JSON "segment available" call to FastAPI so it can update a small rolling manifest (the last N segment references) for that camera. This is metadata only, not the segment.
6. CloudFront, in front of the S3 `live/` prefix, serves the manifest and segments to the browser under a short-lived signed URL/cookie scoped to the requesting viewer's session.
7. Browser: the existing hls.js integration (`connectCamera()`) plays the CloudFront URL exactly as it plays the local URL today — same player, same buffering/liveSync settings, different `source`.

**Audio path (validated against code, `app/main.py:1790–1799`, `:7707`, `:7769–7783`):**
- `0:a:0?` — audio is mapped optionally (won't fail if a camera has no audio track).
- Codec: AAC.
- Bitrate: 96 kbps.
- Channels: mono.
- Sample rate: 48 kHz.
- Audio and video are muxed together in the same MPEG-TS HLS segments — there is no separate audio stream, protocol, or pipeline. The direct-to-S3 upload carries the same muxed `.ts` segment; nothing new is needed for audio specifically.
- **Existing main live-view unmute behavior is preserved as-is:** `<video id="camera{n}" autoplay muted controls playsinline>` (`app/main.py:7707`) starts muted for browser autoplay-policy compliance, with a working `toggleCameraAudio(n)` unmute control (`app/main.py:7769–7783`) already wired on this surface. Nothing about the relay changes this.
- **Open question, not yet resolved:** `dashboard-video-{camera_number}` (`app/main.py:8049`) and `monitor-video-{camera}` (`app/main.py:15641`) are also marked `muted` in the DOM; no corresponding unmute control was found near either during the validation pass. This needs verification before cloud-relay playback is wired into those surfaces — tracked here so it isn't lost, not resolved by this decision.

**Appliance authentication (control plane, per-request):** unchanged from what's audited in §2 for existing endpoints — bearer + nonce + timestamp + replay table via `authenticate_appliance()`. The new element is what FastAPI *does* after authenticating a relay request: instead of accepting media bytes, it authorizes the specific camera against the tenant/customer/site/appliance/camera model and, if authorized, issues a short-lived STS-based session (see "Short-lived scoped credentials" below) rather than opening an upload channel to itself. *(How the appliance first obtains its base credential — replacing manual Cloud ID entry with automatic machine identity — remains a separate, parallel decision; see §2 and §6.)*

**Browser playback:** unchanged from the original proposal — no new client-side library. The page already loads hls.js for local cameras (`connectCamera()`, and the analytics-rule preview player, both already `<script src="https://cdn.jsdelivr.net/npm/hls.js@latest">`). For a cloud-routed camera, the same function is given a CloudFront URL instead of `/static/hls/camera{n}.m3u8` as its `source`; hls.js's live-sync settings (`liveSyncDurationCount`, `liveMaxLatencyDurationCount`, `app/main.py:7791`) already configured for local playback carry over unchanged. The existing `waiting`/`playing`/`pause` event handlers and on-screen "Reconnecting…" state require no changes.

**Continuous vs. on-demand publishing (hybrid, preserved):**
1. Local HLS and local recording continue running exactly as they do today, regardless of relay state.
2. Cloud live upload for a camera starts only when at least one authorized viewer requests that camera.
3. The existing command/control flow sends `start_live_relay` to the appliance over the command-poll channel it already polls.
4. The appliance obtains (or renews, if already holding one for another camera) the scoped live-upload credential from FastAPI's control plane, and begins uploading newly-completed `.ts` segments directly to S3.
5. After the last viewer leaves, a short grace period (target ~30s, to absorb refreshes/tab switches without visibly restarting the stream) elapses, then FastAPI sends `stop_live_relay` and the appliance stops uploading; any outstanding credential is simply left to expire on its short TTL.

Rejected the "always push every camera to the cloud continuously" alternative because appliance uplink usage and S3/CloudFront cost would then scale with camera count instead of with actual viewership, which matters once the fleet reaches "hundreds/thousands of appliances."

**Latency expectations:** roughly 6–10 seconds end-to-end at the currently-configured 2-second local segment size plus hls.js's live-sync buffering (already set to a few segments), tunable down toward 4–6 seconds with shorter segments and tighter buffering if a later customer need demands it. This is normal for VMS live view — not a compromise — and is comparable to or better than typical commercial VMS cloud-viewing latency. It is explicitly not sub-second; if a future requirement (e.g., live PTZ joystick control) needs sub-second response, that's the trigger to layer in the AWS IVS or WebRTC path noted above as a future addition, not to redesign this one.

**Possible future optimization (not part of this decision):** switching the cloud-relay output specifically to CMAF/fMP4 (`-hls_segment_type fmp4`, `.m4s` segments) would enable standard Low-Latency HLS extensions and is more CDN-idiomatic than MPEG-TS. This is explicitly deferred — V1 keeps the existing, working `.ts` pipeline unmodified, and this would be evaluated later as its own scoped change, independent of and after this architecture ships.

**Reconnect/failure behavior:**
- *Appliance → S3 upload failure:* the segment-watcher reuses the existing `process_supervisor` restart pattern and the same offline-queue/exponential-backoff approach already used by `cloud_upload_worker` and the `appliance-agent`'s `OfflineQueue`. Unlike recordings, a failed or stale live segment is simply dropped rather than retried/backfilled — the next segment a few seconds later supersedes it, since "catching up" a live view has no value.
- *Credential expiry/renewal failure:* if the appliance can't reach FastAPI's control plane to renew its short-lived upload credential before it expires, uploads for that camera simply stop until connectivity/authorization is restored — the same visible effect as any other gap (below), with no separate failure mode to design for.
- *Cloud/CDN gap:* if segments stop arriving (appliance offline, network blip, relay stopped, credential expired), the manifest simply stops advancing. No new player-side code is needed — hls.js already surfaces this as its existing `waiting` state, and the frontend's existing `setState(n, 'Reconnecting…')` handler already fires on that event today for local cameras.
- *Recovery:* once segments resume arriving, playback resumes automatically the same way it already does for local live view today — no explicit "reconnect" handshake is required because HLS is inherently poll-and-catch-up.
- *Viewer-side network loss:* handled entirely by hls.js's existing retry/backoff behavior, unchanged.

**Short-lived, narrowly scoped AWS credentials for live media uploads (corrected design):** FastAPI authenticates the appliance (`authenticate_appliance()`, unchanged) and authorizes the requested camera against the existing tenant/customer/site/appliance/camera model, then issues a short-lived STS-based session (preferred over per-segment presigned URLs, since a viewing session uploads many segments over an unpredictable duration and re-requesting a URL every 2 seconds would just relocate the bottleneck). These credentials must:
- have a short TTL (target: single-digit minutes, renewable while the relay stays authorized/active),
- be renewable only while FastAPI continues to consider the relay session authorized and active,
- grant `PutObject` (and nothing else) restricted by IAM policy condition to exactly `s3://<bucket>/live/{customer_id}/{site_id}/{appliance_id}/{camera_id}/*` — no access to any other customer's, site's, appliance's, or camera's prefix,
- provide no public S3 access (bucket stays private, consistent with the "blocked public access, encryption... narrowly scoped application credentials" guidance already written in `deploy/AWS-READINESS.md`),
- grant only the minimum S3 permission needed for live upload — no `ListBucket`, no `GetObject`, no delete, no access to the `recordings`/other prefixes.

**AWS services required:**
- The existing FastAPI application / control plane (unchanged endpoints extended, no new service).
- AWS STS (`AssumeRole`) for short-lived credential issuance.
- A new, narrowly-scoped IAM role/policy for live upload, conditioned on the `live/{customer_id}/{site_id}/{appliance_id}/{camera_id}/*` prefix.
- S3 `live/` prefix (same bucket already used for recordings — no new bucket strictly required).
- CloudFront (a distribution in front of that prefix — the config surface, `ANYAICAM_CLOUDFRONT_URL`, already exists and is currently unused for this purpose).
- The existing database / customer / site / appliance / camera authorization model — unchanged, just consulted at credential-mint time instead of at segment-ingest time.
- No new compute service, no media server, no SFU, no signaling service, and no new AWS product surface to learn/operate beyond STS/IAM, which are foundational AWS services, not a new subsystem. Optional, only if segment volume later requires offloading manifest state from the existing database: DynamoDB for the rolling per-camera manifest, added later without changing the client-facing contract.

**Scaling and cost considerations:**
- *FastAPI/ECS scaling is intentionally decoupled from aggregate camera video bandwidth.* Because segment bytes go appliance → S3 directly, the FastAPI/ECS fleet only ever handles small authenticated JSON calls (auth, relay start/stop, credential issuance, segment-available notifications) — its sizing follows normal API/control-plane load, not concurrent live-viewer count or camera count. This is the specific mechanism that prevents the bottleneck identified in the validation pass, not just a general benefit of using managed AWS services.
- *Storage:* live segments are small (2s H.264/AAC `.ts` chunks) and short-lived — a rolling-window lifecycle policy on the `live/` prefix (e.g., expire objects after a few minutes) keeps storage cost negligible regardless of fleet size, since old live segments have no value once superseded.
- *Request volume:* with the on-demand publish model, PUT volume scales with concurrent *viewed* cameras, not total camera count — a fleet of thousands of appliances with typical VMS usage (most cameras watched rarely, mainly reviewed via already-uploaded recordings) keeps this small in practice. S3 request-rate scales well when keys are naturally sharded by customer/site/appliance/camera, which this prefix design already provides.
- *Egress:* CloudFront bills only when someone actually watches, the same "cost follows viewership" property as the publish model — this remains a key reason this option was preferred over AWS IVS, whose ingest-hour billing accrues whenever a channel is live regardless of viewer count.
- *Appliance bandwidth:* only cameras currently being viewed upload live segments, so a typical site (customer usually not actively watching most of the time) adds negligible sustained uplink load; peak load is bounded by however many cameras a given appliance's users are simultaneously viewing, which is small in practice.

### Security hardening TODO (separate from this decision, tracked for later)

The existing recording uploader (`cloud_upload_worker` / `upload_cloud_recording_job`, `app/main.py:1515–1614`) currently relies on AWS credentials available directly in whatever box's environment runs it — resolved via boto3's default credential chain (`boto3.client("s3", region_name=AWS_REGION)`, `app/main.py:1533`, no explicit keys passed) — and this worker starts unconditionally regardless of `RUNTIME_ROLE`, meaning appliance boxes running `RUNTIME_ROLE=edge`/`combined` need real, apparently long-lived AWS credentials present locally today. This is a pre-existing pattern, not introduced by this decision, and is out of scope to fix here. **Once the live-relay STS-based architecture above is built and proven in production, evaluate migrating the recording uploader to the same short-lived, narrowly-scoped credential pattern**, removing the need for static AWS keys to live on customer-premises hardware at all.

### Why this was selected over WebRTC, Kinesis Video Streams, IVS, and SRT

- **vs. WebRTC/self-hosted SFU:** WebRTC wins on latency (~200–500ms vs. ~6–10s) but requires building and operating an entirely new media-server cluster (SFU) plus TURN infrastructure, a new appliance-side publisher stack (FFmpeg alone doesn't speak WebRTC — needs GStreamer/whip or a dedicated library), and a new browser-side player (not hls.js). None of that reuses existing AnyAiCam code, and the latency improvement isn't needed for a security-camera live view (as opposed to a video call). Rejected as primary; noted as a possible future low-latency tier for specific use cases (e.g. PTZ control), added later behind the same authorization model rather than replacing this path.
- **vs. AWS Kinesis Video Streams + WebRTC (signaling channels):** solves signaling/TURN as a managed service, but doesn't solve fan-out — the appliance ("master") would need to sustain one WebRTC connection per concurrent viewer with no built-in SFU, which caps how many simultaneous viewers a single appliance's uplink can support and doesn't match "customer support agent, technician, and customer all wanting to check the same camera at once." It also requires a new AWS WebRTC SDK on both appliance and browser (not hls.js), and provides no recording integration reuse. Rejected for the live path for the same core reasons as self-hosted WebRTC, plus the fan-out limitation.
- **vs. AWS IVS (managed RTMPS ingest + CDN fan-out):** the closest AWS-native competitor, and the strongest alternative considered. It solves fan-out well (that's its whole purpose) and the FFmpeg change to push RTMPS is trivial. It was not chosen as primary because: (a) it requires standing up and maintaining a whole new per-camera-channel provisioning lifecycle synced against the existing `cameras` table, which is new code with no reuse; (b) its own recording feature would run parallel to, not reuse, the already-built and already-tested `cloud_upload_worker` S3 pipeline; (c) its ingest-hour billing accrues whenever a channel is live, independent of whether anyone is watching, which fits poorly with a large fleet of mostly-unwatched security cameras compared to S3/CloudFront's watch-driven cost. It remains the recommended path to add later if a customer segment specifically needs IVS's lower latency or built-in fan-out at very high concurrent-viewer counts per camera.
- **vs. SRT push (e.g., via AWS MediaConnect):** SRT's real strength is loss-resilient transport over poor/unreliable links, which isn't the primary problem being solved here (most appliance sites have ordinary business/consumer broadband, and RTMPS/HTTPS already tolerate that adequately). SRT also isn't natively browser-playable — it still needs a transcode/repackaging hop to HLS or WebRTC before a browser can show it, adding a second infrastructure component (MediaConnect plus something else) for no corresponding benefit here. Rejected as unnecessary complexity for the target network conditions; could be revisited later specifically for sites with known-poor uplinks if that becomes a real operational problem.
- **Common thread:** every rejected option requires either new appliance-side publisher software (WebRTC/KVS/SRT), a new AWS product surface with no code reuse (KVS/IVS), or solves a problem AnyAiCam doesn't currently have (SRT's loss resilience, WebRTC's sub-second latency) at the cost of infrastructure AnyAiCam would have to build and operate from scratch. The recommended option is the only one that is primarily an *extension* of already-shipped, already-tested code (`cloud_upload_worker`'s pattern, `ANYAICAM_CLOUDFRONT_URL`, `authenticate_appliance()`, hls.js) rather than a new subsystem.

### Reuse plan summary

**Unchanged:** ONVIF/RTSP capture, local FFmpeg HLS/recording (`.ts` container, not modified), `cloud_upload_worker` S3 pipeline and recording upload behavior, `object_storage.py`, `cloud_config.py`, `cloud_security.py`, DB schema, `authenticate_appliance()`, the `appliance-agent` package, existing tests.

**New, additive only:**
- A cloud-side **relay-session/credential endpoint** in `app/appliance_cloud.py` (e.g. `POST /api/appliance/live/{camera_id}/session`) that authenticates + authorizes via existing logic and returns a short-lived STS-scoped credential — replaces the originally-proposed segment-ingest endpoint; this one never receives media bytes.
- A small, separate **segment-notification endpoint** (tiny JSON, e.g. `POST /api/appliance/live/{camera_id}/segment-available`) for manifest bookkeeping — metadata only.
- A small manifest-maintenance module for the rolling per-camera live playlist.
- A new IAM role/policy for STS-issued, prefix-scoped live-upload credentials.
- An appliance-side segment-watcher/uploader task (mirrors `scan_recordings_for_cloud_upload()` in shape) that requests/renews credentials via the control plane and `PutObject`s directly to S3.
- A viewer-session signed-URL/cookie minter for CloudFront.
- Two new entries in `ALLOWED_COMMANDS` (`start_live_relay`, `stop_live_relay`, currently `{'restart_service','refresh_cameras','run_diagnostics','install_update'}`).
- A second URL source in the frontend's `connectCamera()`.
- An S3 `live/` prefix and an actual CloudFront distribution behind the already-existing `ANYAICAM_CLOUDFRONT_URL` config.

### Production drift check (read-only, for later)

`/health` (unauthenticated) already returns `runtime_role`, `version`, and `build_id` — the fastest way to check what the live EC2 instance is actually running is `curl -s https://app.anyaicam.com/health` and compare against `ecs-task-definition.json` and `git log` locally, before resorting to `aws ecs describe-task-definition`, `docker inspect ... | jq 'keys'` (names only, never values), or `env | grep -oE '^CAMERA[0-9]+_HOST'` on the instance itself. Nothing here has been executed against production.

### Implementation phases (approved direction — implementation not yet authorized)

0. Characterization tests for current local HLS/recording behavior (safety net, no behavior change).
1. IAM role/policy for scoped, short-lived live-upload credentials (prefix-conditioned on `live/{customer_id}/{site_id}/{appliance_id}/{camera_id}/*`); no application code yet.
2. Cloud-side control-plane endpoints: relay-session/credential issuance (STS `AssumeRole`) and the small segment-availability notification endpoint, plus the manifest-maintenance module — behind `ANYAICAM_LIVE_RELAY_ENABLED=false` by default. No media bytes touch this code path even in testing.
3. Appliance-side segment-watcher/uploader: requests/renews scoped credentials from the control plane and `PutObject`s `.ts` segments directly to S3, gated by `RUNTIME_ROLE in {edge, combined}` and a per-camera relay-active flag defaulting off. Verify local HLS/recording are unaffected and that FastAPI's request logs show no segment bytes.
4. Command-channel wiring (`start_live_relay`/`stop_live_relay`) triggered by the web app's "open camera" action; end-to-end test in staging with one appliance/one camera, confirming the on-demand start/stop + grace-period sequence.
5. S3 `live/` prefix lifecycle policy + CloudFront distribution + signed-URL/cookie minting; test CDN playback in staging.
6. Frontend wiring: `connectCamera()` picks cloud-relay vs. local URL by context; verify hls.js playback, existing reconnect UI, and the existing unmute control against the relayed stream.
7. Reconnect/failure hardening (segment drop-not-retry, credential-renewal failure, CDN gap) + idle-relay auto-stop; load test with multiple concurrent cameras/viewers, and specifically confirm FastAPI/ECS resource usage does not scale with concurrent live-viewer count.
8. Gradual production rollout behind the feature flag, one pilot appliance at a time, before flipping the default.

Each phase is one independently testable, independently revertable PR. Appliance identity/enrollment replacement (Cloud ID → automatic machine identity) and the recording-uploader security hardening TODO above are separate, parallel tracks and do not block this rollout.

### Phase 0 result — complete and APPROVED (2026-08-12)

Phase 0 (characterization tests only, no transport code) was authorized and completed. Four new test files were added under `app/tests/`, all test-only — no production file was modified:

- `test_live_stream_ffmpeg_characterization.py` — `camera_url()`'s LAN-only RTSP construction, and `start_live_stream()`'s exact FFmpeg command (video/audio mapping, AAC 96k/mono/48kHz, HLS output, 2s segments, list size 5, current MPEG-TS default container, current `hls_flags`). `subprocess.Popen` is mocked; no real ffmpeg process or camera is touched.
- `test_runtime_role_lifespan_characterization.py` — exercises the real `lifespan()` control flow (leaf worker coroutines mocked) to prove `RUNTIME_ROLE=cloud` starts zero camera supervisors while `edge`/`combined` each start one live + one recording supervisor per configured camera.
- `test_live_view_page_characterization.py` — calls `home()` directly to lock down the local HLS source pattern, the preserved hls.js live-sync settings, the primary live-view video starting muted, and the unmute control toggling `video.muted`. Named to sort after `test_cloud_readiness.py` — see the file's docstring for why.
- `test_recording_cloud_upload_characterization.py` — `start_recording()`'s FFmpeg command, `cloud_recording_s3_key()`'s key convention, `scan_recordings_for_cloud_upload()`'s discovery/queueing/gating, and `upload_cloud_recording_job()`'s S3 `upload_file()` call shape (bucket, key, content type, storage class, SSE). `boto3.client` is mocked; no real AWS call is made.

**Result: 45/45 tests pass** (25 new + 20 pre-existing in `app/tests/`), run twice from a clean state for determinism. Full command:
```
cd app && python -m unittest discover -s tests -p "test_*.py" -v
```

**Two unexpected findings surfaced while writing these tests, both flagged for a separate, explicit decision:**

1. **Production bug — FIXED in Phase 0.1 (2026-08-13):** `app/main.py` called `hashlib.sha256(...)` (lines 2262 and 3389, inside `sha256_file()`) but never imported `hashlib` anywhere in the module. `upload_cloud_recording_job()` calls `sha256_file()`, so the existing recording→S3 upload pipeline was crashing with `NameError` on every real upload attempt once `ANYAICAM_CLOUD_UPLOAD_ENABLED=true`. Fixed with a single added line, `import hashlib`, in the module's top-level import block (`app/main.py`) — no other production behavior was changed. The recording-upload characterization test (`test_recording_cloud_upload_characterization.py`) was updated to exercise the real `sha256_file()` (asserting the actual digest/size of a test file) instead of mocking it, while `boto3`/network stay mocked; it passes against the fix.
2. **Pre-existing test-suite fragility (not caused by Phase 0, but triggered by adding new files that import `main`) — still an open TODO:** `partner_db.py` runs `initialize_database()` (schema migrations) automatically the first time it's imported per process, while `app/tests/test_cloud_readiness.py` deletes and re-creates its own temp sqlite file at its own module import time. Any new test file that imports `main` (and so `partner_db`) earlier in discovery order freezes the migrated database to a different path than the one `test_cloud_readiness.py` just deleted, breaking its migrated-tables assertion. Currently worked around only by naming `test_live_view_page_characterization.py` to sort after `test_cloud_readiness.py` (documented in that file's docstring); no existing file was modified. **This remains a latent trap for any future test file and is a real cleanup TODO** (e.g., a `conftest.py`/fixture that controls DB initialization order explicitly, or removing the import-time side effect from `partner_db.py`) — not resolved by Phase 0 or Phase 0.1, intentionally deferred.

### Phase 0.1 result — complete (2026-08-13)

A small, separately-approved cleanup/checkpoint between Phase 0 and Phase 1:
- Fixed finding #1 above (`import hashlib` added to `app/main.py`; no other production behavior changed).
- Updated the recording-upload characterization test to exercise the real `sha256_file()` rather than mocking around the bug.
- Removed one incidentally-created file, `C:\app\recordings\license_state.json` — positively confirmed (its own `created_at` timestamp and file mtime matched, to the second, this session's earlier one-off diagnostic `import main` probe) to have been created by that probe and nothing else; every other file/directory under `C:\app\recordings` (all dated 2026-08-05, predating this session) was left untouched.
- Full `app/tests/` suite re-run after the fix: **45/45 passing.**
- Finding #2 (test-suite import-order fragility) remains an open, un-fixed TODO — intentionally out of scope for both Phase 0 and Phase 0.1.

### Phase 1 — EXECUTED and VERIFIED in AWS (2026-08-13)

**Status: IAM EXECUTED AND FUNCTIONALLY VERIFIED IN AWS. No application code has been written. Phase 2 has not started.** This section is the IAM design for scoped, short-lived live-upload credentials, per implementation phase 1 in this section's phase list above. The design below was created for real in the AWS account and functionally verified — see "Phase 1 execution result — VERIFIED" near the end of this section for the exact evidence. This is no longer a plan-only section.

**Decisions (approved, corrected from the initial draft):**
1. **One IAM role** for all AnyAiCam live-media uploads — not one role per camera/appliance/customer.
2. **STS `AssumeRole` with an inline session policy** narrows each temporary session down to exactly `live/{customer_id}/{site_id}/{appliance_id}/{camera_id}/*`. The role's own base policy may permit the broader `arn:aws:s3:::anyaicam2026/live/*`; the session policy passed at `AssumeRole` time is what actually intersects that down to one exact camera prefix per credential. (Session policies can only narrow an assumed role's permissions, never widen them.)
3. **Same S3 bucket already configured by `ANYAICAM_S3_BUCKET`** — no new bucket. Live media uses a new `live/` prefix; existing recordings stay exactly where `cloud_upload_worker`/`cloud_recording_s3_key()` already put them, untouched.
4. **Credential lifetime: `DurationSeconds = 900`** (15 minutes) for V1. Credentials may be renewed by the appliance while its live relay session remains authorized and active (per the existing `start_live_relay`/`stop_live_relay` control-plane flow already documented above) — renewal is a Phase 2 application-code concern, not part of this IAM design.
5. **No `sts:ExternalId` for V1.** This is a same-account architecture; the explicit trusted principal in the trust policy is the FastAPI/ECS task role itself. Revisit `ExternalId` only if a cross-account or third-party principal is introduced later.
6. **Minimum live-upload permissions: `s3:PutObject` only.** Explicitly no `ListBucket`, no `GetObject`, no `DeleteObject`, no access to the `recordings/` prefix, and — enforced by the per-request session policy, not just the base role policy — no access to any other camera's, appliance's, site's, or customer's prefix.
7. **Session naming:** CloudTrail-readable, based on appliance/camera identity plus a unique/session value (e.g. the `{appliance_id}-{camera_id}-{timestamp}` shape suggested during planning). The exact format is a Phase 2 concern — it must be validated against AWS's `RoleSessionName` character/length rules when the `AssumeRole` call is actually implemented, not decided here.
8. **No ABAC / session tags for V1.** The inline session-policy design is used instead — simpler, and it expresses the exact S3 resource prefix directly rather than through tag-matching conditions in the role's own policy.
9. **The trust-policy `Principal` — path chosen 2026-08-13, concrete ARN still unresolved (nothing created yet).** Between the two remediation paths originally identified, the standing V1 decision is now: **keep the current Docker-on-EC2 deployment and use an EC2 instance profile as the AWS application identity** (path (a) below) — ECS migration (path (b)) is explicitly deferred as a separate, later, independently-authorized decision, not part of this design.
   - **(a) — CHOSEN for V1.** Create a dedicated EC2 application IAM role, attach it to `i-0f0fb6a78871b20d4` via an instance profile, and use *that role's* ARN as the live-upload role's trust-policy principal. Full two-role design in "EC2 application identity for the current Docker-on-EC2 deployment" below.
   - **(b) — deferred, not decided now.** Migrate this deployment to actually run via the already-committed `ecs-task-definition.json` (which already assumes `ANYAICAM_RUNTIME_ROLE=cloud` and implies an ECS task role). Would also resolve the `RUNTIME_ROLE=edge`-in-production drift (§3/§6) as a side effect, which is worth remembering if this is revisited later — but is not being done now.
   
   The concrete ARN this produces is still unresolved because **nothing in AWS has been created yet** — this section only documents the approved design and the exact resources/commands that *would* create it, pending separate execution authorization.

### EC2 application identity for the current Docker-on-EC2 deployment (chosen path, APPROVED design, not yet applied — 2026-08-13)

Standing V1 decision: **keep the current Docker-on-EC2 deployment; do not migrate to ECS in this phase.** The AWS application identity is a dedicated EC2 instance profile attached to `i-0f0fb6a78871b20d4`. Two IAM roles, not one:

- **A — EC2 Application Role.** The machine identity for the AnyAiCam AWS-side application itself. Its only permission is `sts:AssumeRole` on role B below — deliberately **no broad S3 permissions**, so a compromise of the application process alone cannot read/write S3 directly, only obtain a further-scoped, short-lived, `live/`-only upload session.
- **B — Live Upload Role** (`anyaicam-live-relay-upload`, as already designed above). Trust principal is role A's ARN, not a generic placeholder. Base permissions and per-camera session-policy narrowing are unchanged from the design already approved above.

Explicitly **not doing in this step:** creating anything in AWS, changing `ANYAICAM_RUNTIME_ROLE`, touching Cloudflare/camera config, or migrating to ECS. This is design only.

#### A1. EC2 Application Role — trust policy (trusts the EC2 service, so the instance can assume it)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ec2.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

#### A2. EC2 Application Role — permissions policy (may ONLY assume the live-upload role; no direct S3 access)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AssumeLiveUploadRoleOnly",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::880690594006:role/anyaicam-live-relay-upload"
    }
  ]
}
```
`<AWS_ACCOUNT_ID>` resolved 2026-08-13, obtained read-only from the AWS account, not guessed: `880690594006`.

#### B1. Live Upload Role — trust policy (updates the earlier generic template: the principal is now role A specifically, not an ECS task role)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::880690594006:role/anyaicam-ec2-app-role" },
      "Action": "sts:AssumeRole"
    }
  ]
}
```
No `sts:ExternalId` condition, per decision 5. The principal is the EC2 Application Role's ARN (role A) — resolvable only once role A actually exists in AWS with a known account ID; not guessed here.

#### B2. Live Upload Role — base permissions policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LiveSegmentUploadOnly",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::anyaicam2026/live/*"
    }
  ]
}
```
`<ANYAICAM_S3_BUCKET>` resolved 2026-08-13 to `anyaicam2026`, confirmed by read-only AWS-side verification (see "AWS bucket verification result" below) — not guessed. This is the *maximum* role B can ever do — `s3:PutObject` under `live/` only, nothing else. The per-camera narrowing to one exact prefix happens only via the session policy below, applied at `AssumeRole` time; this base policy alone would still let a session write to any camera's `live/` prefix, which is why the session policy is mandatory on every call, not optional hardening.

#### C. Per-camera inline AssumeRole session-policy template

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::anyaicam2026/live/{customer_id}/{site_id}/{appliance_id}/{camera_id}/*"
    }
  ]
}
```
Phase 2 (not started) is where FastAPI substitutes the real `{customer_id}/{site_id}/{appliance_id}/{camera_id}` values for the authorized relay session and passes this as the `Policy` parameter on the `sts:assume_role` call, after `authenticate_appliance()` and the existing tenant/customer/site/appliance/camera authorization checks succeed.

#### Exact AWS resources this design would create (none created yet)

1. IAM policy `assume-live-upload-role-only` (template A2), attached to a new IAM role `anyaicam-ec2-app-role` (template A1) with trust to `ec2.amazonaws.com`.
2. IAM instance profile `anyaicam-ec2-app-role` containing that role.
3. An association between that instance profile and EC2 instance `i-0f0fb6a78871b20d4`.
4. IAM role `anyaicam-live-relay-upload` (template B1). Corrected during execution: AWS IAM does not allow a role's `MaxSessionDuration` to be set below 3600 seconds (1 hour) — the role's maximum session duration stays at AWS's default 1 hour. The V1 900-second (15-minute) credential lifetime (decision 4) is enforced instead by requesting `DurationSeconds=900` on each `sts:AssumeRole` call, which is a Phase 2 application-code concern, not a role-level setting.
5. IAM policy `live-segment-upload-only` (template B2), attached to that role.

No S3 bucket, no CloudFront distribution, and no lifecycle policy are created by this step — those are later phases (§8 implementation-phase list, phases 2 and 5).

#### Safe, read-only ways to determine whether an AnyAiCam S3 bucket already exists

None of these require write access, and none were run during this planning step:
- `aws s3api list-buckets` (or `aws s3 ls`) from any location with authenticated AWS CLI access — the live EC2 instance does not have the CLI installed, so this needs to run from wherever AWS access already exists (e.g. a laptop with `aws configure`, or AWS CloudShell in the Console).
- AWS Console → S3 → the bucket list → search for a name containing `anyaicam`.
- AWS Console → CloudTrail → Event history → filter for `CreateBucket` events with a resource name containing `anyaicam`, which would also show who created it and when, if it exists.
- Locally, informationally only (not authoritative, may be a stale placeholder): the repo's untracked `deploy/.env.production.example` or local `.env`/`aws.env` files may reference an `ANYAICAM_S3_BUCKET=` value — worth checking yourself, since those files are already known (from the original audit) to contain other real secrets and were deliberately not opened or printed during this session.
- The running container itself was already checked (per this session's production check) and has `ANYAICAM_S3_BUCKET` present as a key but **empty** — not useful for discovering an existing bucket, but confirms recording-to-S3 upload is not currently active in production.

#### AWS CLI commands that would eventually apply this design (illustrative — NOT executed)

```bash
# A1 + A2: EC2 application role
aws iam create-role --role-name anyaicam-ec2-app-role \
  --assume-role-policy-document file://a1-ec2-app-role-trust.json \
  --description "AnyAiCam EC2 application machine identity; may only assume the live-upload role"
aws iam put-role-policy --role-name anyaicam-ec2-app-role \
  --policy-name assume-live-upload-role-only \
  --policy-document file://a2-ec2-app-role-permissions.json

# Instance profile, containing role A, attached to the running instance
aws iam create-instance-profile --instance-profile-name anyaicam-ec2-app-role
aws iam add-role-to-instance-profile \
  --instance-profile-name anyaicam-ec2-app-role --role-name anyaicam-ec2-app-role
aws ec2 associate-iam-instance-profile \
  --instance-id i-0f0fb6a78871b20d4 \
  --iam-instance-profile Name=anyaicam-ec2-app-role

# B1 + B2: live-upload role. MaxSessionDuration is left at AWS's 1-hour default --
# IAM rejects any value below 3600s, so the 15-minute V1 credential lifetime is
# enforced at AssumeRole call time via DurationSeconds=900 instead (Phase 2, not here).
aws iam create-role --role-name anyaicam-live-relay-upload \
  --assume-role-policy-document file://b1-live-upload-role-trust.json \
  --description "AnyAiCam live-media S3 upload identity; narrowed per-camera by inline session policy"
aws iam put-role-policy --role-name anyaicam-live-relay-upload \
  --policy-name live-segment-upload-only \
  --policy-document file://b2-live-upload-base-policy.json

# Verify IMDS exposes the new role -- run FROM the EC2 instance itself afterward
curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/
# expect the role name "anyaicam-ec2-app-role", not empty/404
```
AWS Console equivalents: IAM → Roles → Create role (twice, for A and B, using the JSON above as the trust/permissions documents) → EC2 → Instances → select the instance → Actions → Security → Modify IAM role, to attach the resulting instance profile.

#### Verification checklist (to run only after execution is separately authorized)

1. `aws iam get-role --role-name anyaicam-ec2-app-role` and `--role-name anyaicam-live-relay-upload` — confirm both exist with the intended trust policies and nothing else attached (`aws iam list-role-policies` / `list-attached-role-policies` should show exactly one inline/attached policy each).
2. `aws ec2 describe-iam-instance-profile-associations --filters Name=instance-id,Values=i-0f0fb6a78871b20d4` — confirm the instance profile is actually associated with the running instance.
3. From the EC2 instance itself: `curl http://169.254.169.254/latest/meta-data/iam/security-credentials/` now returns the role name instead of 404.
4. A scoped `aws sts assume-role --role-arn <live-upload-role-arn> --role-session-name test --policy file://<template-C-filled-in>` succeeds and returns temporary credentials; a `PutObject` with those credentials to the *matching* camera prefix succeeds, and a `PutObject` to a *different* camera's prefix (or outside `live/`) is denied.
5. Confirm the Docker container's boto3 client actually ends up using the instance-profile credentials rather than the existing (currently empty-valued) `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` env vars — boto3's default credential chain checks explicit environment variables *before* falling back to the instance-metadata service, so those variables may need to be fully unset (not just empty) in the container's environment for this to work correctly. **This is a deployment-configuration consideration to keep in mind for whenever execution is authorized — no `.env`/compose change is being made now.**

#### Placeholder resolution — both now RESOLVED (2026-08-13)

1. **`<AWS_ACCOUNT_ID>` — RESOLVED: `880690594006`.** Obtained read-only from the AWS account, not guessed. Substituted into templates A2 and B1 above.
2. **`<ANYAICAM_S3_BUCKET>` — RESOLVED: `anyaicam2026`.** A read-only bucket listing (`aws s3api list-buckets`, 2026-08-13) found three buckets in the account: `anyaicam-sandbox-dev-filebucket-7rmu1rmhtiur` (name implies sandbox/dev), `anyaicam2026` (the candidate), and `aws-sam-cli-managed-default-samclisourcebucket-onisu7irxxnv` (standard AWS SAM CLI deployment-artifact bucket, unrelated). `anyaicam2026` was then confirmed via read-only AWS-side verification:
   - Exists in account `880690594006`, region `us-east-1` — matches the region documented in `deploy/.env.production.example`/`cloud_config.py`'s `ANYAICAM_S3_REGION` default.
   - Public access is fully blocked and server-side encryption is enabled (SSE-S3/AES256) — matches the "blocked public access, encryption" guidance already written in `deploy/AWS-READINESS.md`.
   - No `anyaicam/recordings/...` objects exist yet — consistent with, not contradicting, the already-confirmed fact that production's `ANYAICAM_S3_BUCKET` env var is currently empty (§3) and recording-to-S3 upload isn't active yet; an intended-but-not-yet-wired-up bucket is exactly what this looks like.
   - No bucket tags are present — inconclusive on its own, but not disqualifying given the other signals.
   
   Substituted into templates B2 and C above. Repo-side cross-check already done and remains true: `deploy/.env.production.example`/`deploy/.env.staging.example` only ever had placeholder values (`REPLACE_BUCKET` / `replace-staging-bucket`) — the real bucket name was never documented in the tracked repo, and was resolved entirely by direct, read-only AWS-account inspection rather than guessed from code.

#### Remaining information needed before any of this can be created

1. ~~Confirmation of who has permission to create IAM roles/policies/instance profiles~~ — resolved by execution: the user created both roles directly via the AWS Console (IAM → Create role → Add permissions → Create inline policy), by hand, not via infrastructure-as-code (still none tracked in this repo).
2. ~~The IAM verification checklist above is what actually proves the design works — not yet run.~~ — **now run and passed; see "Phase 1 execution result — VERIFIED" immediately below.**
3. Separately (not blocking this IAM design, but still needed before it can be *fully* exercised end-to-end by Phase 2 code): a decision on whether/how to fix the confirmed `RUNTIME_ROLE=edge`-in-production drift (§3/§6), and the boto3-credential-chain/env-var consideration in verification-checklist item 5 above. Both still flagged, neither scheduled — no fix is being made now.

#### Phase 1 execution result — VERIFIED (2026-08-13)

Phase 1 IAM is now **executed and functionally verified in the real AWS account**, not merely designed. Every fact below was verified directly in AWS, not assumed from the design documents above.

**Created:**
- IAM role `anyaicam-ec2-app-role` (the EC2 Application Role, template A1/A2).
- IAM role `anyaicam-live-relay-upload` (the Live Upload Role, template B1/B2).
- Inline policy `live-segment-upload-only` on `anyaicam-live-relay-upload`: `s3:PutObject` only, resource `arn:aws:s3:::anyaicam2026/live/*` — exactly template B2, no broader S3 permissions were added.
- Trust relationship on `anyaicam-live-relay-upload` limited to exactly one principal: `arn:aws:iam::880690594006:role/anyaicam-ec2-app-role` — exactly template B1.

**Attached and verified live in AWS:**
- `anyaicam-ec2-app-role` is attached to production EC2 instance `i-0f0fb6a78871b20d4` via an instance profile.
- The instance profile was verified **active through IMDSv2** on the instance itself — `iam/security-credentials/` now exposes `anyaicam-ec2-app-role` (previously 404, per the §3 production check before this role existed).

**Functionally verified (not just "created," actually exercised):**
- `sts:AssumeRole` from `anyaicam-ec2-app-role` into `arn:aws:iam::880690594006:role/anyaicam-live-relay-upload` succeeds.
- Requesting `DurationSeconds=900` on that `AssumeRole` call succeeds and returns a 15-minute session — confirming the V1 credential-lifetime target (decision 4) is achievable even though the role's own `MaxSessionDuration` stays at AWS's 1-hour default (see the corrected note on decision 4/exact-resources above: IAM does not allow lowering a role's own max below 3600s).
- Using that session, `s3:PutObject` to `s3://anyaicam2026/live/test-customer/test-site/test-appliance/camera1/` **succeeds** — the allowed, matching prefix.
- Using that same session, `s3:PutObject` to the *different*, unauthorized `camera2` prefix is **denied with `AccessDenied`** — confirming the session-policy-narrowing design (template C's intent) actually holds, not just in theory.
- The base `anyaicam-ec2-app-role` (role A) was confirmed **unable to call `s3:DeleteObject` directly** — consistent with role A having no S3 permissions of its own beyond `sts:AssumeRole` on role B (template A2).
- A temporary test object (`iam-test.txt`) created during this verification was manually deleted from the S3 console afterward — no test artifacts were left in `anyaicam2026`.

**What this proves:** the approved Phase 1 design (one shared role, session-policy-narrowed per camera, `s3:PutObject`-only, no `ListBucket`/`GetObject`/`DeleteObject`, EC2-instance-profile-based trust, no `sts:ExternalId`) is not just written down correctly — it behaves correctly against the real AWS account, including the negative case (a session cannot write outside its authorized prefix) and the privilege-separation case (the base app role cannot touch S3 directly). This is the evidence Phase 2 application code can now be built against.

**What this does NOT do:** no application code was written or changed, `ANYAICAM_RUNTIME_ROLE` was not touched, the confirmed production drift (§3/§6) was not fixed, and Phase 2 has not started.

#### Phase 2 result — implemented and tested, NOT YET COMMITTED (2026-08-13)

Phase 2 was authorized and built: the cloud-side control-plane endpoints for live-relay credential issuance and segment bookkeeping, per implementation phase 2 in this section's phase list. Behind a feature flag, defaulting off, exactly as the phase list requires — **no media bytes touch this code path**, and neither route does anything at all unless `ANYAICAM_LIVE_RELAY_ENABLED=true` is explicitly set.

**New files:**
- `app/live_manifest.py` — `LiveManifestStore`: a small rolling per-camera manifest (last 5 segment references), backed by a JSON file. Loading is deliberately **lazy** — deferred to the first real `record_segment()`/`manifest_for()` call, not `__init__` — so that merely importing `appliance_cloud.py` (which `main.py` already does, and which every existing Phase 0 test transitively imports) touches no filesystem at all. Verified directly: the backing file does not exist after construction, only after real use.
- `app/tests/test_live_manifest.py` — 7 tests: rolling-window trim, dedup of a re-recorded key, per-camera isolation, persistence across store instances, and the no-filesystem-on-construction guarantee itself.
- `app/tests/test_live_relay_session_endpoints.py` — 11 tests for the two new routes below, calling the route functions directly (pulled off a freshly-registered `FastAPI()` app, no TestClient/ASGI needed) with `authenticate_appliance()` and `boto3` mocked — no real network/AWS/DB call is made by any test.

**Extended files:**
- `app/appliance_protocol.py` — added `LIVE_RELAY_SESSION_DURATION_SECONDS` (900, matching Phase 1 decision 4), `live_relay_s3_prefix()`, `live_relay_session_policy()` (builds Phase 1 template C, filled in per-request), and `live_relay_session_name()` (CloudTrail-readable, sanitized to AWS's `RoleSessionName` character/length rules — resolves the item Phase 1 decision 7 explicitly deferred to Phase 2).
- `app/appliance_cloud.py` — added `boto3` import (guarded, matching `main.py`'s existing pattern), the `ANYAICAM_LIVE_RELAY_ENABLED` / `ANYAICAM_LIVE_UPLOAD_ROLE_ARN` / `ANYAICAM_LIVE_MANIFEST_FILE` config reads (reusing the already-existing `ANYAICAM_S3_BUCKET` and `AWS_REGION`/`AWS_DEFAULT_REGION` — no new bucket/region config introduced), a `live_manifest_store` singleton, and a `_authorized_camera()` helper (`SELECT * FROM cameras WHERE id=? AND appliance_id=?`, `403` if the requested camera doesn't belong to the authenticated appliance) shared by both new routes.

**Two new routes, both gated by `authenticate_appliance()` then the feature flag, in that order:**
- `POST /api/appliance/live/{camera_id}/session` — authenticates, authorizes camera ownership, then calls `sts:AssumeRole` on the Phase-1-created `anyaicam-live-relay-upload` role (ARN from `ANYAICAM_LIVE_UPLOAD_ROLE_ARN`) with the per-camera session policy and `DurationSeconds=900`, and returns the resulting temporary credentials + `bucket`/`key_prefix` as JSON. Missing config (`boto3` unavailable, role ARN/bucket/region unset) returns `503`; an STS call failure is caught and returned as `502` with a generic message, never a raw exception. FastAPI never touches a media byte here — only STS metadata.
- `POST /api/appliance/live/{camera_id}/segment-available` — authenticates, authorizes camera ownership, then requires the payload's `segment_key` to start with exactly that camera's authorized prefix (`live_relay_s3_prefix(customer_id, site_id, appliance_id, camera_id)`) — **`403` otherwise**. This check was added during review: without it, an authenticated appliance could submit a `segment_key` naming a *different* camera's (or a different appliance's/customer's) prefix and have it recorded in this camera's manifest, even though it could never have obtained upload credentials for that prefix. Verified with three tests: the exact camera's own prefix is accepted; another camera's prefix on the *same* appliance is rejected; a forged prefix naming a different appliance/customer entirely is rejected — all three confirm the manifest is left untouched on rejection, not partially written.

**Result: 63/63 `app/tests/` tests passing** (45 prior + 7 + 11 new), full suite re-run clean, no regressions.

**What Phase 2 deliberately does NOT do:** no appliance-side code (segment-watcher/uploader is Phase 3), no `start_live_relay`/`stop_live_relay` command wiring or `ALLOWED_COMMANDS` changes (Phase 4), no S3 lifecycle policy or CloudFront (Phase 5), no frontend changes (Phase 6). `ANYAICAM_RUNTIME_ROLE=edge` production drift (§3/§6) remains unfixed, deliberately, same as every prior phase. **Nothing has been committed or pushed** — this code exists only in the local working tree pending review and a separate commit authorization, per the established pattern for this project.

---

### Phase 3 result — implemented and tested, NOT YET COMMITTED (2026-08-13)

Phase 3 was authorized and built: the appliance-side live-segment watcher/uploader, per implementation phase 3 in this section's phase list. It requests/renews scoped credentials from the Phase 2 control plane and `PutObject`s `.ts` segments directly to S3, gated by `RUNTIME_ROLE in {edge, combined}` and a per-camera relay-active flag defaulting off, exactly as the phase list specified. `start_live_stream()`, `camera_url()`, and the local recording pipeline were not touched.

**New files:**
- `app/live_relay_uploader.py` — the watcher/uploader itself. Reads new segment names directly out of each camera's own local `.m3u8` playlist (not a filename glob — FFmpeg's default segment naming has no guaranteed separator between a camera number and its index, so a glob risks ambiguity; the playlist is the same file hls.js already reads for local playback and is authoritative per-camera). Every playlist entry is validated before being touched: `Path(...).name` strips any directory component (defeats path traversal), a regex requires the literal `camera{N}` prefix and a `.ts` suffix, and the resolved path's parent is re-checked against the resolved HLS folder as a second, independent guard — invalid entries are logged and dropped, never opened. Appliance identity (`appliance_id` + bearer credential) is read from the same `credential.json` file format the `appliance-agent` package already writes on activation (`ANYAICAM_STATE_DIR`, default `/var/lib/anyaicam`) — no second enrollment mechanism. Control-plane calls use the same bearer + nonce + timestamp headers `authenticate_appliance()` already requires, over stdlib `urllib.request` (no new dependency). STS session responses are fully validated (dict shape, non-empty `access_key_id`/`secret_access_key`/`session_token`/`expiration`/`bucket`/`key_prefix`) before caching, with only a camera number/ID and a fixed reason code ever logged on rejection — never response contents or credential values. Session expiry (`_session_expires_soon`) treats every ambiguous case (missing/non-string/blank/malformed/timezone-naive) as expired, forcing renewal rather than risking reuse. A failed or stale segment is logged and dropped, never retried, per the architecture's documented reconnect behavior. `set_relay_active(camera_number, camera_id, active)` is the only way a camera's relay turns on, defaulting to nothing active — Phase 4's `start_live_relay`/`stop_live_relay` command wiring (not part of this phase) is the intended, and currently only, caller in production.
- `app/tests/test_live_relay_uploader.py` — 53 focused unit tests: identity loading/validation, playlist parsing and path/name validation (including a symlink-escape case and a path-traversal case), dedup tracking, session-expiry and response-shape validation, control-plane request shape (headers/body), upload/notify flow, and the disabled-worker paths (`RUNTIME_ROLE=cloud`, `LIVE_RELAY_ENABLED=false`). No real network/AWS call is made; `boto3` and `urllib.request.urlopen` are mocked throughout.

**Extended files:**
- `app/main.py` — a 4-hunk, 16-line additive change to `lifespan()`, mirroring how `health_task`/`retention_task` are already conditionally created: one import of `live_relay_uploader`, one `live_relay_task` created only when `RUNTIME_ROLE in {"edge", "combined"} and live_relay_uploader.LIVE_RELAY_ENABLED` (both must hold — the task itself is never constructed otherwise, not just parked asleep), one `.cancel()` alongside `cloud_upload_task.cancel()` in the shutdown path, and one append to the shutdown `gather()`. Nothing else in `main.py` changed.

**Validation result:**
- **Focused tests: 53/53 passing** (`app/tests/test_live_relay_uploader.py`), run standalone and confirmed clean.
- **Full `app/tests/` suite:** blocked from running on this WSL host directly — `app/main.py` hardcodes `HLS_FOLDER`/`RECORDINGS_FOLDER` under `/app/...` (not env-configurable) and creates them at module import time, which assumes the process runs inside the project's Docker image (`WORKDIR /app`); on this WSL host, `/` is `root:root`, so `import main` fails with a `PermissionError` before any test logic runs. This is a pre-existing condition of `main.py`, not introduced by Phase 3.
  - Investigated and confirmed how the Phase 0–2 full-suite runs ("45/45", "63/63") avoided this: they were run via **Windows-native Python**, not WSL. Evidence: `C:\app\static`/`C:\app\recordings` already exist on this machine (matching this doc's own Phase 0.1 note about `C:\app\recordings\license_state.json`), and a read-only probe confirmed `Path("/app/static/hls").resolve()` under Windows Python resolves to `C:\app\static\hls` (Windows pathlib anchors a leading `/` with no drive letter to the *current drive's root*, not to a Unix filesystem root) — an already-existing, unprivileged directory, not one requiring creation.
  - Ran the real full suite via `py -3.14 -m unittest discover -s tests -p "test_*.py" -v` from the Windows path of `app/` (the same interpreter confirmed to already have every `requirements.txt` package installed, via a read-only import probe — no installs were performed). **Result: 115/116 tests passed.**
  - The **one error**: `test_pending_segments_excludes_a_symlink_resolving_outside_hls_folder` (one of the 53 new Phase 3 tests) — `OSError: [WinError 1314] A required privilege is not held by the client`, because `os.symlink()` on Windows requires `SeCreateSymbolicLinkPrivilege` (Administrator or Developer Mode), which this non-elevated account doesn't have. This is a failure of the *test's own setup step* (constructing a symlink to prove the containment check catches it), not of the `_pending_segments()` logic under test — that same test, and the containment check it exercises, already passed cleanly in the earlier WSL/Linux run, where `os.symlink()` needs no special privilege. No Phase 3 code defect was found.
  - `test_runtime_role_lifespan_characterization.py` — the test most at risk from the `lifespan()` wiring change — passed all 3 cases (`cloud` starts zero camera supervisors; `edge`/`combined` each start one live + one recording supervisor per camera), confirming the new `live_relay_task` gating didn't disturb existing `RUNTIME_ROLE` behavior. Every other existing test file in `app/tests/` also passed, confirming local HLS/recording/live-view behavior is unaffected.
- Separately, and unrelated to Phase 3 code: diagnosed a pre-existing discrepancy where `git status` showed ~19 additional modified files. Confirmed read-only (`git diff --numstat`, `git diff --ignore-all-space --name-only`, byte-level CR-stripped comparison) that every one of those files is a **CRLF-vs-LF line-ending difference only, with zero content change** — `git diff --ignore-all-space` reduces the entire working-tree diff to just `app/main.py`. Root cause: no `core.autocrlf`/`.gitattributes` configured in this repo, and the working tree (on `/mnt/c/...`, a Windows/OneDrive-backed path) has CRLF endings where HEAD's blobs have LF. Not touched, not normalized, not restored.

**What Phase 3 deliberately does NOT do:** no command-channel wiring (`start_live_relay`/`stop_live_relay`, `ALLOWED_COMMANDS` changes — Phase 4), no S3 lifecycle policy or CloudFront (Phase 5), no frontend changes (Phase 6). `set_relay_active()` is never called from anywhere in production code yet, so no camera has an active relay by default even if `ANYAICAM_LIVE_RELAY_ENABLED=true` were set. `ANYAICAM_RUNTIME_ROLE=edge` production drift (§3/§6) remains unfixed, deliberately, same as every prior phase. **Committed and pushed as `cfa6862`** ("Phase 3: appliance-side live-relay segment uploader"), confirmed synchronized with `origin/build/v1.2-modular-foundation` at that commit.

---

### Phase 4 result — live-relay command-channel wiring, IMPLEMENTED, TESTED, COMMITTED, AND PUSHED (2026-08-13)

Phase 4 was authorized and built: wiring the existing appliance command channel so cloud live-relay control can turn individual camera relays on and off, using the Phase 3 seam `set_relay_active(camera_number, camera_id, active)`, per implementation phase 4 in this section's phase list.

**Files changed:**
- `app/appliance_protocol.py` — `ALLOWED_COMMANDS` extended with `start_live_relay`/`stop_live_relay`.
- `appliance-agent/anyaicam_agent/config.py` — new `live_relay_commands_file` property, mirroring `credential_file`/`queue_file`/`cameras_file`.
- `appliance-agent/anyaicam_agent/commands.py` — `_validate_relay_camera_number`/`_validate_relay_camera_id`/`_set_relay_command` helpers, plus `start_live_relay`/`stop_live_relay` dispatch branches in `execute()`.
- `app/live_relay_uploader.py` — `RELAY_COMMANDS_FILE`, `_last_applied_relay_state`, `_validate_camera_number`/`_validate_camera_id`/`_parse_relay_commands_file`/`_reconcile_relay_commands`, wired into `live_relay_worker()`'s existing enabled-path loop.
- `appliance-agent/tests/test_agent.py` — 21 new tests.
- `app/tests/test_live_relay_uploader.py` — 27 new tests.

**Architecture / behavior:**
- `start_live_relay` and `stop_live_relay` are now allowed through the existing appliance command channel (`POST /api/partner/appliances/{id}/commands` → `GET /api/appliance/commands` → `appliance-agent`'s `execute()` → `POST /api/appliance/commands/{id}`) — no new endpoint, no change to that channel's existing mechanics.
- `appliance-agent` validates `camera_number` and `camera_id` and atomically writes desired relay state to `ANYAICAM_STATE_DIR/live_relay_commands.json` (temp-file write + `os.chmod(0o600)` + `replace()`, matching `save_credential()`'s existing pattern).
- File permissions are restricted to `0600`.
- The file contains only `camera_number`, `camera_id`, and `active` — no AWS credentials, passwords, or RTSP URLs ever reach it.
- The VMS app process reads and reconciles that desired-state file from `live_relay_worker()`, once per tick.
- Reconciliation calls `set_relay_active()` only when a state transition is actually required — an unchanged desired entry is never replayed on subsequent ticks.
- Removing a camera from desired state, clearing the file, or losing the file entirely deactivates a previously-active camera exactly once, then stops considering it — repeated empty/missing ticks make no further calls.
- Changing `camera_id` for the same `camera_number` while active deactivates the old identity first, then activates the new one — never a single in-place swap, so a stale S3 session/prefix can never be silently reused for a different camera.
- Malformed entries are isolated per `camera_number` and never block any other valid entry in the same file.
- Relay remains OFF by default — nothing in discovery, onboarding, startup, recording, or local live view activates it.
- `set_relay_active()` itself was not modified.
- Existing local HLS, recording, camera discovery, camera-compatibility detection, the Phase 2 control plane, and Phase 3 upload/session behavior were not redesigned.

**Important two-process limitation:** `appliance-agent` and the VMS app are separate processes (see §2). Cloud command `status='completed'` means the desired state was successfully and durably recorded by `appliance-agent` — it does **not** mean the VMS app has confirmed applying `set_relay_active()`. Application happens asynchronously, on the VMS worker's next reconciliation tick (normally within `SCAN_SECONDS`, default 1 second). This is documented as an inherent eventual-consistency limitation of the two-process bridge design, not as confirmed activation.

**Known unresolved integration gap:** the cloud `cameras` table currently has a DB `id` but no authoritative appliance-local `camera_number` mapping. Phase 4 deliberately does not solve this. Whatever eventually queues `start_live_relay`/`stop_live_relay` (the Phase 6 "open camera" frontend action) will need to resolve `camera_id` → appliance `camera_number` before queueing — that mapping mechanism does not exist yet and was intentionally not designed or built as part of this phase.

**Testing:**
- `appliance-agent/tests/test_agent.py`: **26/26 passing** (5 pre-existing + 21 new Phase 4 tests).
- `app/tests/test_live_relay_uploader.py`: **80/80 passing** (53 pre-existing Phase 3 + 27 new Phase 4 tests).
- Full `app/tests/` suite via Windows-native Python (`py -3.14`, the same environment Phase 0–3 used): **184 tests total, 183 passing, 0 failures, 1 error** — the sole error remains the same pre-existing, unrelated Windows-only `os.symlink()` privilege limitation documented under Phase 3. Zero Phase 4 regressions.

**What Phase 4 deliberately does NOT do:** no Phase 5 S3 lifecycle/CloudFront/CDN work, no Phase 6 frontend work, no live-cloud activation, no camera discovery changes, no camera-compatibility changes, no DB schema migration, no `app/main.py` changes, no new HTTP endpoint, and no redesign of `app/appliance_cloud.py` beyond the `ALLOWED_COMMANDS` source it already imports from `app/appliance_protocol.py`. **Committed and pushed as `a02076c`** ("Phase 4: live-relay command-channel wiring"), confirmed synchronized with `origin/build/v1.2-modular-foundation` at that commit.

---

### Phase 5 result — S3 lifecycle + CloudFront segment-delivery foundation, IMPLEMENTED AND VERIFIED IN AWS, NOT YET COMMITTED (2026-08-14)

Phase 5 was authorized and built as the infrastructure/distribution layer deferred from Phases 2–4: S3 retention for live-relay objects and a CloudFront distribution in front of the `live/` prefix, per implementation phase 5 in this section's phase list. Scope was deliberately narrowed during design review to **a segment-delivery foundation only** — no viewer-authorization/signing infrastructure and no manifest-serving code, both explicitly deferred to a future, separately-authorized phase.

**AWS resources created (verified live in the account, not merely designed):**
- **S3 lifecycle rule** on bucket `anyaicam2026`: ID `live-segment-expiration-1d`, scoped to prefix `live/`, current-object expiration after 1 day, incomplete multipart uploads aborted after 1 day. No noncurrent-version rule — bucket versioning is disabled, so none is needed. (Corrected during design against real S3 constraints: the architecture's original "expire after a few minutes" language is not achievable — S3 Lifecycle expiration only operates in whole-day increments — so 1 day was chosen as the smallest practical unit, not a minimized-but-still-generous window.)
- **CloudFront Origin Access Control** `anyaicam-live-relay-oac` (ID `EP2WMLOJVC737`), S3 origin type, SigV4 signing always required.
- **CloudFront distribution** `anyaicam-live-relay` (ID `E1LBM1M5362SXK`, domain `d31cxfv0l904ar.cloudfront.net`): origin is `anyaicam2026` via the OAC above; `GET`/`HEAD` only; viewer protocol redirects HTTP to HTTPS; managed `CachingOptimized` cache policy; no origin request policy; `PriceClass_100`; no alternate/custom domain; no ACM certificate. **Deliberately created with `Enabled = false`** — see "Distribution left disabled" below.
- **S3 bucket-policy statement** `AllowCloudFrontOACReadLivePrefixOnly`: `Principal` `cloudfront.amazonaws.com`, `Action` `s3:GetObject`, `Resource` `arn:aws:s3:::anyaicam2026/live/*`, `Condition` `AWS:SourceArn` restricted to exactly `arn:aws:cloudfront::880690594006:distribution/E1LBM1M5362SXK`. Block Public Access remains fully ON — this grant is scoped to a specific AWS service principal with a `SourceArn` condition, which S3's public-access-block evaluation does not treat as a public grant.

**Distribution left disabled — this is a deliberate, load-bearing safety property, not an oversight:** Origin Access Control plus the scoped bucket policy prevents any *direct* public read of the S3 origin, but neither of those, on their own, makes a CloudFront-delivered URL private — without a trusted key group and signed URLs/cookies, anyone who learns (or guesses) a `live/*` object's CloudFront URL could fetch it if the distribution were enabled. Since Phase 5 intentionally defers that signing infrastructure, the distribution was created with `Enabled = false` specifically so the infrastructure could be stood up, wired, and verified without opening any viewer-facing path to live camera footage. **The distribution must remain disabled until a future, separately-authorized phase implements viewer authorization (trusted key group, public/private signing key, signed-URL or signed-cookie minting).**

**Architecture limitation, explicitly documented (not solved by this phase):** the current appliance-side uploader (Phase 3) writes `.ts` segment objects to `live/{customer_id}/{site_id}/{appliance_id}/{camera_id}/` only. **No CloudFront-playable `.m3u8` manifest is ever written to S3** — `app/live_manifest.py`'s `LiveManifestStore` (Phase 2) persists its rolling per-camera manifest to a local JSON file only, not to S3. Phase 5 is the CDN/retention foundation for segment objects; it does not add manifest-generation or manifest-upload code, and does not redesign the Phase 2/3 control plane to solve this gap. Whichever future phase wires up viewer playback will need to resolve how a browser obtains a playable playlist (e.g., FastAPI serving the manifest dynamically outside the CDN, versus writing an `.m3u8` to S3 for CloudFront to serve) — that decision was deliberately left open, not made here.

**What Phase 5 deliberately does NOT do:** no application code changed (`app/`, `appliance-agent/` untouched), no deployment-file changes (`ecs-task-definition.json`, `deploy/*` untouched), no changes to the Phase 1 IAM roles, no custom domain or ACM certificate, no trusted key group/public key/signed-URL/signed-cookie infrastructure, no manifest generation or upload, no Phase 6 frontend work, and no live production activation — the distribution stays disabled. `ANYAICAM_RUNTIME_ROLE=edge` production drift (§3/§6) remains unfixed, deliberately, same as every prior phase.

**Validation:** this phase changed zero application code, so no `app/tests/` run was needed or performed for it. All verification was AWS-side, performed manually in the Console and reported back: lifecycle rule, OAC, distribution, and bucket-policy configuration all confirmed to match the approved design exactly as listed above.

This documentation update is the only tracked-repo change associated with Phase 5 — no application code was written for it. **Not yet committed.**

---
### Phase 6a result — camera_id ↔ camera_number mapping + technician assignment, IMPLEMENTED, TESTED, COMMITTED, AND PUSHED (2026-08-14)

Phase 6a was authorized and built: the first of six Phase 6 sub-phases (6a–6f), resolving the "Known unresolved integration gap" flagged under Phase 4 — the cloud `cameras` table's TEXT `id` (multi-tenant: `customer_id`/`site_id`/`appliance_id`) had no relationship to the appliance-local `camera_number` (1..`CAMERA_COUNT`) that the FFmpeg/HLS pipeline and the entire edge-side frontend actually use.

**Files changed:**
- `app/db_migrations.py` — nullable `cameras.camera_number INTEGER` column plus a partial unique index `idx_cameras_appliance_camera_number ON cameras(appliance_id,camera_number) WHERE camera_number IS NOT NULL`, added via the same dual-backend (SQLite/PostgreSQL) schema-inspection-guarded pattern already used for `partner_users`/`customer_camera_permissions`.
- `app/camera_mapping.py` (new) — `resolve_camera_number()`, `assign_camera_number()`, `CameraNumberConflict`.
- `app/partner_workspace.py` — the existing customer-setup wizard's "Camera setup" step extended with a "Local camera slot #" column; `PUT /api/customer/cameras` extended to resolve `appliance_id` from a trusted, customer-scoped DB lookup and call `assign_camera_number()`.
- `app/tests/test_camera_mapping.py` (new) — 19 focused resolver/assigner tests.
- `app/tests/test_customer_camera_number_wiring.py` (new) — 6 migration + route-wiring tests.

**Architecture/behavior:**
- The mapping is never inferred — no IP matching, no discovery-id parsing, no name/ordering heuristics were used (all explicitly ruled out during design: camera IPs must never leave the LAN per §1, and discovery's own id is IP-derived and unstable). It is an explicit, human-confirmed assignment made through the existing customer-setup wizard only.
- `camera_number` is unique per `appliance_id` — not globally, not per customer — enforced by both the partial unique DB index (authoritative backstop against a race) and an application-layer pre-check in `assign_camera_number()` (a clean, catchable `CameraNumberConflict` instead of a raw integrity error).
- `configure_customer_cameras()` obtains `appliance_id` exclusively from a `SELECT id,appliance_id FROM cameras WHERE id=? AND customer_id=?` scoped to the authenticated caller's own `customer_id` — never from the browser payload, per the explicit security refinement required before implementation.
- Every failure mode fails closed: a nonexistent `camera_id`, a camera belonging to a different customer, and a camera belonging to a different appliance are all indistinguishable `LookupError`/silent-skip outcomes — none of them ever reveal whether a mismatched `camera_id` exists elsewhere. An unassigned (`NULL`) `camera_number` is a valid, safe default state.
- Duplicate/conflicting assignments within one "Save camera setup" batch are rejected and reported per-item in the response's `errors` list without aborting or partially applying unrelated items in the same batch (batch isolation, mirroring the precedent already established in the Camera Compatibility feature's `evaluate_scan_results()`).
- The client script now visibly surfaces a non-empty `errors` list in its toast message instead of showing only a generic "saved" success message — a conflicting/invalid assignment can never appear to have silently succeeded.
- A successfully assigned `camera_number` does not by itself prove that slot is actually configured on a given appliance today — `camera_mapping.py`'s own docstring records this explicitly, so any future relay-start logic consuming this mapping must still fail closed if the mapped slot isn't actually available.

**Testing:**
- `app/tests/test_camera_mapping.py`: **19/19 passing.**
- `app/tests/test_customer_camera_number_wiring.py`: **6/6 passing.**
- Full `app/tests/` suite via Windows-native Python (`py -3.14`, the same environment prior phases used): **209 tests total, 208 passing, 0 failures, 1 error** — the sole error remains the same pre-existing, unrelated Windows-only `os.symlink()` privilege limitation documented under Phase 3. Zero Phase 6a regressions.

**What Phase 6a deliberately does NOT do:** no AWS/CloudFront changes (CloudFront distribution `E1LBM1M5362SXK` remains disabled, exactly as Phase 5 left it), no manifest generation or signed-URL/signed-cookie infrastructure (Phase 6b), no customer live start/stop routes (Phase 6c), no wiring of the existing-but-unused `live_view_sessions` table (Phase 6c), no cloud-specific live-view frontend surface (Phase 6d), and no redesign of the Phase 2/3/4 relay implementation (`app/appliance_cloud.py`'s live-relay routes, `app/live_relay_uploader.py`, `app/appliance_protocol.py`, `appliance-agent/anyaicam_agent/commands.py`) — all untouched.

**Committed and pushed as `5f05f10`** ("Phase 6a: add camera number mapping"), confirmed synchronized with `origin/build/v1.2-modular-foundation` at that commit.
**Architecture status: APPROVED — the direction above (corrected media format, direct-to-S3 data plane, STS-scoped credentials, control/data-plane separation, hybrid on-demand publishing) is the accepted design.**
**Implementation status: Phase 0 (characterization tests) APPROVED and COMPLETE. Phase 0.1 (hashlib fix + cleanup checkpoint) COMPLETE. Phase 1 IAM EXECUTED AND FUNCTIONALLY VERIFIED in AWS (2026-08-13) — both roles exist, the instance profile is attached and confirmed live via IMDSv2, `AssumeRole` with `DurationSeconds=900` works, the per-camera session-policy narrowing was proven with both a positive (`camera1`, allowed) and negative (`camera2`, denied) test, and the base app role was confirmed to have no direct S3 access. No broader S3 permissions were added than designed. Phase 2 (cloud-side control-plane endpoints) IMPLEMENTED, TESTED, COMMITTED, AND PUSHED (2026-08-13) — credential-issuance and segment-available routes exist behind `ANYAICAM_LIVE_RELAY_ENABLED` (default off), 63/63 tests passing; committed as `fa15116` ("Phase 2: cloud-side live-relay control-plane endpoints") and confirmed synchronized with `origin/build/v1.2-modular-foundation` at that commit. Phase 3 (appliance-side segment-watcher/uploader) IMPLEMENTED, TESTED, COMMITTED, AND PUSHED (2026-08-13) — `app/live_relay_uploader.py` plus a small additive `lifespan()` wiring in `app/main.py`, gated on `RUNTIME_ROLE in {edge, combined}` and `ANYAICAM_LIVE_RELAY_ENABLED`; 53/53 focused tests passing, and 115/116 passing on a full `app/tests/` suite run via Windows-native Python (`py -3.14`, the same environment Phase 0–2 used) — the sole error is a Windows-only `os.symlink()` privilege limitation in one test's setup, not a code defect (that same check already passed on WSL/Linux); committed as `cfa6862` ("Phase 3: appliance-side live-relay segment uploader") and confirmed synchronized with `origin/build/v1.2-modular-foundation` at that commit. Phase 4 (live-relay command-channel wiring) IMPLEMENTED AND TESTED (2026-08-13) — start_live_relay/stop_live_relay wired through the existing appliance command channel into the Phase 3 set_relay_active() seam via a shared desired-state file (ANYAICAM_STATE_DIR/live_relay_commands.json); 26/26 appliance-agent tests passing, 80/80 app/tests/test_live_relay_uploader.py tests passing, and 183/184 passing on a full app/tests/ suite run via Windows-native Python (py -3.14) — the sole error is the same pre-existing, unrelated Windows-only os.symlink() privilege limitation documented under Phase 3; committed as `a02076c` ("Phase 4: live-relay command-channel wiring") and confirmed synchronized with `origin/build/v1.2-modular-foundation` at that commit. Separately, Camera Discovery — IP Camera Compatibility Detection (§9, outside the numbered implementation phases) was implemented, tested, committed, and pushed as `f8053e2` ("Camera discovery: add reusable IP camera compatibility detection") on 2026-08-13. Phase 5 (S3 lifecycle + CloudFront segment-delivery foundation) IMPLEMENTED AND VERIFIED IN AWS (2026-08-14) — a 1-day expiration/abort-incomplete-multipart lifecycle rule on the `live/` prefix of `anyaicam2026`, plus a CloudFront distribution (`E1LBM1M5362SXK`, domain `d31cxfv0l904ar.cloudfront.net`) reading that prefix through a scoped Origin Access Control and bucket-policy statement restricted to exactly this distribution's ARN; the distribution was deliberately created **disabled** and no signed-URL/cookie or trusted-key-group infrastructure exists yet, so no `live/` object is externally viewable as a result of this phase; no application code changed. **Not yet committed.** Phase 6a (camera_id ↔ camera_number mapping + technician assignment) IMPLEMENTED, TESTED, COMMITTED, AND PUSHED (2026-08-14) — nullable `cameras.camera_number` plus a per-appliance partial unique index, `app/camera_mapping.py`'s explicit-assignment-only resolver/assigner, and the existing customer-setup wizard extended with a trusted, customer-scoped `appliance_id` lookup and batch-isolated, UI-visible error reporting; 25/25 focused tests passing, and 208/209 passing on a full app/tests/ suite run via Windows-native Python (py -3.14) — the sole error is the same pre-existing, unrelated Windows-only os.symlink() privilege limitation documented under Phase 3; committed as `5f05f10` ("Phase 6a: add camera number mapping") and confirmed synchronized with `origin/build/v1.2-modular-foundation` at that commit. CloudFront distribution `E1LBM1M5362SXK` remains DISABLED, unchanged since Phase 5. Production is still confirmed running `RUNTIME_ROLE=edge`, contradicting the committed `ecs-task-definition.json` — this drift remains deliberately untouched and flagged for a later, separate fix. Phase 6b and all subsequent phases are NOT YET AUTHORIZED — each requires a separate, explicit go-ahead before any further application code is written.**

---

## 9. Camera Discovery — IP Camera Compatibility Detection (feature, IMPLEMENTED, TESTED, COMMITTED, AND PUSHED — 2026-08-13)

An additive extension to the existing camera discovery pipeline (§2's "camera discovery and verification" ownership under `app.edge`). Independent of, and not gated by, the Phase 0–3 live-relay authorization state in §8 above — it is not a new phase in that numbered plan. Adds a compatibility verdict to each device the existing discovery pipeline already finds, so a customer/technician can see whether a discovered IP camera (wired or Wi-Fi) is ready for the normal onboarding pipeline before configuring it.

**New reusable module: `app/edge/camera_compatibility.py`.** A pure, stdlib-only, dependency-light module — no network I/O, no database access, no FastAPI import — deliberately designed so a future caller (e.g. a customer-facing pre-purchase compatibility checker, explicitly not built as part of this feature) can call `evaluate_camera_compatibility()` directly with its own capability facts and get the same verdict a live discovery scan would produce. `evaluate_scan_results()` is the adapter specific to today's discovery wire format, translating `discovery.py`'s `rtsp_support`/`onvif_support` field names into the engine's own contract.

**Cloud-side integration: `app/appliance_cloud.py::secure_scan_results()`.** After the existing `sanitize_appliance_payload()` call (unchanged — credentials are stripped before the compatibility engine or database ever sees a result), a `status=='complete'` submission now runs `evaluate_scan_results(results)` before storing/inserting. The single `INSERT OR IGNORE INTO cameras` loop was changed from unconditional per discovered device to branching on each item's `compatibility_status`.

**Decision model — three possible verdicts, always with machine-readable reason codes:**
- **APPROVED** — RTSP and ONVIF both positively confirmed.
- **PARTIALLY_SUPPORTED** — every case where support can't be proven either way, or where there's a countervailing signal (e.g. RTSP failed but ONVIF confirmed — benefit of the doubt, since ONVIF-confirmed devices almost always also support RTSP).
- **NOT_SUPPORTED** — the single case requiring affirmative evidence of incompatibility: RTSP positively checked and absent, AND ONVIF positively checked and absent. No other combination reaches this status — an unconfirmed/unknown capability never by itself produces NOT_SUPPORTED ("could not prove support" is never promoted to "proved unsupported").

**Wi-Fi vs. Ethernet transport is never inferred and never affects compatibility.** Neither ONVIF WS-Discovery nor a TCP port probe can reveal physical connection medium at the IP layer, so the engine's `transport` field is pure passthrough (`"wired"`/`"wifi"`/defaulting to `"unknown"`) — verified by test that identical capabilities with different `transport` values produce byte-identical `status`/`reasons`.

**Persistence, without a schema migration:**
- **APPROVED** devices are inserted into `cameras` with `status='discovered'` — byte-identical to pre-feature behavior, so the existing onboarding/install flow for a wired-camera-equivalent device is completely unchanged.
- **PARTIALLY_SUPPORTED** devices are inserted with `status='needs_review'` (a new value for the existing free-text `cameras.status` column — confirmed nothing in the codebase switches on an enumerated set of status values, so this is safe).
- **NOT_SUPPORTED** devices are **not inserted into `cameras` at all** — the strongest available protection against a NOT_SUPPORTED camera ever being silently treated as approved later. They remain fully visible, with their reasons, in `camera_scan_jobs.results_json` (the same JSON blob the customer-setup wizard's scan-results screen already renders), so nothing is hidden from the customer — only excluded from ever reaching the onboarding table.

**No `discovery.py` probing changes were needed.** `appliance-agent/anyaicam_agent/discovery.py::scan()` already unconditionally probes both RTSP (554/8554) and ONVIF (scopes or 80/8000) for every candidate IP and returns definite booleans (`rtsp_support`, `onvif_support`) — confirmed there is no "not checked" state produced by the current implementation, since both checks always run for every candidate. The engine's tri-state (`True`/`False`/`None`) contract exists for forward-compatibility with future callers, not because today's discovery data needed enriching.

**No DB schema migration, no onboarding UI changes, no live-cloud activation, no unrelated Phase work.** `appliance-agent/anyaicam_agent/discovery.py`, `app/main.py`, the `partner_workspace.py` customer-setup wizard UI/JS, and every Phase 0–3 live-relay file are untouched by this feature.

**New tests:**
- `app/tests/test_camera_compatibility.py` — 32 tests: the full tri-state decision table, the explicitly required scenarios (APPROVED/PARTIALLY_SUPPORTED/NOT_SUPPORTED, unknown manufacturer/model, ONVIF/RTSP probe-failure resilience, Wi-Fi/wired parity), transport passthrough, defensive-input handling, no-credential-leakage, and `evaluate_scan_results()` batch-isolation tests (one item's evaluation failure never affects the others).
- `app/tests/test_scan_results_compatibility_wiring.py` — 9 tests: `secure_scan_results()` route-level wiring — APPROVED→`discovered`, PARTIALLY_SUPPORTED→`needs_review`, NOT_SUPPORTED excluded from `cameras` but visible in stored results, mixed-batch isolation, Wi-Fi/wired parity through the real adapter, credential-stripping ordering preserved, non-`complete` status is a no-op, missing-id fallback. Named to sort alphabetically after `test_cloud_readiness.py`, for the same pre-existing test-discovery-order reason documented in `test_live_view_page_characterization.py` — the file was originally named `test_camera_discovery_scan_wiring.py`, reproduced that known fragility by sorting before it, and was renamed (no other change) once discovered.

**Result: 32/32 and 9/9 passing on their own; full `app/tests/` suite (`py -3.14 -m unittest discover`, the Windows-native environment established in Phase 3 as this host's working full-suite runner): 156/157 passing; the sole error is the same pre-existing, unrelated Windows-only `os.symlink()` privilege limitation already documented under Phase 3 above — zero feature regressions.**

**Known limitations:**
- Reason codes/status live in `camera_scan_jobs.results_json` and, for `cameras.status`, as `discovered`/`needs_review` values — there is no dedicated `cameras` column for reason codes; a schema migration would be needed for that, deliberately not done.
- The existing customer-setup wizard's "Save camera setup" step still unconditionally writes `status='configured'` for any camera it lists, with no compatibility awareness — a `needs_review` camera can still be confirmed by the customer, but only after having already seen its flagged status one step earlier in the same wizard. Not changed, to preserve existing onboarding UI exactly.
- Devices behind non-standard RTSP/ONVIF ports (outside 554/8554 and 80/8000) are reported as `rtsp_supported=False`/`onvif_supported=False` even if genuinely compatible on a different port — a pre-existing `discovery.py` detection blind spot, unchanged by and not fixable from this cloud-side feature.
- **Wi-Fi vs. Ethernet transport cannot be reliably determined by ONVIF/IP discovery today, by design and by evidence** — confirmed during design review that no signal available to `discovery.py` (WS-Discovery, TCP probes, ARP) reveals physical connection medium; the engine never guesses it, and `discovery.py` never populates it.

**Committed and pushed as `f8053e2`** ("Camera discovery: add reusable IP camera compatibility detection"), confirmed synchronized with `origin/build/v1.2-modular-foundation` at that commit.
