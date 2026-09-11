# Ryzen appliance — checkpoint

**Read `docs/PROJECT_CHECKPOINT.md` first.** This file assumes that context.

**Do not re-image, re-provision, or "clean up" Ryzen from this checkpoint.**
Its role is the primary real-hardware validation appliance — 5 real cameras,
real recordings, a real activated customer identity. Everything below is
carried forward from the last session that actually touched Ryzen; this
reconciliation session (2026-09-11, `reconcile/golden-foundation-20260911`)
deliberately did **not** SSH into or modify Ryzen, per explicit instruction,
so treat every fact below as "true as of the last time someone checked" and
re-verify anything load-bearing (disk usage, running commit, container
health) before acting on it.

## What software Ryzen is running right now

Ryzen is running an **old, pre-reconciliation build**, patched at runtime,
not through source control. Concretely:
- The VMS container is pinned to a specific tag from earlier this session's
  Motion Cloud validation work: `anyaicam-vms:motion-cloud-validation-3023279`
  (commit `3023279`) — this predates almost everything in
  `reconcile/golden-foundation-20260911`.
- The agent installed on Ryzen does **not** have `anyaicam-setup --claim`
  support, confirming its `appliance-agent` package is old — well before the
  staging claim-flow work landed.
- Several of tonight's fixes exist on Ryzen **only as manual runtime edits**
  to env files (`vms.env`) — specifically `AWS_REGION`, `ANYAICAM_STATE_DIR`,
  and `ANYAICAM_CLOUD_URL` — not as code changes and not as anything that
  would survive a container rebuild or reinstall. These are exactly the kind
  of fix `docs/PROJECT_CHECKPOINT.md` classifies as TEMPORARY DIAGNOSTIC, not
  a PERMANENT PRODUCT FIX.

**Implication:** the AWS_REGION guard, the `restart_count` migration, the
degraded-vs-offline distinction, and the JS-syntax fix all now exist in the
golden branch's *source*, but Ryzen's actually-running software does not
contain that source — it's running old code plus hand-edited env vars for
one of the six items. **Ryzen needs an eventual upgrade to a real built
golden image**, once one exists, to make these fixes permanent on Ryzen
itself rather than dependent on env files nobody will remember to recreate.

## Cloud/customer identity (as activated this session)

- Customer email: `alexmata25@gmail.com`
- `customer_id`: `4efaf5153f`
- `site_id`: `4de6186be8` (site name recorded as "Ryzen Home Site")
- `appliance_id`: `5e76625989`
- `cloud_id`: `AIC-C90CF0C9`

This activation is real — done by SSHing directly into Ryzen and running the
real `anyaicam-setup` against `anyaicam-staging`, not simulated.

## Camera / recording state

- 5 cameras discovered, configured, and recording locally (ONVIF/RTSP →
  FFmpeg → HLS/recording pipeline).
- Live Relay is active: real segments upload from Ryzen to S3, playback via
  CloudFront-signed URLs.
- Multi-camera grid Live View (`/customer-live`) — confirmed working with
  real video.
- Dedicated single-camera Live View (`/customer/cameras/{id}/live`) — the
  malformed-JS-template-literal bug and the degraded-vs-offline bug are both
  fixed in source (golden branch) and verified via automated checks
  (esprima JS-syntax parse + a live HTTP status check), **but Alejandro's
  own browser confirmation that he sees video on this specific page has not
  yet been received** as of this checkpoint. Treat this as open until he
  confirms directly — do not mark it done based on the automated checks
  alone.

## Known remaining issues on Ryzen specifically

1. **Portal URL / cloud identity persistence — not fully closed for
   Ryzen's exact failure mode.** The `f613b2b` precedence fix (now in the
   golden branch) stops an installer's untouched bootstrap placeholder from
   overriding a real activation value. It does **not** cover the case Ryzen
   actually hit: a real, non-default, but *stale* value already sitting in
   `vms.env` from a previous activation. That gap is still open — see item 3
   in `docs/PROJECT_CHECKPOINT.md`'s defect list.
2. **Disk usage around ~91.9%**, which is what pushes
   `online_status` to `degraded` rather than `online` — this is why the
   degraded-vs-offline distinction mattered in the first place. Disk usage
   itself has not been remediated (no recordings deleted, per explicit
   instruction not to touch retained recordings).
3. **A `restart_count` telemetry anomaly was observed but not fully
   root-caused** — noted, not yet investigated further.

## Exact next step

1. Do not act on this checkpoint without first re-verifying live state
   (SSH in, check the actually-running image tag, check disk usage, check
   `vms.env`) — this file was last updated without touching Ryzen, so drift
   since is expected, not a surprise.
2. Once a real golden build/image exists (see `docs/PROJECT_CHECKPOINT.md`
   → Authoritative branch), the next real step for Ryzen is an actual
   upgrade to that build through whatever the golden release's normal
   update mechanism is — not another hand-patch of `vms.env`.
3. Get Alejandro's own browser confirmation on the dedicated single-camera
   Live View page before considering that bug fully closed end-to-end.
4. Do not delete recordings, do not rediscover/re-enroll cameras, do not
   disable the working multi-camera relay, and do not change Motion Cloud
   settings on Ryzen without explicit instruction — all of this is proven,
   working, real-hardware-validated behavior.
