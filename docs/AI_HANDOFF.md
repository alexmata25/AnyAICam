# AnyAiCam — AI Handoff / Architecture Source of Truth

Maintainers: update this file at the end of every significant session, before context is lost. This file — plus committed code and Git history — is the source of truth. Do not reconstruct architecture decisions from chat memory; read this file first.

Last updated: 2026-08-12 (Claude Code audit session, took over from Codex)
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
- Possible drift between the committed `ecs-task-definition.json` (`ANYAICAM_RUNTIME_ROLE=cloud`) and whatever the live EC2 instance is actually running — needs to be confirmed operationally, not assumed from the repo.
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

**Architecture status: APPROVED — the direction above (corrected media format, direct-to-S3 data plane, STS-scoped credentials, control/data-plane separation, hybrid on-demand publishing) is the accepted design.**
**Implementation status: Phase 0 (characterization tests) APPROVED and COMPLETE. Phase 0.1 (hashlib fix + cleanup checkpoint) COMPLETE, pending commit approval. Phase 1 and all subsequent phases are NOT YET AUTHORIZED — each requires a separate, explicit go-ahead before any transport code is written.**

