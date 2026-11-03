# Gateway development handoff

Updated 2026-11-02. Continue on existing main, application and installer branches;
preserve their commits. No deployment or live-system acceptance is implied.

Consolidated current readiness and exact owner decisions:
`gateway-consolidated-readiness.md`. The final bounded review found no further
proven package-independent task; no unchanged tests or recovery searches were rerun.

## Current references

- `windows-gateway-resource-provenance.md`: exact reusable assets, all eight
  vendor filenames, wheel lock, seals, targeted recovery result and staged inputs.
- `gateway-offline-remote-deadline-proposal.md`: authoritative expiry data path,
  signed-grant proposal, unresolved policy and durable-revocation limitations.
- `windows-gateway-beta-acceptance-checklist.md`: unified acceptance matrix;
  installed-system rows remain NOT RUN.
- `windows-gateway-editions-checkpoint.md`: validated source milestones.

## Completed in the autonomous pass

- One targeted read-only recovery pass used previously unreviewed external-drive
  transfer metadata. It describes source bundles, not the sealed parity payload.
  No package candidate, extraction or manifest/member verification was possible.
- Historical keyset digest `2389a655...af7c` is explicitly labelled production
  in the existing build checklist. Synthetic DO-NOT-SHIP keys are separate.
  No key was generated, replaced or promoted to a trust anchor.
- Lifecycle comparisons reject missing or mismatched computer/install/data-root
  identities, preventing unrelated target snapshots from forming PASS evidence.
- Notification retrieval now matches the existing fanout contract for a viewer
  whose stored mode is all and who has no explicit permission rows. Own-recipient
  and customer scope, explicit can_alerts denial, selected-mode denial and
  camera-less alert exclusion remain enforced. Earlier removed-camera coverage
  is preserved in the integrated tree.

## Remaining blockers and next actions

Validation: 13 focused installer configuration/lifecycle tests passed in
25.771s; 8 application notification tests passed in 3.002s; integrated fixed
sequential runner passed **130 tests in 34.894s**. Diff check passed. These are
isolated source/SQLite/synthetic-snapshot checks, not installed acceptance.

1. Recover the unchanged sealed `R430-win134-parity-d6b4244` from an exact
   accessible original/backup path. Last documented origin is the 2026-10-28
   AlexDellLaptop handoff, section 6, naming
   `C:\AnyAiCam-Transfer\R430-win134-parity-d6b4244`, sealed 2026-10-27.
   No current verified copy or alternative backup location is established.
   Owner action: identify/authorize that exact source or transfer the unchanged
   package and original SHA256SUMS to an accessible development location.
   Verify the recorded manifest seal and every member before use; request a
   standalone keyset/Tesseract only if absent from the recovered package.
2. Independently confirm the current intended production public-key environment
   and digest. Historical labels do not approve arbitrary recovered bytes.
3. After verification, resolve exact Python/Inno install scope, candidate metadata,
   current signing session and measured build window. Existing Python installer,
   SDK, baseline setup and source bundle must be reused rather than requested anew.
4. Windows installation/UI/media acceptance needs an explicitly disposable target,
   recoverable rollback image/checkpoint, isolated identities and synthetic media.
   Historical AAC-WinVMS-1341-Test records do not establish present readiness.
5. Offline remote expiry requires the authoritative grant/migration contract
   decisions in the proposal. No deadline or grace is invented. Process-local
   cancellation write-failure denial is implemented; durable restart revocation
   remains unresolved.

The bounded application/installer review found no further proven independent
defect after these fixes. Resume with new evidence or the above inputs; do not
repeat exhausted searches or represent source fixtures as installed acceptance.

## Boundaries

Remote cancellation/expiry must preserve independently licensed local recording,
LAN viewing and local playback. No production/R430/Ryzen changes, live billing,
retention changes, security bypasses, replacement keys or VM operations are
authorized. Before a RAM-intensive workload, present its machine, estimated RAM,
fresh available memory and duration, then obtain explicit owner approval.
