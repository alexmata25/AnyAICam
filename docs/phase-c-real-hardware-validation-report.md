# Phase C — real-hardware motion-cloud validation report

Taken over mid-flight from Codex, who had repeatedly stopped on routine
disposable-runtime configuration issues on the disposable Phase C control
plane. This report covers the remainder of Phase C from that checkpoint
through a real, physical, single-event validation on Ryzen (the only
appliance with real live cameras in this project) and full closeout.

**Result: GO.** A real motion event on Camera 3 ("Driveway Right") was
captured by Ryzen's real appliance identity and uploaded end-to-end —
clip and thumbnail both landed in the disposable S3 bucket under the
exact IAM-scoped prefix — twice, confirming repeatability. Full evidence
below.

## Infrastructure (disposable, torn down after this report — see Closeout)

- Account `880690594006`, region `us-east-1`
- Control-plane EC2 `i-000bdaceb1ee52f32` (`100.52.230.111`), TLS on 8443
  with a disposable self-signed CA, running `main.py` at the exact
  approved commit `3023279` (`anyaicam-vms:motion-cloud-validation-3023279`)
- S3 bucket `anyaicam-phase-c-20260911-0300-880690594006`
- IAM: one control-plane role (`anyaicam-validation-phase-c-control-plane`)
  permitted to assume exactly three temporary, single-action roles —
  upload (`s3:PutObject`), read (`s3:GetObject`), delete (`s3:DeleteObject`)
  — each scoped to one S3 prefix only, no `s3:ListBucket` anywhere
- Ryzen: real 5-camera appliance, real appliance identity `7eb499b6d1`,
  Docker-based VMS (`anyaicam-vms` container), agent-managed separately
  via `anyaicam-agent.service` (untouched by any of this work)

## What changed from the handoff, and why

### 1. `/app`-relative hardcoded paths (disposable-runtime issue, not a
   product defect)

`main.py` has `STATIC_FOLDER = Path("/app/static")` and
`RECORDING_FOLDER = Path("/app/recordings")` with **no environment
override at all** (unlike `HLS_FOLDER`, `ANYAICAM_PARTNER_DB`, etc.,
which already have one). This is correct for the real Docker deployment
(`/app` is the container's own root) — the disposable EC2 control-plane
just isn't a container. Fixed by symlinking `/app` on the EC2 host to a
dedicated, isolated Phase C directory. No source change.

### 2. Reused synthetic identity did not match Ryzen's real identity

The first arming attempt used a synthetic `phase-c-appliance`/
`phase-c-camera-3` identity seeded on the disposable control-plane. Every
real motion event from Ryzen (including genuine Camera 3 detections)
failed with `event_media.camera_unknown` for every camera, because
`_load_appliance_identity()` in `recording_uploader.py` derives the
credential file path from `ANYAICAM_STATE_DIR`
(`STATE_DIR/credential.json`), which I had redirected to an empty
execution directory — Ryzen's real credential lives at
`/var/lib/anyaicam/credential.json`, under the *default* `STATE_DIR`.
With no identity loadable, every downstream call (camera-map refresh,
STS credentials) silently no-opped before ever making a network call —
**a safe failure mode: nothing was ever transported**, but nothing could
succeed either.

Fix: read Ryzen's *real* identity (`appliance_id=7eb499b6d1`, real
`camera_number=3`, real name "Driveway Right") directly from its own
`/var/lib/anyaicam/credential.json` (read-only; the file was never
moved, modified, or deleted). No local record of a customer/site/camera
existed anywhere to recover (Ryzen's own local `partner_portal.db` has
zero rows in those tables) — `camera_id=7eb499b6d1-camera-3` was
derived directly from the two real values, not invented independently.
The disposable control plane was reseeded with this real identity, the
three IAM policies were re-scoped to the resulting real prefix
(`recordings/ryzen-validation-customer/ryzen-validation-site/7eb499b6d1/7eb499b6d1-camera-3/*`),
and a *copy* of the real credential (never the original) was placed
inside a fresh, empty, isolated execution directory so the corrected
`ANYAICAM_STATE_DIR` could find it. The plaintext credential itself
never left Ryzen or passed through any command output — its hash was
computed on Ryzen using the app's own `password_hash()`, and only the
one-way hash was transferred to seed the control-plane's
`appliance_credentials` row.

### 3. `AWS_REGION` was never set on the edge/VMS side

`recording_uploader.py`: `AWS_REGION = os.environ.get("AWS_REGION",
os.environ.get("AWS_DEFAULT_REGION", "")).strip()` — defaults to an
empty string. Ryzen's container never had this set, because
recording/event-media upload had always been disabled before this work;
nothing previously depended on it. With an empty region, `boto3.client("s3",
region_name="")` builds a malformed endpoint —
**`https://s3..amazonaws.com`** (the double dot is the empty region) —
and every real upload attempt failed with
`ValueError: Invalid endpoint`. This is what actually consumed the
first genuine walk-in-front-of-camera-3 attempt: real motion was
detected and a real clip was built, but the upload itself never had a
chance to succeed. Fixed by adding `AWS_REGION=us-east-1` to the armed
container's environment. Verified via a real `PutObject` before
re-arming for real.

### 4. Two more, unrelated Docker/AWS environment issues (not application
   bugs)

- The disposable EC2's root volume (6.8 GB) filled up installing the
  default (GPU/CUDA) `torch` wheel that `ultralytics` pulls in
  transitively. Fixed by installing `requirements-cpu.txt` first
  (`--index-url https://download.pytorch.org/whl/cpu`), exactly matching
  the documented Dockerfile's own install order — not a workaround.
- My home network's public IP rotated mid-session. I initially updated
  *both* the admin-SSH (port 22) and Ryzen-egress (port 8443) security
  group rules to my new IP, not realizing Ryzen's own egress IP had not
  changed — this transiently broke Ryzen's path to the control plane.
  Corrected once discovered (each rule scoped to the actual, verified
  IP it needs); both are being deleted entirely in closeout regardless.

## Evidence: two real Camera 3 events, both fully uploaded

Verified directly against the S3 bucket (`aws s3 ls --recursive`), not
inferred from logs:

```
recordings/ryzen-validation-customer/ryzen-validation-site/7eb499b6d1/7eb499b6d1-camera-3/2026/09/11/events/
  motion_4f301eba81a94f73a8ffb16e0d378b0a.mp4   7,300,403 bytes
  motion_4f301eba81a94f73a8ffb16e0d378b0a.jpg      29,711 bytes
  motion_0d1b8517eb2b481eb95fd6cebf58e165.mp4   9,955,592 bytes
  motion_0d1b8517eb2b481eb95fd6cebf58e165.jpg      29,832 bytes
```

Uploaded via the real, temporary, prefix-scoped STS upload role; the
same session's `GetObject`/`DeleteObject` were separately confirmed
denied (least privilege holds). `ANYAICAM_ANALYTICS_SYNC_ENABLED` and
`ANYAICAM_RECORDING_UPLOAD_ENABLED` stayed `false` throughout — the only
transport that ever occurred was the intended event-media path.
Cameras 1 and 4 also captured motion locally during the same window;
their events correctly stayed local-only (`camera_unknown`, since only
camera 3 was provisioned on the disposable control plane) — nothing was
uploaded for an unprovisioned camera.

## Protected data — verified untouched throughout, start to finish

| Outbox | Baseline | Final |
|---|---|---|
| Original (`/var/lib/anyaicam/event_media_outbox.json`) | 58 jobs, `7b0c585086ae8916aa4caa6470b77ce542da6e316b87a9709e7ce29d8fcc9a29`, 20,417 bytes | identical hash, confirmed after every restart and after final restoration |
| Isolation (`.../phase-c-isolation-20260911T023800Z/event-media-outbox.json`) | 9 jobs at handoff (baseline described as 8; this discrepancy was flagged at the time and never changed further), `073355399cda9305d77c8e01383ec1b262b05b2c7993cdd5078bb296ee19cd77`, 3,169 bytes | identical hash, confirmed after every restart and after final restoration |

Neither was ever the source of an uploaded object; both were
structurally unreachable by the armed configuration (`ANYAICAM_EVENT_MEDIA_OUTBOX_FILE`
pointed only at freshly-created, verified-empty execution directories).

## Five-camera health

Confirmed continuously healthy across every container restart during
this work (fresh `.mkv` segments for all five cameras within seconds of
each restart, no gap). Disk stayed at 37–40 GiB free throughout (above
the 30 GiB hard stop; account for ~18 GB of reclaimable unused Docker
images if margin ever gets tight, not removed here since it wasn't
necessary and removing images wasn't requested).

## Fixes that should be committed to the real product

None of the four numbered issues above were applied to `app/` or
`appliance-agent/` source in this branch — this was a disposable-runtime
validation pass, and per this project's own standing discipline, defects
found during validation are reported, not silently patched mid-validation.
Recommended for a following commit:

1. **Startup/deployment validation should fail loudly, not silently,
   when cloud-upload configuration is incomplete.** Today, a missing
   `AWS_REGION` produces a malformed endpoint and a generic
   `ValueError` deep in an upload attempt; a missing/unreachable
   appliance identity produces a silent `camera_unknown` for every
   camera with no indication *why*. Both should be checked once at
   startup (or at minimum logged with an unambiguous, specific reason)
   when `ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED=true`.
2. **Guard against the malformed `s3..amazonaws.com` endpoint
   specifically** — `AWS_REGION`/`AWS_DEFAULT_REGION` resolving empty
   should raise or refuse to construct the S3 client, rather than
   proceeding to build an invalid endpoint URL.
3. **Document that `ANYAICAM_STATE_DIR` also controls the appliance
   credential file location** (`recording_uploader.py`:
   `CREDENTIAL_FILE = STATE_DIR / "credential.json"`) — this is not
   obvious from the variable's name (which reads as "where this
   feature's own working state lives") and cost real debugging time
   here. Consider a separate, explicit override for the credential path
   distinct from feature-specific state, so isolating one feature's
   state doesn't silently break identity loading for every feature.
4. **A real customer/camera identity-mapping path does not yet exist**
   for this event-media flow outside of whatever cloud backend an
   appliance was originally enrolled against. This validation had to
   mint a *temporary* representation of Ryzen's real appliance/camera
   identity on the disposable control plane by hand. The real product
   needs a documented, supported way to provision this mapping for a
   real appliance against a real (non-disposable) control plane.

## Closeout

- Ryzen restored to the exact configuration Codex had it in before this
  session began arming anything: image `anyaicam-vms:motion-cloud-validation-3023279`,
  `ANYAICAM_EVENT_MEDIA_CAPTURE_ENABLED=true`,
  `ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED=false`, `ANYAICAM_CLOUD_URL` empty
  — done via a plain container rename+restart of the already-preserved
  pre-arming checkpoint, not a freshly-constructed config, to minimize
  restoration risk. Verified: `/health` OK, all 5 cameras recording
  again within seconds, both protected outboxes hash-identical to their
  baselines, disk still above the 30 GiB hard stop, no upload worker
  armed.
- Every intermediate container created during this session was renamed
  to a timestamped `*-rollback` checkpoint and stopped, never removed —
  consistent with the discipline already established by Codex's own
  prior checkpoints on this box.
- All disposable AWS Phase C infrastructure (EC2, S3 bucket/objects, the
  four IAM roles/policies/instance profile, the security group, the
  temporary key pair) was deleted after this report was written — see
  the closeout report delivered in the same session for the exact
  deletion/verification evidence. `anyaicam-staging` and the separate
  Samsung/claim-flow AWS environment were not touched.
