# AnyAiCam Claude-ready handoff

This file did not exist on `codex/authoritative-vms-reconciliation-20260907`
despite several in-code comments referencing `docs/AI_HANDOFF.md` (e.g.
`appliance-agent/anyaicam_agent/service.py`'s `check_for_source_update()`,
`appliance-agent/anyaicam_agent/commands.py`'s RDM4 comments) -- it exists on
other branches' earlier history but this branch's own lineage never carried
it forward. Recreating it here so anyone (human or Claude) picking this work
up next has one current starting point.

## Status right now

Branch `staging/cloud-integration-repair` is **pushed to origin**, tip
`8a83bf30a67b6a7434bed8fac44f16a9095d12ed`. Code and docs are staging-
ready. **Nothing has been deployed anywhere since the disposable EC2
instance from the edge-validation pass was terminated** (see
`docs/phase1-edge-validation-report.md` and
`docs/phase1-privileged-watcher-fix-report.md` for what was found/fixed
there, including the privileged-watcher packaging gap -- now fixed, but
not yet re-verified live on a real host; that's the first thing to check
whenever staging/edge validation resumes). `anyaicam-staging`
(34.194.19.113) -- the only host reachable under that name this session
-- turned out to be the **cloud control-plane** (portal + storefront +
Caddy, `ANYAICAM_RUNTIME_ROLE=cloud`, live Stripe keys configured), not
an edge-appliance host running the agent+VMS Docker Compose pair this
work changed; the appliance installer has never run there at all (no
`/etc/anyaicam`, `/opt/anyaicam`, `/var/lib/anyaicam`, no `anyaicam`
user). Full detail: `docs/phase1-staging-repair-report.md` §13. **Before
any further staging work, figure out where an actual edge-appliance-
shaped Linux/Docker target is** -- a fresh disposable instance
(`installer/README.md`'s own stated validation target) is the most
likely answer, which needs AWS credentials this session didn't have.

## Repository orientation (read this first)

- **`origin/main` and `origin/develop` are stale.** Both are frozen at the
  very first commit (`1eee376`, "Initial AnyAICam platform", 2026-08-05) --
  64 `app/*.py` files. **Do not treat either as canonical.**
- **`codex/authoritative-vms-reconciliation-20260907` (currently at
  `ec5272f`) is the real, current state of the product** -- 187 `app/*.py`
  files, the full `appliance-agent/anyaicam_agent/updater/` package,
  `event_media.py`/`event_media_uploader.py`/`analytics_sync.py`, etc. Any
  future work should branch from here, not from `main`, unless someone has
  since designated a newer canonical branch -- check `git log --oneline` on
  the candidates (`integration/universal-vms-*`, other `codex/*` branches)
  for anything more recent before assuming this one still is.
- There is also a Ryzen appliance and a Samsung appliance running their own
  deployed snapshots of this codebase, which may lag or diverge from
  whatever is currently canonical in git -- always confirm which commit is
  actually running on a given physical box before assuming the repo matches
  it (see the project's own standing note on this in the memory/handoff
  trail from earlier sessions).

## What Phase 1 (this pass) did

Fixed the Samsung staging lab's failed end-to-end activation. Full
root-cause report, security review, test results, staging plan, Samsung
checklist, and rollback procedure: **`docs/phase1-staging-repair-report.md`**
(read that file for the details -- not duplicated here).

Branch: `staging/cloud-integration-repair`, latest commit
`363c3466dcdff7316e0b5b3fc1bcedd07a7b6d47`. **Pushed to origin.** Not
deployed anywhere (see "Status right now" above). Samsung untouched.

In one sentence: a fresh appliance could never finish activating at all
(`coordinated_reenroll()` required an identity that doesn't exist yet on a
new box), so nothing downstream -- cloud URL, credential access, the
update-check endpoint, per-camera event-upload eligibility -- ever got
configured; all of it is now fixed and tested, with a sanitized installer
package built from the same commit
(`dist/anyaicam-appliance-installer-1.1.0-vms-50bdd6ed4d77.tar.gz`,
SHA-256 `93f840d50455c4ab211b259b2d3b57ead978fd398df85c0a03623a4dfc443208`).
Full details, including the per-camera eligibility fix and a documented,
reproduced-nonreproducible flaky test investigation: `docs/phase1-staging-repair-report.md`
§2a, §9-§12.

## What is still open

1. **Live verification on an actual Linux/Docker host.** Everything in
   this pass was verified by direct code reading, path-constant
   matching, and full before/after unit-test comparisons across four
   separate full-suite runs -- there was no Linux target available to
   actually run the Compose stack and confirm things like in-container
   credential readability against a live filesystem. The staging
   installation plan (`docs/phase1-staging-repair-report.md` §6) is
   written specifically to close this gap before Samsung, and is the
   next recommended step (§12's staging-readiness decision).
2. **The full Cloud Motion / Cloud 24/7 product build**, using
   `docs/cloud-product-architecture-plan.md` as the architecture/phasing
   reference. That plan's Phase 1 (the shared staging repair, now
   including per-camera eligibility) is what this pass completed;
   Phases 2-4 (Cloud Motion, Cloud 24/7, website/portal/installer
   integration) have not been started. Deriving `cloud_recording_mode`
   automatically from a purchased entitlement/plan (rather than today's
   manual admin-set route) is part of that future work, not this pass.
3. **37 pre-existing test failures**, unrelated to this work (confirmed
   identical across four separate full-suite runs spanning the
   unmodified base commit through both follow-up commits) -- camera-
   numbers fallback, customer analytics/mobile-playback UI, PPE model
   loading, recording remux, retention worker, a couple of updater
   interlock edge cases. Not investigated further here; out of Phase 1's
   scope, but worth someone's attention. Separately, the suite also has
   at least one genuinely flaky (non-deterministic) test,
   `test_talk_audio_relay.py::test_no_appliance_channel_uses_local_isapi_fallback`
   -- confirmed by an immediate identical re-run producing a different
   result -- worth the team knowing about even though it wasn't caused
   by, and doesn't block, this work.

## Standing constraints that carried through this pass

- `ANYAICAM_ANALYTICS_SYNC_ENABLED` and `ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED`
  are administrator-controlled flags that must stay explicitly set once
  turned on -- nothing in this pass touches either.
- No production deployment, no Samsung install/restart, no camera
  discovery, and no Stripe/pricing/subscription changes without their own
  separate, explicit approval each time.
- Every phase of work on this project is expected to get its own explicit
  authorization before implementation begins -- this file, along with
  `docs/phase1-staging-repair-report.md` and
  `docs/cloud-product-architecture-plan.md`, is what the next session
  (human or Claude) should read before starting the next one.
