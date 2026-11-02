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

## Resumed application and installer sprint (2026-11-02)

Create/write/delete probes passed in the main checkout and both existing
application/installer worktrees. All three began clean; their existing branches
and commits were preserved. Previous agent processes were unavailable, so the
application and installer roles were recreated on the same worktrees.

Application `fc8976d` integrated as `5397e9b`: product-mode persistence now
publishes a flushed complete temporary file using atomic replacement. Failed
flush/replacement preserves the accepted state. Three mock-backed regressions
cover visibility and failure recovery, including local remote-access denial.

Installer `fce2d8c` integrated as `f75d0ba`: fresh silent Full VMS setup missing
a required product mode is rejected during preflight, before installation
mutation. Explicit Gateway editions still derive their existing modes; existing
installation handling is preserved. One source regression covers the guard.

Coordinator ran tests sequentially: baseline 115 passed in 22.672s; application
26 passed in 2.123s; installer 10 passed in 8.270s; integrated fixed runner
**119 passed in 21.773s**. Diff check passed. Historical Python invalid-escape
warnings remain; the final run filtered those warnings. Installer validation is
source-level, with no compiler or installed-system acceptance claim.

No production, billing or retention changes occurred. No build, install,
download, signing, VM or service operation ran. Sealed dependency/keyset access,
toolchain approvals and isolated Windows lifecycle/UI acceptance remain pending.

## Approved two-agent source sprint (from f72a112)

Owner approved two agents, separate worktrees and combined 2 GiB additional
RAM / 1 GiB disk planning allowance. Read-only telemetry before launch:
ALEXDELLSERVER 2026-11-02 15:54:24 CST, 5.59 GiB available RAM, C: 5082.90 GiB
free. Application and installer worktrees under
`C:\AAC-Dev-Reports\Gateway-Worktrees` each started at f72a112. Application
agent owned app/tests; installer agent owned installer/prerequisite checks and
acceptance docs; coordinator owned integration, checkpoint/master and all test
execution. Tests ran sequentially through one coordinator slot.

Application commit `ea0a0bc` was applied locally as
`f1d8e2d5ecf4ca95517984b18c097a0a210b740c`:

- Non-object persisted mode JSON no longer crashes startup/configuration
  recovery; it cannot serve as a remote-service grant.
- Malformed WHEP session locations do not prevent cleanup of valid sessions.
- The independent cancellation guard retries unexpected cleanup failures with
  capability-safe logging while propagating task cancellation normally.
- Four actual-function/mock regressions cover those failure/recovery paths.

Installer commit `21e8ea8` was applied locally as
`f3fd265b48f4a978835daea9360c87e9c31b9824`:

- Build-only metadata guard requires numeric Inno version 7.1.0.0 before
  staging or compiler execution. Wrong/missing/incomplete metadata fails closed.
  This does not establish authenticity or replace sealed provenance checks.
- Presence inventory explicitly reports required version and unverified version
  status, preventing file presence from being mistaken for validated toolchain.
- Two regressions plus strengthened inventory assertions cover the guard;
  prerequisite/dependency/build/acceptance documents reflect it and disk gates.

Coordinator targeted validation: 23 application checks passed in 1.726s and
8 installer/prerequisite checks in 2.572s. Integrated fixed sequential runner:
**115 passed in 22.058s**, preserving all 109 baseline checks. `git diff --check`
passed. No new runtime imports, compiler execution, package installs, inference,
VMS/VM startup, real video/cloud calls or signing operations occurred.

At 15:59:04 CST, free RAM was 5.43 GiB and disk 5082.81 GiB; two worktrees
occupied 85.17 MiB. These are resource snapshots, not a measured peak-memory
profile. Worktree branches remain available as clean local evidence. All commits
use repository-approved Alejandro Mata <amata@anyaicam.com> identity.

No wholesale Blue merge occurred. Shared VMS architecture, edition transport
boundaries, local licence/recording/playback/Basic Motion and retention policy
are preserved. Latest-source certification still needs an authoritative current
branch/ref or newer sealed handoff; the available 579578b/Blue bundles were
already compared. The dependency payload/keyset remain unlocated in searched
paths; Python installer and SDK are present but tool installation, versions,
signing authorization and isolated installed acceptance still need their
separate approvals. This sprint creates a validated source candidate, not a
compiled/signed/installed 1.3.4.1 beta.

## 1.3.4.1 alignment comparison (from 2f501a6)

Owner approves verified 1.3.4 as minimum, with 1.3.4.1 target. Gateway's direct
base remains `579578b604cdf1b641905193cf8891de1e1a0a1f`. Available origin is a
sealed local bundle, not a current upstream service. Its branch tip is still
579578b. Read-only connected GitHub search found no accessible AnyAiCam-VMS
repository; this does not prove no newer private source exists. Current
authoritative repository/branch or newer sealed handoff has been requested.
No claim of global latest-source alignment is possible until it is supplied.

Compared both existing Windows bundles in an isolated bare review repository
under AAC-Dev-Reports. Blue's parallel `028846f` (2026-10-28 10:26 -0500) is
earlier than integration `16fba66` (10:52) and tip 579578b (11:07). All twelve
acceptance helper files plus installer-size-policy.ps1 match byte-for-byte
between Blue's original and 579578b. The integration additionally verifies all
native catalog members rather than a sample, preserves the pre-sign size gate,
adds generated Python launcher cleanup and reconciles activation wording.
No unintegrated newer authoritative Windows commit was found in these sources.
Linux/Red bundles found alongside these transfers are not treated as approved
Windows 1.3.4.1 successors merely because their dates are later.

All 109 lightweight checks passed again sequentially in 21.553s;
`git diff --check` passed. This milestone changes documentation only; no source
runtime, dependency pin, signing key or architecture was modified.

An isolated `git merge-tree --write-tree --name-only` simulation against the
unchanged Gateway tip found conflicts if Blue's original parallel history were
merged wholesale: `installer/tests/test_windows_acceptance_tools_1341.py`
(add/add), `installer/windows/acceptance/Get-AnyAiCamInstallState.ps1` (add/add),
and `installer/windows/build.ps1` (content). No actual merge/index/worktree or
Gateway ref was changed. That merge is unnecessary for the available sources.

Focused integration plan if a newer authoritative source is supplied:

1. Confirm owner-approved source tip, provenance, ancestry to e3d7eda/d6b4244
   and relationship to 579578b; inspect it in an isolated review checkout.
2. Compare unique commits and actual patch/blob contents so already reconciled
   Blue work is not applied twice. Simulate the proposed integration first.
3. Review overlaps in build.ps1 (early keyset/interpreter checks, Git discovery,
   all-member catalog/size/publisher checks), edition wizard/runtime configuration,
   service state paths, lifecycle snapshot allowlist and acceptance tests.
4. Review runtime overlaps in product_mode/edge sync, recording/event/P2P upload
   boundaries, independent local capacity, per-camera analytics and main.py.
   Preserve Local zero-video-upload, Cloud event-only fresh authorization,
   cancellation/session retries, Basic Motion/local playback and retention.
5. Present exact commits/conflicts/resolutions for owner approval before merging.
   Preserve the current Gateway tip, apply only approved integration in isolation,
   then rerun the complete 109-check lightweight baseline and relevant new tests.
   Installed acceptance and installer compilation remain separate approvals.

Installer architecture and prerequisite findings are unchanged: one shared VMS
setup for all editions. Build still needs sealed vendor/wheel payload, verified
Inno 7.1.0 and installed approved helper interpreter, confirmed public keyset/
digest/environment, approved setup/release versions, current signing workflow
and an approved resource window. Original sealed package and current-source
handoff are separate recovery needs; neither a parallel commit nor a successful
mock suite substitutes for binary dependencies or installed acceptance.

## Prerequisite recovery and expiry evidence (from 056bbd5)

Extended the existing remediation with the requested component/version/location/
verification/action table. Read only existing transfer handoffs/manifests,
candidate filenames in transfer/Downloads, build documentation and previous
bounded cache evidence; no remote original-machine access was assumed. Rehashed
the small handoff and 26.96 MB Python installer: both match their manifests.
Python 3.12.10 installer, baseline 1.3.4 setup (size confirmed), and SDK
10.0.26100.0 tools exist outside the workspace. Package payload, approved public
keyset and Inno 7.1.0 remain unlocated in searched paths, not proven absent from
every machine. Historical keyset digest is known; actual key IDs/current intended
environment still require the approved public file and owner confirmation.
The shared VMS setup remains the only installer architecture for all editions.

Documented distinct server 7-day renewal grace, signed feature snapshot TTL,
168-hour analytic grant maximum, accepted local-capacity offline protection,
login TTL/offline grace and untimed persisted remote mode in the beta checklist.
No signed expiry value exists in persisted remote mode; do not transplant login,
analytics or billing TTLs into a new cancellation policy. No billing, trust,
local-capacity, retention or runtime behavior changed in this milestone.

Three isolated existing-function regressions added: login offline-grace exact
boundary, renewal-grace calculation and active-only per-camera product mode.
All 109 sequential checks passed in 22.121s, preserving the original 106;
`git diff --check` passed. Existing installer acceptance plan now requires a
named approved disposable target, fresh resource measurements/estimates and an
owner-managed rollback image before a test-window request. No target assigned,
resource-heavy operation proposed or infrastructure changed.

## Installer and acceptance preparation (from c52a07b)

Confirmed the existing shared `AnyAiCam-VMS.iss` architecture, AppId,
edition-selection helper and WinSW launch path; no separate Gateway installer
or application is introduced. A targeted presence check still finds no Inno
Setup compiler at the build-script path and no sealed
`R430-win134-parity-d6b4244` directory. The already hash-verified Python 3.12.10
installer remains present in the supplement; it was not installed or re-audited.
Existing remediation remains authoritative for all eight pinned vendor inputs,
108 locked wheels, public keyset/digest, version and current signing approval.
Compilation is NOT READY until those inputs/tool installations and the resource
window are approved/resolved. No cache rescan or dependency download occurred.

Extended the existing beta checklist with R01-R08 and W01-W05: authenticated
cancellation/expiration fixtures, local video/notification continuity, independent
local licensing, WHEP retry/long-poll teardown, reactivation, WireGuard peer and
proxy-trust evidence, per-camera event upload and media-access protection.
It explicitly records that offline remote grants have no new expiry deadline
and existing mode-change restarts may interrupt recording. If immediate offline
expiry is required, an approved expiry/grace contract and authenticated expiry
data are still needed. No live billing cancellation is part of acceptance.

Fixed IPv4-mapped IPv6 peer comparison in the Gateway tunnel cancellation gate;
native IPv6 and equivalent configured addresses are covered. Full VMS and LAN
behavior remain intact. All 106 sequential lightweight checks passed in 20.291s
(the original 104 plus two regressions); `git diff --check` passed. No model,
service, upload, installer compilation or VM was started. Build approval now
explicitly requires current disk availability/estimated staging usage as well
as machine/RAM/duration/unchanged running VMs. No heavy window is requested now.

## Remote cancellation and detection boundaries (from 8bbcca2)

Both approved roots passed temporary-file create/read/delete probes:
`C:\AnyAiCam-Gateway-Dev` and `C:\AAC-Dev-Reports`. The external master checklist
was reconciled first: its standalone Linux branch/CLI and commercial-policy
claims are preserved as historical context, not Windows requirements.

Gateway remote access now requires persisted Hybrid configuration from the
existing authenticated configuration channel. Bootstrap, administrative flags
and a retained local licence cannot substitute for that grant. Explicit empty,
null or unknown configuration mode revokes remote access; an omitted key retains
compatibility. Full VMS keeps its existing mode and override behavior.
The publisher rechecks authorization before offers, after an in-flight offer,
and independently of long polling. Revocation retries closure of cloud-brokered
WHEP sessions using their existing local capabilities, preserving LAN paths.
A dormant Gateway publisher can activate after a later configuration grant.
Media-fetch tokens cannot bypass cancellation; requests from the configured
WireGuard gateway address are refused before static HLS/media delivery while
remote access is inactive. Installed acceptance must verify the actual proxy
source address and trusted-proxy configuration against this configured address.

Cancellation takes effect after configuration is received; this does not add
an offline subscription-expiry deadline. Existing configuration-change restart
behavior is preserved. These changes do not promise uninterrupted recording
during that existing restart, nor alter local licensing or retention.

Gateway premium detection rechecks signed per-camera analytic grants at use
and before persisting inference results. Cached camera flags and a combined
role cannot bypass the gate. Basic Motion/local recording fields and valid
independent local analytic grants survive remote-plan cancellation. Full VMS
retains its existing behavior. No billing, quota or deletion policy changed.

Validation: the fixed sequential runner now contains the original 84 checks
plus 20 remote/detection regressions (104 total). All passed with mocks and
small temporary files; no application, service, inference model or AWS client
was started. Installed cancellation timing, actual WHEP teardown, WireGuard
proxy identity and Blue's lifecycle acceptance remain hardware acceptance gates.

## Installer recovery handoff (from d95780e)

Existing remediation document now opens with the exact sealed recovery request
and shared-installer edition arguments. No prerequisite/cache audit was repeated.
The current host is ALEXDELLSERVER; original-machine records identify
AlexDellLaptop. No other machine connection was assumed or attempted.
The recorded parity package `R430-win134-parity-d6b4244` remains unavailable at
the checked local folder/zip/7z paths; request it unchanged with SHA256SUMS.txt
digest `e9d6d867a764e48c07f6090d196ef0660dfac6c30f1dd91e6e49267e32bd43a0`.

Important recovery correction: the approved Python 3.12.10 installer is present
in the existing Blue supplement. Its current SHA-256 matches the sealed record:
`67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb`.
Supplement manifest/authenticity record and 579578b HANDOFF.md also match their
recorded hashes. This supplies the installer file, not an installed helper;
installation still needs approval and current signature verification remains
a pre-installation prerequisite. No download
is needed for this file. The original signed VMS baseline setup is present,
but is not a source-cache substitute and was not extracted/installed.

Blue's handoff also reports its prior signing certificate expired 2026-10-27;
new signing needs current authorization, without exposing credentials. Build
still uses one shared package with edition chosen during setup. No runtime,
Blue source/handoff, retention or feature code was changed in this preparation.
Fourteen targeted installer source/configuration checks passed in 7.118 seconds;
git diff --check passed. The 84-check runtime suite was not repeated for these
documentation-only updates.

## Active runtime integration (from 476ca2a)

Committed as `0229300` after 80 focused checks passed; tree was clean before
the next persistence milestone.

Gateway local camera operation now uses the independent vms_license_capacity
component of the same verified, identity-bound accepted signed grant. A zero
remote-plan count no longer disables local cameras when a valid local licence
remains. No new grant is invented: missing/invalid/revoked trust still fails
closed, malformed licence values are refused, migrated unsigned records do not
gain this capability and old signed grants without a separate licence component
retain their old count. Full VMS keeps its existing plan-limited capacity.
Cloud event writes still require per-camera eligibility and fresh server
authorization; local licence capacity never grants cloud service entitlement.

Basic Motion already wrote legacy JSON in-app alerts, but the customer inbox
reads the notifications table, so Gateway customer accounts saw an empty inbox.
gateway_notifications.py bridges the existing alert into that table using
notification_engine's recipient/camera permissions, per-user dedupe and read
state. The explicit local_only fanout mode never queues push/email/SMS; the
existing legacy external dispatch remains untouched. Full VMS does not enter
the Gateway bridge, and fanout's default external behavior is preserved.

Gateway alert cards now link to local camera/timestamp Playback without requiring
an uploaded event-media row. Gateway recording URLs prefer an existing local MKV
before cloud URL signing; existing authenticated /local/remux routes and history
permissions remain in place. Full VMS retains cloud-first behavior. Tests exercise
the actual local registry/credential lookup and camera licence gate without
contacting a camera, starting FFmpeg, importing the VMS or querying cloud services.

Windows service startup validates Gateway edition and edge role before directory
creation, stale-process cleanup or Job Object changes. Invalid Gateway roles
fail early; Full VMS role behavior and Blue's remaining launcher fixes are kept.
No installed launcher or service was executed. The helper and existing isolated
launcher fixture (with the application invocation replaced) were tested.
Local recording strategy, retention, premium analytics grants and customer
authentication/offline-grace security policies are unchanged.

## Event-media retry persistence

Concurrent clip completion/retry threads previously performed unlocked
load/modify/save operations with the same .tmp filename. A write could lose
another event or fail replacing a temporary file another writer had renamed.
event_media_outbox now serializes in-process mutations with a reentrant lock
and writes a unique temporary file in the protected outbox directory, flushes
it, and atomically replaces the destination. Failed replacement leaves the
previous file intact and cleans up its own temporary file. This is an
in-process thread-safety fix, not a cross-process locking protocol.

Four focused persistence tests use tiny disposable JSON files, including one
bounded two-writer unit test (no stress workload or parallel test suite).
They verify both events survive, failed-write preservation, saved retry metadata,
denial/backoff, disabled-transport preservation and successful retry after
authorization recovers. No media file, retention setting, quota or grant is
deleted or changed by this fix. Full VMS uses the same corrected persistence
mechanism without changing its upload eligibility/defaults.

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

84 focused unittest checks passed sequentially, covering edition defaults, explicit override
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
