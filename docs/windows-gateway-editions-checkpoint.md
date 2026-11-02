# Windows Gateway editions checkpoint — 2026-11-02

Branch: `codex/windows-gateway-editions`. Resumed from `579578b`.
Normal apply_patch create/read/delete access verified within this checkout.

## Implemented foundation

The existing Windows VMS remains the application for all editions. Set
`ANYAICAM_GATEWAY_EDITION=local_gateway` or `cloud_gateway` in the existing
protected ProgramData/AnyAiCam/config/vms.env and restart through the normal
service lifecycle to select the fallback Local or Hybrid product mode.
Unset or `full_vms` preserves legacy behavior. Invalid selections raise a
configuration error rather than silently choosing cloud services.

Precedence remains administrative product mode, persisted cloud mode,
installer bootstrap, then edition default. Existing explicit per-feature
flags retain precedence. Editions are not entitlements or feature gates;
recording, playback, analytics, licensing and retention remain shared.
Cloud Gateway is an edge VMS edition, not the cloud control-plane runtime role.
No installer edition picker or edition-specific UI is implemented yet.

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

## Validation and remaining work

Lightweight unittest coverage exercises both edition defaults, Full VMS legacy
defaults, explicit flag overrides, configuration precedence and invalid input,
plus seven existing Windows acceptance source checks. No VMS process, service,
installer build, Hyper-V operation or external service was started. pytest is
not installed in the available Python; the full application suite was not run.
Next work: edition setup/UI integration and explicit buffering/retention design,
then isolated tests. Real service/deployment/merge and RAM-intensive acceptance
require separate approval. No deployment or merge is authorized by this checkpoint.
