# Windows Gateway editions checkpoint — 2026-11-02

Branch: `codex/windows-gateway-editions`. Foundation commit: `862ceb48ba77376f2b2c1b299c8e76e89f0df843`.
Owner-approved Git identity configured repository-locally: Alejandro Mata
<amata@anyaicam.com>. No global Git configuration changed. The 38 focused checks
were rerun successfully before committing the installer/UI implementation.
Installer/UI and transport-boundary implementation committed as `0d47a74`.
Licensing and fresh event authorization follow-up committed as `f996e72`.
Media/configuration/UI safeguards and initial lifecycle preparation committed
as `cca96a1afb13ec6f7409cff44c3058db2501cf8a`.
Normal apply_patch create/read/delete access verified within this checkout.

## Build prerequisite remediation (from f7d7533)

Full findings/owner actions: `docs/windows-gateway-prerequisite-remediation.md`.
Blue's sealed 579578b handoff records Inno Setup **7.1.0** and the earlier
`R430-win134-parity-d6b4244` package as the source of vendor inputs, wheels,
compiler and public keyset. That package is unavailable at its recorded location
here. Upstream source/verification pages were checked without obtaining binaries.
Upstream 7.1.1 is different; no replacement or installation was authorized. The
repo previously specified major 7 only, not an exact compiler patch.

The bounded cache audit reconciled all 108 wheel locks with all 108 Windows pins
and top-level manifests: zero missing requirements or version mismatches, including
the CPU variants. No exact-name candidate for the 108 wheels or eight vendor
inputs was found in the explicitly scanned caches. See
`windows-gateway-wheel-audit.json` and `.csv` for scope/limits and per-wheel pins.
No global disk-absence claim is made; inaccessible locations were not bypassed.
All eight vendor inputs remain required for the shared installer.

Git now resolves from its existing Program Files installation without PATH
changes. Optional -GitExe is forwarded to both commit lookup and the unchanged
packaged-source-integrity policy. Interpreter/pinned public-keyset validation
now precedes vendor hashing/native extraction/catalog signing. PowerShell parser,
helper and source-integrity tests pass without executing build.ps1. No pins,
source-integrity exclusions or signing gates were weakened. DEPENDENCIES.md's
stale 'Tesseract not bundled' row was removed; the pinned bundle is preserved.

python --version / py --list-paths show only 3.15.0, the test interpreter. The
explicit build-helper path needs verified 3.12.10; Blue's existing py -3.11 helper
fallback remains. Shipped Python stays embedded 3.12.10. No qualifying helper
was found at the documented paths/launcher. SDK tools exist; public keyset/pin,
versions and signing configuration still require owner confirmation. No private
key/credential/certificate-store contents were inspected or output.

Owner actions: restore the approved sealed inputs or authorize exact-pin recovery;
approve compiler/helper installation if no existing approved tools are supplied;
confirm keyset/environment/digest, release versions and signing mode securely.
Git/repository checks are resolved; vendor/wheel recovery is not. No build,
installation, large download, VM change, live AWS/Stripe, push/merge/deploy occurred.

## Beta preparation update

The presence-only audit is `tools/audit_windows_gateway_prerequisites.py`, with
the recorded result in `docs/windows-gateway-beta-prerequisites.json`. It reads
the build script and wheel lock directly; no tool execution, dependency download,
large-file hashing, certificate-store inspection or external call occurs.
The initial audit on AlexDellServer found Inno Setup 7 absent, Git absent from the build shell's
PATH, all 8 pinned vendor inputs absent and all 108 locked wheels absent.
makecat.exe and signtool.exe exist at the build's SDK paths, but their integrity
and usability are unverified. No build interpreter or approved keyset was supplied.
Azure signing DLL/metadata are absent at configured/default locations; certificate
signing variables are unset. No signing mode or credential was selected. The
audit always reports NOT_VALIDATED, even if all file-presence checks pass.

The precise per-edition acceptance record is
`docs/windows-gateway-beta-acceptance-checklist.md`: installation/repair/upgrade,
activation, discovery, local recording/playback, notifications, mock upload and
denial cases, Full VMS regression and all Blue component gates. Each installed
gate requires its own result/evidence; no beta installer was built or accepted.

Code review found Cloud Gateway computed its event time window after uploading.
Reversed timestamps, mixed timezones or datetime overflow could therefore fail
after bytes had been sent. Gateway time-window computation now runs before
credentials/AWS work and is reused for registration. Invalid input fails closed;
Full VMS timing/behavior remains unchanged. New mock-backed tests also verify
that catalog registration failure retains retry intent after a simulated upload;
all sleeps and transports are mocked. Local retention and event quotas are unchanged.

No further code-level blocker was identified by this focused review. This is
not installed-system acceptance or a complete code audit. Missing build inputs,
approved versions/keyset/signing decisions and the unexecuted lifecycle gates
remain beta blockers. R430 RAM and VM inventory have not been queried; no heavy
window is requested. Before any such request, report fresh R430 RAM, estimated
additional RAM/duration with basis, and the existing VMs that will remain running.

## Implemented foundation

The existing Windows VMS remains the application for all editions. Set
`ANYAICAM_GATEWAY_EDITION=local_gateway` or `cloud_gateway` in the existing
protected ProgramData/AnyAiCam/config/vms.env and restart through the normal
service lifecycle to select the fallback Local or Hybrid product mode.
Unset or `full_vms` preserves legacy behavior. Invalid selections raise a
configuration error rather than silently choosing cloud services.

Precedence remains administrative product mode, persisted cloud mode,
installer bootstrap, then edition default. Explicit per-feature flags retain
precedence within the edition's video transport boundary. Local recording,
playback, discovery, notifications, analytics and licensing remain shared.
Cloud Gateway is an edge VMS edition, not the cloud control-plane runtime role.

## Installer, UI and transport implementation

Setup now offers Full VMS, Local Gateway and Cloud Gateway on one edition
page. Silent installs accept `/GatewayEdition=full_vms|local_gateway|cloud_gateway`.
New Gateway installs derive Local/Hybrid mode respectively; Full VMS keeps the
existing Local/Hybrid choice and silent ProductMode requirement. The runtime
receives `-GatewayEdition`, validates/persists it in vms.env and preserves saved
selections when no selection is supplied on upgrades/repairs. Setup refuses
invalid selections and saved-edition switches before stopping services, and
the runtime helper independently enforces the saved-edition rules.

The shared page shell displays the selected Gateway edition and its video
upload limits. Full VMS has no new notice or changed navigation. No separate
application, entitlement system or replacement camera pipeline was created.

Local Gateway forbids event, recording, backup and S3 live-relay video uploads,
even when the saved mode is Hybrid or upload flags explicitly say true.
Cloud Gateway forbids recording/backup/live-relay uploads. Event clips and
their thumbnails require explicit ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED=true,
the existing per-camera motion entitlement, a valid session/credentials/region,
and the existing server registration policy. Edition selection alone never
enables this flag. No development or live environment was configured for upload.
Full VMS transport behavior is preserved. Transport flags and low-level upload
entry points enforce boundaries, including the recording pilot-scope path.
Local event capture/outbox writes remain separate from transport.

## Licensing and authorization follow-up

Gateway event uploads now also require the existing accepted camera-capacity
licence for the provisioned camera number. Missing, zero, revoked or unreadable
capacity fails closed. A cloud-control-plane runtime's capacity exemption is
not an edge licence. No new licence issuer, unsigned bypass or expiration rule
was introduced; accepted-capacity offline behavior stays in vms_capacity.py.
The Full VMS path retains its existing authorization behavior.

Cloud Gateway reauthorizes every clip through the existing credentials endpoint
instead of reusing a cached STS session. That endpoint already checks camera
ownership, effective cloud event entitlement and motion_event_policy before
issuing narrowly scoped event credentials. A denial/outage discards the old
cached session/client and leaves the clip pending locally. Malformed, expired
or timezone-naive fresh credentials are rejected. This adds one authorization
request per Gateway clip; Full VMS credential caching remains unchanged.

Gateway clip references must match the existing producer's
`/recordings/clips/motion/motion_{event_id}.mp4` output, preventing ordinary
recording segments from being passed directly to the event uploader. Existing
path containment checks still apply. Shared media registration also checks the
camera licence; it uploads no new video objects.

Gateway UI notices now report accepted camera-slot capacity or the need to
activate, plus whether Cloud Gateway event upload is explicitly configured.
They do not expose raw trust failure details or enable upload settings.
Installer preflight preserves Blue's legacy unreadable-config repair behavior
when no edition is requested; explicit edition changes still require a readable
saved choice, and invalid or switched saved editions remain refused.

## Media preflight, configuration and UI completion

Cloud Gateway now probes event duration before requesting credentials or creating
an AWS client. Unknown, non-finite, zero and oversized durations fail closed,
leaving local retry intent intact. This reuses the existing event policy's
individual clip sanity ceiling; it introduces no retention or daily quota change.
The successful preflight duration is reused for registration, avoiding a second
probe. Optional thumbnails require a JPEG extension and JPEG header, preventing
ordinary video files from being passed through the thumbnail argument. Full VMS
keeps its existing probe timing and optional thumbnail behavior.

The shared Cloud recording page now shows an edition-specific explanation for
Gateways, with a link to events and no bulk scan/retry controls or queue polling.
Its existing settings permission check remains in place. The Full VMS page keeps
the original controls/polling. Gateway scan, queue and retry endpoints return 403
without changing state; internal recording-backup enqueue is also blocked.

Fresh installer configuration now derives Gateway bootstrap mode in the runtime
helper as well as Inno Setup and refuses conflicting explicit edition/mode pairs
before service changes. Existing mode choices, legacy upgrades and saved upload
and retention switches are preserved. No upload flag is automatically enabled.

Blue's lifecycle snapshot allowlist now includes edition, video-upload switches,
RETENTION_DAYS and local management/deletion switches. Existing comparison logic
detects changed values. Synthetic comparisons prove an edition, upload flag or
retention change fails while an unchanged repair passes. Secret values remain
excluded. No installed-system snapshot was collected in this work.

## Blue acceptance inspection

`16fba66` integrates Blue's acceptance tooling; `579578b` updates the accepted
activation wording regression. The handoff names Blue's original `028846f`,
but that object is unavailable in this checkout; no new ancestry claim is
made for that original hash. The previous session's signed-baseline ancestry
finding is carried forward, not independently re-established here.
Source inspection confirms installer size/catalog gates, lifecycle fixtures,
SYSTEM probes and WinSW/Job Object checks. Eleven focused edition/source
acceptance checks passed in this session. R430 service, Smart App Control,
signed upgrade and actual lifecycle acceptance remain unverified, as recorded
in installer/windows/acceptance/ACCEPTANCE-TEST-STATUS.md.

## Retention inspection

The legacy main.py retention worker and the local_storage_manager are distinct.
The former uses RETENTION_DAYS (default seven), age-only deletion, newest-file
protection and local catalog cleanup. It does not require upload confirmation.
The latter is separately enabled by LOCAL_STORAGE management/auto-delete flags,
uses free-space thresholds (10% reserve, 20% warning) and customer policy age
limits, and excludes continuous cloud recordings. Its age default can also
come from an active entitlement. Cloud recording/event-media retention is a
third policy, in recording_retention_sweep.py, with confirmed object deletion
required before catalog removal. Edition selection changes none of these.
Do not present the newer storage policy as the sole local retention authority.
Any future Gateway buffering policy must reconcile both local deletion paths
and prove upload safety before changing deletion behavior.

## Event-clip retention inspection

Verified event_media_policy.py, not just historical prose: motion_event_policy
supports 7/14/30-day retention. Active v2 cloud_event_storage entitlements default
to 14 days, with a supported RDM retention override taking precedence. Missing,
cancelled or ineligible v2 entitlements are refused; existing legacy fallback
remains. allows_event_media checks policy and individual clip duration, without
consulting daily_seconds_used. The one-hour individual clip sanity ceiling is
unrelated to a daily allowance.

DEFAULT_DAILY_SECONDS still contains a historical six-hour value, but
cloud_policy_for_customer returns no default daily cap. A configured recording
allowance is read separately by recording_uploader; event clips are exempt.
No quota, retention period, billing policy or deletion behavior was changed.

## Validation and remaining work

66 focused unittest checks passed sequentially, covering edition defaults, explicit override
boundaries, Full VMS compatibility, Gateway notices, isolated actual transport
functions with mocks, real PowerShell edition and product-mode blocks, event
entitlement/retention policy with in-memory SQLite, and existing acceptance,
activation wording, Hybrid state-folder and premium-default checks. Added
coverage executes actual session and uploader functions in isolation, including
a fully mock-backed authorized event upload and catalog registration, capacity
failures, cached-session refusal, expired/malformed credentials, event-reference
validation and licence UI notices. Tests never send a network request; their
media file is a few disposable bytes. No real video was uploaded.
Added media preflight, Gateway versus Full VMS page behavior, backup API refusal,
fresh-install conflicts, saved configuration and synthetic lifecycle regressions.
Additional beta checks cover prerequisite parsing/presence-versus-integrity,
invalid time windows before transport and retained retry intent after failed
mock registration.
The fixed non-discovery runner is `python tools/run_windows_gateway_light.py`;
it imports no VMS application, starts no services and runs no actual AWS client.
git diff --check passed. Python 3.15 reports existing invalid-escape warnings
when parsing main.py. The pytest-based installer mode suite could not import
because pytest is unavailable; its page-order/argument/preflight assertions
were updated for the edition page, but that suite was not run. No full app test
suite or installer build/compilation was run. No VMS process, service, Hyper-V
operation or external service was started.

Next: installer compilation and Windows lifecycle/UI acceptance in an authorized
test environment. R430/SAC/signed-upgrade gates remain open. There is no new
Gateway buffering/deletion policy. Real service/deployment/merge and RAM-intensive
acceptance require separate approval. No push, deployment or merge occurred.

Build/lifecycle preparation is in `docs/windows-gateway-build-lifecycle-plan.md`.
It specifies source/dependency/keyset/signing checks, fresh edition and lifecycle
matrices and Blue's remaining A1/A3/A4/A5/A6/A7 evidence. The actual Inno Setup 7
path used by build.ps1 was checked and was unavailable; nothing was installed.
The R430's owner-reported 4.66 GB free / 31.91 GB is not a current measurement.
No heavy operation is proposed now. Any later proposal must state actual machine,
fresh free RAM, estimated additional RAM and duration and obtain approval before
execution. Do not compile, run a full suite, touch Hyper-V or terminate processes
under the present authorization.
