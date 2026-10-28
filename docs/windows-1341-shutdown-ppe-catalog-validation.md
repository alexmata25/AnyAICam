# Windows 1.3.4.1 shutdown, import, PPE and catalog follow-up

This work follows the preserved 2e3258f03d4e9a913b938b32c68240f3846d2270
checkpoint. All application execution, service probes and tests ran only in
AAC-WinVMS-1341-Test. The original checkpoint, bundle, handoffs, OCR engine and
8/10 strict OCR result remain unchanged. No final installer was packaged,
signed or deployed. Earlier reports describe their historical checkpoints;
this report supersedes their open findings only where stated below.

## Activity finalizer ownership and service shutdown

Confirmed: activity finalizers submitted with run_coroutine_threadsafe were
not owned by the lifespan shutdown list. Outer asyncio.run cancellation could
abandon clip work after shutdown.complete. The production finalizer waits for
an activity's quiet/cap boundary, then performs local event persistence before
clip build/upload; cancelling it immediately is not successful media flushing.

ActivityFinalizers now retains cross-thread submissions and their actual Tasks,
retrieves exceptions, and seals admission on shutdown. The lifespan first
cancels/awaits detection workers while recording supervisors and buffers remain
available. It drains accepted activity finalizers and existing clip_tasks under
one 20-second grace budget, with a separate two-second bound for actual Task
cancellation cleanup. Quiet-period waiting ends on shutdown without changing
the recorded activity start/last-seen times or normal recognition behavior.
Finalizer task names include camera and owner id for timeout diagnosis.

The cancellation phase awaits actual Tasks, not just concurrent Futures: a
cancelled Future can report done before its coroutine finishes cleanup. Errors,
timeouts and cancellation-resistant work are explicitly reported. Such shutdowns
log shutdown.incomplete rather than shutdown.complete. No buffer or recording
deletion was added. Startup refuses to overwrite outstanding finalizer work.
The source WinSW stop allowance is 60 seconds rather than 30, accommodating the
new bounded phase and existing recorder teardown; no installed customer service
was reconfigured.

Source-extracted lifespan tests assert that event persistence and clip build
precede recorder termination and shutdown.complete, and that timeout outcomes
never claim completion. Owner tests cover cross-thread submission, normal drain,
awaited cancellation cleanup, cancellation resistance, sealed admission, clean
restart, closed-loop submission and existing motion-clip tasks.

A guest-only WinSW -> PowerShell -> Uvicorn service probe exercised the actual
source-extracted finalizer/lifespan as LocalSystem (SID S-1-5-18), with an
fsync'ed persistence-boundary marker. In the corrected controller run, stop
command returned in 1.344 seconds and SCM reached STOPPED 3.594 seconds later.
The journal orders recording.persisted and clip.built before frames.stopped and
shutdown.complete. The new probe service was removed (SCM query exit 1060).

The initial controller incorrectly treated WinSW stop command completion as
SCM STOPPED and checked results too early. Its initially false result is retained
alongside the later journal, successful post-stop recheck, and corrected rerun.
This was a probe timing defect, not evidence that the application skipped its
shutdown hook.

Limits remain explicit: this service probe used a persistence marker and a
controlled clip-build boundary, not an encoded camera recording or installed
VMS. Real-camera stops with active recording, native encoding, slow storage,
thread cancellation and hard service termination remain acceptance gates.
Cancelled or timed-out native/thread work cannot be called safely flushed merely
because Python cancellation returned. Late worker-thread submissions are refused
and logged after admission closes; real in-flight inference/worker-stop coverage
is still required before claiming complete clip preservation under load.

## Native import access violation

The original failure is corroborated by Windows Application Error/WER:

- Exception 0xc0000005, fault offset 0x000000000027e990.
- Faulting module C:\AAC-Test\Python312\python312.dll.
- Python file version 3.12.10150.1013 (Python 3.12.10 distribution).
- Report id fe800d1f-51af-4986-b915-dfe5b5e2f5da.
- Original Python traceback: importlib.get_data while importing torch.export,
  reached through main.py importing torch during pytest collection.

The original Report.wer and Application event details are copied as read-only
evidence. Its module list includes OpenCV and torch native libraries; this alone
does not establish a conflicting DLL, a bad wheel or the fault's root cause.

Before application changes were copied into the guest, three fresh collections
of the original 12-file list succeeded. Twelve fresh-process import probes
(four repetitions of cv2->torch/default PATH, cv2->torch/minimal-engine PATH,
torch->cv2/minimal-engine PATH) also succeeded. All 15 returned 0 with no timeout.
Exact commands, per-case logs and Python/torch/source hashes are preserved.
These are bounded non-reproduction results under the recorded environment,
not a claim that the historical crash is fixed or impossible to recur.

## PPE fixture corrections and test-state isolation

Both inherited PPE failures originated from positive-path fixtures that supplied
camera entitlement and a fake PPE detector but left ppe.PPE_ENABLED false,
Windows' correct default without installed model weights. The fake media fixture
now explicitly enables that test-only appliance switch. It also supplies the
completed local-event-recording boundary: clip-sharing tests do not have an
actual camera/FFmpeg buffer from which to wait for post-roll. Assertions still
require exactly one PPE event, one appropriate upload and correct media sharing;
duplicate suppression and negative cases remain intact. No application PPE,
availability or entitlement gate was changed.

A broader development run exposed another fixture lifetime issue: TestClient
shutdown correctly seals an owner, but later direct-call tests without startup
were reusing that sealed global. The existing activity-isolation fixture now
provides a fresh owner for each test. The clip-wiring background-loop fixture
also cancels/awaits pending tasks before closing its loop. The failed development
runs are preserved rather than relabelled as passing or inherited application
defects. The production admission guard remains enabled.

The expanded run then finished with 252 passed, 3 failed and 1 skipped. All
three additional failures were independently reproduced against a fresh archive
of the unchanged 2e3258f checkpoint (archive SHA256
3f354dcc5a74765bcc1ddd2dc05509881091ab1cbcb67992c1d9f719b35bd99e).
Two clip-wiring tests also omitted the completed local-recording boundary before
their fake clip builder; the error-path case now requests the existing fake
uploader fixture too. The third was a checker false positive: it recognized
platform_paths.native_path(...) but not an already imported native_path(...).
The checker now recognizes an explicitly imported platform_paths helper, with
new negative cases still rejecting raw and unimported Linux-path expressions.
No production path, ACL policy or allowlist was loosened.

An external attempt was blocked by the isolation audit in the expanded run.
The diagnostic harness records its destination (1.1.1.1) and preserves a stack
trace in a separate JSONL file. No connection was permitted; this is not a
zero-attempt run and must not be reported as one. The original isolation
harness is unchanged; the diagnostic derivative retains the same blocking
exception and adds evidence only.

The isolated Windows-path rerun identifies that attempt as pinned Ultralytics'
import-time ONLINE = is_online() in utils/__init__.py:806, calling
socket.create_connection((dns, 80), timeout=2.0) at line 630. The audit stopped
it at getaddrinfo before a connection. This is a reachability probe, not evidence
of a telemetry payload or a Stripe/production API call. The trace and destination
are retained; third-party reachability behavior is not silently declared fixed.

## Final recorded regression results

| Coverage | Latest results |
| --- | --- |
| Activity owner | 6 passed |
| Source-extracted finalizer/lifespan shutdown | 5 passed |
| PPE event/media sharing | 3 passed |
| AI clip wiring | 16 passed |
| Continuous activities | 14 passed |
| Original nine-file LPR core selection | 114 passed |
| Event-only edge behavior | 52 passed |
| Runtime catalog | 12 passed |
| Installer-size guard | 5 passed |
| Functional sandbox tooling | 7 passed |
| Windows analytics paths/availability | 14 passed, 1 Linux-layout skip |

The latest per-node inventory is 258 passed, 0 failed and 1 skipped over 259
unique case-sensitive node IDs, across recorded batches, NOT a single 259-case
run. REGRESSION-RESULTS.json and LATEST-NODE-RESULTS.csv identify each batch and
outcome. Other passing nodes include camera retirement and notification
idempotency. Intermediate failures remain in the history: the expanded run was
252 passed/3 failed/1 skipped; the first corrected focused run was 119 passed/
1 failed/1 skipped because its error-path test had not yet requested the fake
uploader fixture. The final clip file passed 16/16 after that fixture correction;
the isolated Windows-path file passed 14/1 skip. No assertions were weakened.

No pending-task destruction warning was emitted in these recorded development
or verification runs. The final clip rerun reported zero external attempts;
the Windows-path rerun reported the one blocked reachability probe described
above. Native collection did not crash in either expanded run.

## Runtime catalog and installer-size gates

The signed build now runs Test-RuntimeCatalogFiles on every staged runtime file,
with a catalogued/verified count match before Inno compilation. A failure at any
file stops the build. Regression coverage verifies all three staged entries,
rejection of a second-file failure, and refusal of an empty stage. Mock verifier
tests prove orchestration; unsigned native catalog tests prove construction.
Neither substitutes for validating a real authorized signed release catalog.

Assert-InstallerSize checks the actual Inno output and rejects missing, empty or
oversize setup files. The limit is conservatively 750000000 bytes (decimal
750 MB); it cannot be raised through the helper parameter. Tests execute the
PowerShell helper at below/equal/above and empty boundaries, using a smaller test
limit without creating a large dummy installer. The sandbox also checks setup
size. Actual installer size remains NOT MEASURED because no candidate installer
exists and final packaging is prohibited.

## Event-only sandbox status

The existing functional harness requested recording_mode=continuous, which the
current event-only provisioning contract rejects. It now requests event mode
and explicitly verifies the edge's stored local_recording_mode plus FFmpeg
segment writers targeting the private _event_buffer rather than customer-facing
continuous recording paths. Source checks enforce these new harness gates.

The product event-only edge regression suite remains part of the final run.
An installed, clean-machine functional sandbox run, deliberate long idle phase,
real event creation, clip playback and retention measurements have NOT been
performed. Existing moving-pattern/recording checks do not prove idle behavior.
Those gates require a verified authorized test installer, which is not available
while final packaging is closed. No Windows Sandbox feature was enabled on the
R430 host and no existing VM was restarted.

## Release status and evidence

The final regression summary, exact tested source hashes, commit id and bundle
hash are recorded in the sealed Shutdown-PPE-Catalog-20261028 evidence checkpoint.
Historical failed runs, original WER metadata, both service probes and final
logs/XML are retained. The original 2e3258f recoverable bundle is unchanged.

OCR remains 8/10 with unchanged engine, model, images, expected texts and
thresholds. No policy exception has been approved. Real signed-catalog coverage,
actual installer size, accepted signed 1.3.4 upgrade input and installed
event-only/active-camera shutdown acceptance remain open. No release readiness,
publication, production signing or deployment approval is implied.
