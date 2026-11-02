# Windows Gateway editions checkpoint — 2026-11-02

Branch: `codex/windows-gateway-editions`. Foundation commit: `862ceb48ba77376f2b2c1b299c8e76e89f0df843`.
Owner-approved Git identity configured repository-locally: Alejandro Mata
<amata@anyaicam.com>. No global Git configuration changed. The 38 focused checks
were rerun successfully before committing the installer/UI implementation.
Normal apply_patch create/read/delete access verified within this checkout.

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

38 focused unittest checks passed, covering edition defaults, explicit override
boundaries, Full VMS compatibility, Gateway notices, isolated actual transport
functions with mocks, real PowerShell edition and product-mode blocks, event
entitlement/retention policy with in-memory SQLite, and existing acceptance,
activation wording, Hybrid state-folder and premium-default checks.
git diff --check passed. Python 3.15 reports existing invalid-escape warnings
when parsing main.py. The pytest-based installer mode suite could not import
because pytest is unavailable; its page-order/argument/preflight assertions
were updated for the edition page, but that suite was not run. No full app test
suite or installer build/compilation was run. No VMS process, service, Hyper-V
operation or external service was started.

Next: continue installer/UI and entitlement enforcement, then installer
compilation and Windows lifecycle/UI acceptance in an authorized
test environment. R430/SAC/signed-upgrade gates remain open. There is no new
Gateway buffering/deletion policy. Real service/deployment/merge and RAM-intensive
acceptance require separate approval. No push, deployment or merge occurred.
