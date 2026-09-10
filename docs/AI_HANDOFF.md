# AnyAiCam Claude-ready handoff

This file did not exist on `codex/authoritative-vms-reconciliation-20260907`
despite several in-code comments referencing `docs/AI_HANDOFF.md` (e.g.
`appliance-agent/anyaicam_agent/service.py`'s `check_for_source_update()`,
`appliance-agent/anyaicam_agent/commands.py`'s RDM4 comments) -- it exists on
other branches' earlier history but this branch's own lineage never carried
it forward. Recreating it here so anyone (human or Claude) picking this work
up next has one current starting point.

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

Branch: `staging/cloud-integration-repair`, commit
`85a0e5561fef41a910da2702f80bd53d91c184cc`. Not pushed. Not deployed. Samsung
untouched.

In one sentence: a fresh appliance could never finish activating at all
(`coordinated_reenroll()` required an identity that doesn't exist yet on a
new box), so nothing downstream -- cloud URL, credential access, the
update-check endpoint -- ever got configured; all three are now fixed and
tested with zero regressions against the existing ~1,700-test suite.

## What is still open

1. **Per-camera event-upload eligibility.** `cameras.cloud_recording_mode`
   exists in the schema and API but nothing in the upload path
   (`recording_uploader.py`, `event_media_uploader.py`) reads it -- only a
   single appliance-wide `ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED` flag gates
   uploads today. This is real, confirmed, and not fixed in Phase 1 --
   see `docs/phase1-staging-repair-report.md` §3 and §9.
2. **The full Cloud Motion / Cloud 24/7 product build**, using
   `docs/cloud-product-architecture-plan.md` as the architecture/phasing
   reference. That plan's Phase 1 (the shared staging repair) is what this
   pass completed; Phases 2-4 (Cloud Motion, Cloud 24/7, website/portal/
   installer integration) have not been started. That plan also depends on
   #1 above (per-camera entitlement) as its foundation.
3. **Live verification on an actual Linux/Docker host.** Everything in this
   pass was verified by direct code reading, path-constant matching, and a
   full before/after unit-test comparison -- there was no Linux target
   available to actually run the Compose stack and confirm things like
   in-container credential readability against a live filesystem. The
   staging installation plan (`docs/phase1-staging-repair-report.md` §6)
   is written specifically to close that gap before Samsung.
4. **37 pre-existing test failures**, unrelated to this work (confirmed
   identical failure list on the unmodified base commit) -- camera-numbers
   fallback, customer analytics/mobile-playback UI, PPE model loading,
   recording remux, retention worker, a couple of updater interlock edge
   cases. Not investigated further here; out of Phase 1's scope, but worth
   someone's attention.

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
