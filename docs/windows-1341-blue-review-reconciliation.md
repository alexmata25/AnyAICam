# Windows 1.3.4.1 independent-review reconciliation

Validated on AAC-WinVMS-1341-Test only, 2026-10-28. No Tesseract rebuild,
production application edit, OCR threshold change, service deployment or final
installer packaging was performed. The prior engine checkpoint is
7528d002f7be7547951303aeb60ba26906c3c9b0.

## OCR evidence and classification

Blue's execution-path finding is confirmed. validate-minimal-ocr.py invokes
tesseract.exe directly with PSM 7 and the existing A-Z/0-9 whitelist. It does
not call app.lpr, detector, glyph grouping, preprocessing fallback, voting,
entitlement gates or event persistence. make-ocr-test-images.py generates the
fixtures with OpenCV FONT_HERSHEY_DUPLEX, scale 3, thickness 6. The visual
ambiguity explanation is plausible; no production T-to-7 substitution follows
from it.

The unchanged strict run is 8/10: plate2.png expected 7XYZ482, actual TXYZ482
in both repetitions with both engines. All ten native processes exit 0, without
timeouts. Every prediction matches legacy. This is an inherited synthetic
component-test limitation, not evidence of a new minimal-engine regression or
a demonstrated wrong live-camera event. It remains a failing owner-defined gate.

The 114 passing product LPR tests are broader than raw CLI OCR, including
availability/entitlement gates, freshness, frame buffers, media reuse, cadence,
structured results and five real native glyph-recognition cases on different
fixtures. Much coverage is unit/mocked behavior. They do not certify a physical
camera/detector pipeline; the approved detector model is absent in this guest.
Direct read_plate_text/read_plate_glyphs diagnostics and earlier rejected
preprocessing experiments are preserved in the companion baseline report.

No further speculative preprocessing was run. Earlier 0.75-area/thinning
variants degraded other plate reads; a hypothetical leading T-to-7 substitution
would break eight previously correct genuine-T controls. None was applied.

The exact existing acceptance requirement remains 10/10, unchanged inputs,
PSM 7/whitelist, exact text after existing whitespace removal, verified engine
and model, native exit 0 and 30-second per-process timeout. Proceeding at 8/10
requires explicit owner approval identifying this fixture, hashes, allowed
scope and remaining limitations. No such exception is approved. Neither this
review nor component readiness authorizes final packaging.

## Exact warning and task ownership

Preserved original ocr-followup-lpr-integration.log warning:

```text
Task was destroyed but it is pending!
task: <Task pending name='Task-19' coro=<_finalize_ai_activity() running at C:\AAC-Test\Source\app\main.py:40856> wait_for=<Future pending cb=[Task.task_wakeup()]> cb=[_chain_future.<locals>._call_set_state() at asyncio\futures.py:396]>
```

This finalizer is scheduled with run_coroutine_threadsafe at main.py:40795;
the returned future is not retained. Blue's sites at 41349 and 41393 instead
create persist_event_recording and _backfill_ai_event_linked_recording tasks,
respectively. Those task handles are also discarded, but neither coroutine is
the one named in the observed warning. No change to those sites was justified
solely by this warning, and no application task fix was implemented.

Confirmed fixture defect: background_loop stopped and closed its loop without
cancelling and awaiting tasks. Fixture-only cleanup removes the warning:
targeted three-test rerun remains 1 passed, 2 failed, with zero pending-task
warnings, versus one warning before cleanup. Assertions were not weakened.

Four source-extracted shutdown probes pass. They reproduce abrupt-loop-close
failure and verify explicit cancellation/await and asyncio.run cleanup. They
also demonstrate a separate application ownership gap: the actual lifespan
shutdown body can log shutdown.complete while an untracked finalizer remains
pending. Installed Uvicorn 0.34.0 uses asyncio.run, which subsequently cancels
pending tasks. That is not evidence that real Windows service stop flushes
in-flight clips successfully. The smallest proposed product correction is a
registry for owned tasks/futures, bounded drain/cancel/await before shutdown
completion, with dedicated recording/shutdown tests. It must cover both loops
and exception retrieval; no broad speculative replacement is included here.

Two PPE tests still fail because no PPE event is emitted. They occurred in the
unchanged checkpoint and survive fixture cleanup; attribution to application
versus test setup remains open. One initial native PyTorch collection access
violation is preserved, not declared resolved by later passing imports.

## Additional execution and installer gates

Six existing test files ran inside the isolated guest with a 360-second bound:
job-limit, runtime-catalog, functional-harness, upgrade-harness, SAPI-greeting,
and event-only-edge checks. Result: 85 passed, 1 skipped in 160.08 seconds;
zero external connection attempts. The skip is the old 1.3.1-source upgrade
check requiring 7c66928, not an absent compiler or SDK. The passing catalog
tests include unsigned catalog construction; they do not certify signed release
coverage. Full logs, XML and bounded-run JSON are retained.

| Gate | Verified now | Remaining acceptance |
| --- | --- | --- |
| Tesseract under LocalSystem | Scheduled task identity SID S-1-5-18; exact verified engine; five native launches exit 0 and reproduce the existing 4/5 image result | Actual installed service/OCR integration and policy resolution of synthetic mismatch |
| SAPI greeting under LocalSystem | Actual synthesize_speech_sapi returns 254412 bytes, 22050 Hz, 16-bit mono, nonzero peak 31798 under SYSTEM | Real camera relay, visitor audibility and end-to-end greeting acceptance |
| CPU Job Object under WinSW | Separate pinned WinSW probe service runs as SYSTEM; unchanged helper reports cpu=80;memoryMB=0; Windows API confirms child in the same job | Installed VMS workload, sustained CPU measurement and service lifecycle |
| Upgrade from accepted signed 1.3.4 | Narrow approved-folder search found WinSW input but no previous AnyAiCam setup executable | Exact owner-accepted signed 1.3.4 setup, hash/provenance/signature verification, then isolated upgrade/data-retention acceptance |
| Installer at most 750 MB | No final candidate built; no explicit size gate found in installer sources reviewed | Owner size cap remains binding; measure actual setup bytes, conservatively <=750000000 until unit convention is documented; do not claim pass from source/runtime size |
| Event-only sandbox recording | Event-only edge source checks pass | Functional harness still provisions recording_mode=continuous at line 509, without event-only recording assertions; this is a harness coverage gap, not proof the application records continuously. Add explicit event-only scenario and verify clip/playback/idle behavior after packaging is authorized |
| Signed runtime catalog | Native staging includes pinned wheels, Python, FFmpeg, MediaMTX, OCR and WinSW | build.ps1:125 selects one staged file for catalog verification. Replace sample-only validation with complete native-entry verification, reconcile installed bytes and final OCR replacement; verify actual authorized signed catalog/artifact later |

The first WinSW probe configuration omitted a required description element;
WinSW partially registered the new probe service before rejecting it. The exact
failure is retained. Only that newly created, stopped, path-verified probe
service was removed; the corrected configuration then passed. Final SCM query
returns 1060 (removed). The completed SYSTEM scheduled task was also removed.
No existing VMS service or unrelated VM was stopped or modified.

## Accepted engine and dependency information for Blue

Tesseract 5.5.2 / source tag commit 6e1d56a847e697de07b38619356550e5cf4e8633,
MSVC 19.44.35229.0 v143, CMake 3.31.6-msvc6, Windows SDK target 10.0.22621.0.
SDK 26100 is also installed. Two clean same-path builds produced identical
40-file runtime/notice payloads. Non-shipped static-library differences are
archive timestamp headers only; refer to preserved reproducibility report.

Engine SHA256:
ba579ac5e6dbde80a6d4806356b2118fef008a3dfc0f062e0cdba77540e10d88

Model SHA256:
7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2

The 14-DLL runtime has no libiconv dependency, including normal and delay imports.
Every retained build EXE/DLL was audited (44 instances across retained builds
and launcher attempt). System/debug CRT distinctions are explicit; debug probe
executables and static archives are not distributable payload. Required notices,
IJG acknowledgment, model provenance and dependency identities passed the
technical notice audit. This does not grant release/signing authorization.
Leptonica's banner reports JPEG-turbo 3.1.2; pinned jpeg8.dll PE product metadata
is 3.2.0. Both facts remain recorded rather than hiding that discrepancy.

The sealed peer-review handoff contains this report, the baseline investigation,
exact raw warning/full log, build results, dependency audit, notice manifest,
strict results, regression logs and new SYSTEM/WinSW evidence. It is prepared
for the existing authorized transfer channel; no direct separate Blue session
delivery is claimed. Original bundles and previous evidence remain unchanged.

Next safe work is to review the bounded task-ownership correction and add
missing harness/all-entry catalog validation, while obtaining the exact signed
1.3.4 upgrade input. Final packaging stays blocked by the current OCR gate and
the outstanding acceptance requirements above.
