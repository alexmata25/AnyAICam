# Windows 1.3.4.1 OCR baseline and shutdown investigation

The unchanged strict OCR suite remains 8/10, not 10/10. Both failures are
repeats of one inherited image, not two different plates. All predictions match
the accepted legacy engine. No OCR engine, model, preprocessing rule, recognition
expectation, confidence threshold or production LPR code was changed. Variant
experiments stopped when the owner instructed; none was promoted.

## Exact inherited results

| Repeat | Image | Expected | Minimal actual | Legacy actual | Native exit |
| --- | --- | --- | --- | --- | --- |
| 1 | plate2.png | 7XYZ482 | TXYZ482 | TXYZ482 | 0 |
| 2 | plate2.png | 7XYZ482 | TXYZ482 | TXYZ482 | 0 |

| Image | Expected | Minimal in both repeats | Legacy | Strict result |
| --- | --- | --- | --- | --- |
| plate1.png | ABC1234 | ABC1234 | ABC1234 | PASS |
| plate2.png | 7XYZ482 | TXYZ482 | TXYZ482 | FAIL |
| plate3.png | KDN5021 | KDN5021 | KDN5021 | PASS |
| plate4.png | HT88K2P | HT88K2P | HT88K2P | PASS |
| plate5.png | 9MPR317 | 9MPR317 | 9MPR317 | PASS |

Zero OCR process timeouts; each process had a 30-second limit. Harness exit is 1
because of the two text mismatches, despite native processes returning 0.

- Image plate2 SHA256: 4d4d8e792908d2f89c52151c1dab47e6ab48fdf919333c85b4506fe45d8dd48c
- Working engine SHA256: ba579ac5e6dbde80a6d4806356b2118fef008a3dfc0f062e0cdba77540e10d88
- English model SHA256: 7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2
- Preserved legacy report SHA256: f1677bc657b9f0b7f8398a7b2d4a983b3d2e7c2466eb3f83a62ea3119363f642

[Fresh strict results](C:/Hyper-V/AAC-WinVMS-1341-Test/Integration/Artifacts/OCR-Followup-20261028/OCR-Followup-20261028-Strict/ocr-acceptance.json).
Previous reproducible build and notice/dependency evidence remain unchanged in
the separate Minimal-Tesseract-20261028 artifact backup.

## Failure location and rejected experimental changes

Raw Tesseract already reads TXYZ482. The existing grayscale/3x/Otsu fallback
also returns TXYZ482 (confidence 55). Glyph extraction finds seven components
and a seven-component row, but the existing glyph-image stage reads WALESL
(PSM 7) / TWAHLASL (PSM 8), which plausibility checks correctly reject. The
fallback therefore does not fix the inherited line-recognition error.

This does not prove a wrong live camera event: full detector/camera acceptance
was not possible. The Windows guest lacks
C:\ProgramData\AnyAiCam\models\lpr\license-plate-finetune-v1n.pt.
The supplied images are already-cropped synthetic plates, not labeled vehicle
frames. Model availability and licensing gates were not bypassed. The isolated
detector entry point returned no detection for these plate-only inputs.

Before the stop instruction, 11 baseline/configuration/preprocessing profiles
and 10 stroke/scale profiles (including a repeated raw baseline) were evaluated.
The latter used all five originals plus 32 controls spanning two fonts, two
stroke thicknesses and actual leading/middle T and 7 characters. No profile
corrected the complete original five-image set. Examples of measured regressions:

| Attempt | Previously correct expectation | New incorrect read |
| --- | --- | --- |
| 3x grayscale + Otsu | plate1 ABC1234 | ABC1254 |
| 3x grayscale + Otsu | plate3 KDN5021 | KDNS021 |
| 2x grayscale | plate3 KDN5021 | KDNS5021 |
| 2x grayscale | plate5 9MPR317 | 9MPR3S17 |
| 0.75x area downsample | plate3 KDN5021 | KDNS021 |
| 2x2 stroke thinning | plate5 9MPR317 | Q9MPR317 |
| 1.25x horizontal stretch | plate1/plate3/plate5 | all three regressed; only 1/5 originals passed |

Half-size scaling improved the targeted controls from 17/32 to 21/32, but still
left plate2 wrong and did not improve the original 4/5 score. A diagnostic-only
leading-T-to-7 replacement would fix plate2 but breaks eight previously correct
genuine T controls. It was rejected, not implemented. Isolated character mode
reads this 7 correctly, but that observation is insufficient to authorize a
production character-substitution policy or to change the raw acceptance test.

The generator describes these smoke fixtures as already enlarged; their
behavior is not evidence for removing preprocessing intended for real small
camera crops. No further preprocessing experiments are planned under this task.
Detailed JSON and the exact diagnostic scripts/images are retained in this
artifact directory, including degraded outputs.

## Pending asynchronous task warning: cause and application impact

The observed warning names _finalize_ai_activity, the AI activity media
finalizer, not an OCR coroutine. The background_loop fixture in
app/tests/test_ppe_lpr_facial_event_media_gap_audit.py stopped the loop, joined
its thread and closed it without cancelling/awaiting outstanding tasks.
That can garbage-collect a finalizer still sleeping until its activity ends.
The production coroutine deliberately does no blocking cleanup on GeneratorExit;
ordinary cancellation takes a different owner-release path.

The only correction is fixture teardown: cancel and gather outstanding tasks
while its own loop is running, shut down async generators/executor, then stop,
join and close, with a five-second outer drain bound and two-second thread join.
No warning filter was added. No test assertion was removed, skipped or marked
expected-failure. The pending-task warning changed from one occurrence before
to zero after; the same two pre-existing PPE assertions still fail.

Four source-extracted, controlled lifecycle probes passed:

1. Closing without cancellation reproduces the warning.
2. Cancel plus await releases the owner before loop close, without the warning.
3. asyncio.run cancels an untracked finalizer through its normal cancellation path.
4. The actual lifespan shutdown body logs shutdown.complete while an untracked
   finalizer remains pending; outer-runner cleanup cancels it afterward.

Installed Uvicorn 0.34.0 Server.run invokes asyncio.run(self.serve(...)); its
source and the embedded stdlib Runner.close bytecode are preserved in
OCR-Followup-20261028/uvicorn-runner-source.txt.

Conclusion: the specific warning is caused by the test harness, and controlled
normal-runner cancellation works. It would be inaccurate to claim application
shutdown is fully cleared. The application does not explicitly own/await these
finalizers in its lifespan shutdown list. An in-flight finalizer can be cancelled
rather than finish its clip; shutdown.complete is not proof of finalizer/media
completion. Windows service-stop and real in-flight recording/upload behavior
were not tested, and production code remains unchanged.

A separate lifecycle follow-up would explicitly track the scheduled finalizer
futures and define bounded drain/cancel behavior before shutdown.complete,
with appropriate media-completion tests. No such production change is made here.
The characterization probe records the current gap, not desired release behavior.

## Actual regression evidence

| Run | Pass | Fail | Notes |
| --- | --- | --- | --- |
| Core LPR, nine files | 114 | 0 | Includes actual OCR on existing tilted/slanted fixtures |
| Related integration, three files | 11 | 2 | Two PPE event assertions fail on unchanged baseline code |
| Shutdown characterization | 4 | 0 | Controlled actual-function/source-body probes; no service startup |
| Same media fixture after teardown correction | 1 | 2 | Same PPE failures; pending-task warnings 0 |

The PPE failures remain test_ppe_event_in_same_scan_shares_that_scans_clip and
test_two_real_consecutive_scans_of_the_same_person_produce_one_ppe_event: no PPE
event was emitted. They were not attributed to OCR or hidden by the cleanup fix.
The targeted rerun repeats three tests; it is not added to unique coverage counts.
All completed regression runs reported zero external connection attempts.

The first combined collection suffered one native PyTorch access violation
(0xC0000005), before tests ran. Three fresh cv2/torch import probes with/without
the engine PATH and both import orders passed; subsequent split collections ran.
The initial crash remains preserved and unexplained, not declared fixed.

## Precise OCR acceptance requirement and exception policy

Current strict gate requires all of the following, without changing the suite:

- Verified Tesseract 5.5.2 engine launch/version and pinned English model.
- Unchanged image/TXT manifest hashes and preserved legacy reference identity.
- All five original images in each of two repetitions: 10/10 exact expected texts
  after the existing whitespace normalization, using PSM 7 and the unchanged
  A-Z/0-9 whitelist.
- Every OCR subprocess exits 0 within 30 seconds; zero timeouts.

The current 8/10 result is therefore a mandatory blocker under the owner's
existing instructions. Being inherited, synthetic, or matching legacy does not
make the two failures pass. Neither 114 passing core tests nor acceptance of the
reproducible engine/notice checks waives this OCR requirement.

Proceeding without 10/10 requires an explicit owner-approved, documented
exception naming the affected image/results, engine/model hashes, permitted
release/build scope, rationale and validation limits. Tests must remain unchanged
and continue to report the failures; a release-readiness record may separately
record the approved exception. No exception has been approved. An OCR exception
would not implicitly waive PPE failures, the native collection crash, shutdown
follow-up or separate publication/deployment/signing authorization.

The OCR gate is formally resolved only by passing the unchanged requirement or
by an explicit applicable owner exception. Final installer packaging remains
blocked. All original verified backups are preserved; no other VM or unrelated
system was changed.
