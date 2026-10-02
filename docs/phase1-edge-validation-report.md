# Phase 1 edge-appliance validation — disposable EC2 instance

Companion to `docs/phase1-staging-repair-report.md` (see its §13 for why
this couldn't run on `anyaicam-staging`, the shared cloud host).

Instance: `i-0ff5b0e6c0255eb29`, `t3.xlarge`, Ubuntu 24.04, `us-east-1a`.
**Now stopped** (not terminated — awaiting review). Code validated at
commit `cb1e943366b06d9595948df8347ce3cae7ca6973`, branch
`staging/cloud-integration-repair` (pushed to origin).

No production deployment. No Samsung install/restart/config change. No
camera discovery, no real camera contact, no customer records — every
identity/camera-adjacent value used below is synthetic
(`AIC-SYNTH0001`, `synthetic-appliance-0001`, etc.), created directly via
the real installed code, not through any UI or activation-token flow.

## Transparency note: two accidental secret prints

While inspecting `/etc/anyaicam/vms.env` twice during this session, the
full file was `cat`'d rather than filtered to key names only, which
printed the live values of `ANYAICAM_APP_SECRETS` and
`ANYAICAM_CAMERA_CREDENTIAL_KEY` (both generated fresh by this install,
specific to this one disposable instance) into this session's own
transcript. Neither is an AWS credential, a customer secret, or anything
reused elsewhere — they exist only on this box, which is now stopped and
will be terminated once you're done reviewing, at which point they stop
existing entirely. Flagging this plainly rather than not mentioning it:
every other credential check in this report was done by key name only
(`cut -d= -f1`) or by grepping for a pattern, not printing full file
contents, and that should have been consistent throughout.

## Three real, confirmed defects found live — two fixed, one documented

None of these were visible from static analysis; all three only showed
up by actually running the built package on a real target, which is the
entire reason this validation pass exists.

### 1. Fixed: installer package was missing `requirements-cpu.txt`

`docker compose build` failed on the very first install attempt --
`COPY requirements-cpu.txt /tmp/requirements-cpu.txt: not found`. Both
`Dockerfile` and `Dockerfile.production` need it; `build_release_
installer.py`'s `REQUIRED_RELEASE_PATHS` only listed `requirements.txt`.
Fixed (commit `0966fdf`), plus a new regression test that parses both
real Dockerfiles for every COPY source and asserts each is in the
release allowlist -- this exact class of bug (a Dockerfile referencing a
file the packaging tool doesn't ship) can't recur silently now.

### 2. Fixed: `GET /ready` 500'd on every install

`camera_status(request: Request)` requires a request (for its customer-
portal session-scoping branch), but three callers -- `readiness_
snapshot()` (backs `GET /ready`), `health_monitor()` (a background
worker), `site_monitoring_summary()` -- called it with zero arguments.
Confirmed live: `TypeError: camera_status() missing 1 required
positional argument: 'request'` on an otherwise healthy, correctly
installed appliance. Fixed (commit `a14b03d`) by routing all three
through `_legacy_camera_status()` directly -- none of them have or need
a customer session. This turned out to be the confirmed root cause of
two tests that have been sitting in this branch's "pre-existing,
unrelated" failure baseline through every full-suite run this whole
session (`test_readiness_reports_zero_cameras_total_for_a_fresh_
appliance`, `test_readiness_reports_the_real_count_for_dynamic_
cameras`) -- both now pass; see the regression numbers below.

### 3. Fixed: `restart_vms` ran `docker restart`, which never reloads `vms.env`

Confirmed live: after writing `ANYAICAM_CLOUD_URL` into `vms.env`
(exactly what `setup_wizard.py`'s post-activation step does),
`docker restart anyaicam-vms` left the running container's environment
completely unchanged -- `docker restart` reuses whatever environment the
container was created with. `docker compose --project-directory
/opt/anyaicam up -d`, by contrast, was confirmed live to diff the
resolved config (env_file included) and recreate the container only
when something changed, picking up the edit within one call. Fixed in
`privileged_watcher.py`'s `DISPATCH` table (commit `cb1e943`).

### 4. Found, NOT fixed: the privileged-watcher mechanism isn't actually installed anywhere

While fixing #3, found that `appliance-agent/system/` (the watcher
script plus its two systemd units) is a completely different directory
from `appliance-agent/systemd/` (just `anyaicam-agent.service`), and
`build_release_installer.py`'s `agent_paths` only packages the latter --
the watcher is never even shipped in the installer artifact, and no
installer script installs/enables its systemd units even if it were.
**The reboot_appliance/restart_vms privileged-action mechanism does not
function on a real installed appliance today**, independent of fix #3
being correct. This is a real, confirmed, out-of-scope-for-this-pass
finding -- packaging the watcher and wiring it into `08-systemd-setup.sh`
is a separate task, not attempted here given the size of this pass
already. Documented, not silently left for someone to rediscover.

## Checklist results

| Item | Result |
|---|---|
| Clean installation | **Pass**, after fix #1. First attempt failed on that missing file; a genuinely clean run (`userdel anyaicam` + `uninstall.sh --purge-all` first, confirmed `0/5 -> clean` then `5/5 -> existing` after) completed with `docker compose build` succeeding and every service starting. |
| Service and container health | **Pass** for the signals actually used elsewhere (Docker healthcheck `healthy`, `GET /health` 200, `anyaicam-agent.service`/`anyaicam-vms.service` active, `GET /version` reports the exact approved commit). `validate.sh`'s separate `GET /ready` check still fails, but for a *different*, pre-existing reason than the crash fixed above: `readiness_snapshot()`'s `startup_self_test()` requires full AWS production config (region, S3, Secrets Manager, HTTPS enforcement) that a plain edge appliance never has -- a real, confirmed, but out-of-scope design mismatch (edge role vs. an AWS-production readiness bar), not something this pass fixes. |
| Read-only credential mount | **Pass, directly confirmed.** `docker exec anyaicam-vms cat /var/lib/anyaicam/credential.json` succeeds and returns the synthetic identity written by `first_enroll()` on the host; a write attempt from inside the container (`echo test > .../credential.json`) was rejected with `Read-only file system`. This is the central Phase 1 fix (root cause §1.3 in the staging report), now proven on real infrastructure, not just by path-constant matching. |
| Required VMS environment variables | **Pass.** All six installer-provisioned vars present (`ANYAICAM_RUNTIME_ROLE`, `ANYAICAM_ENV`, `ANYAICAM_APP_SECRETS`, `ANYAICAM_CAMERA_CREDENTIAL_KEY`, `ANYAICAM_VMS_COMMIT`, `ANYAICAM_BUILD_ID`), plus `ANYAICAM_CLOUD_URL` written by the real `_upsert_vms_env_key()` and confirmed present in the running container after the (now-fixed) restart handoff. |
| Installer rerun preserves files/config | **Pass.** A synthetic recording file, `vms.env`, the VMS identity file, and the agent's `credential.json` were all byte-identical (sha256-verified) before and after a full `install.sh` rerun; the pre-existing appliance identity was explicitly logged as "preserved, not regenerated." |
| No permanent AWS credentials or secrets exposed | **Pass**, with the transparency note above. No `AKIA`/`ASIA` pattern anywhere in container env, `docker-compose.yml`, or `vms.env`. The container runs as root (pre-existing, already flagged in the staging report's security review) and can therefore read the host's `0600 anyaicam:anyaicam` credential file regardless of UID matching -- expected, not a new finding. |
| Local recording and analytics survive simulated cloud failure | **Pass.** With `ANYAICAM_CLOUD_URL` pointed at a genuinely unreachable host and `ANYAICAM_RECORDING_UPLOAD_ENABLED`/`ANYAICAM_ANALYTICS_SYNC_ENABLED`/`ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED` all temporarily enabled to force real network attempts, the container stayed `healthy` with zero restarts over a sustained window; logs showed clean `control_plane_unreachable` warnings, never a traceback; local recordings directory remained writable throughout. Flags reverted to their default-off state afterward. |
| Rollback procedure works | **Pass.** Backed up `vms.env`/`docker-compose.yml`, deliberately broke the running config (`ANYAICAM_RUNTIME_ROLE` set to an invalid value, container recreated to pick it up), then executed the documented rollback (`docker compose down`, restore backups, `docker compose up -d`) -- container returned to `healthy` with the correct role and a clean `/health` response. |
| Camera discovery / real cameras / customer records / `talk_isapi_diagnostic` | **Not touched**, as instructed. No discovery run. No Samsung contact of any kind (this instance is fully isolated infrastructure). |

## Regression suite (after all three fixes)

Full `app/tests` + `appliance-agent` run at commit `cb1e943366b06d9595948df8347ce3cae7ca6973`:
**1707 passed, 36 failed, 18 skipped.** 35 of the 36 are the same
pre-existing set (down from 37 -- the `camera_status` fix independently
resolved two of them, confirmed both by direct isolated test runs and by
this full-suite count). The 36th,
`test_talk_audio_relay.py::test_no_appliance_channel_uses_local_isapi_
fallback`, is the same non-deterministic timing flake already documented
in `docs/phase1-staging-repair-report.md` §10 -- it resurfaced on this
run and was absent from the immediately-preceding one with zero code
changes in between, exactly the pattern already recorded there. No new
regression from this validation pass.

## Instance disposition

Stopped, not terminated -- `dist/`, the local dedicated SSH keypair, the
security group, and the EC2 key pair are all still in place so this can
be resumed or re-inspected without re-provisioning. Awaiting review
before termination. Cost while stopped: EBS storage only (no compute,
no data transfer, no public IP retained).
