# Windows Gateway beta acceptance record

**Prepared, not executed. No beta installer has been built by this work.**
This supplements Blue's A1/A3/A4/A5/A6/A7 gates. Source tests and mocked
transport results do not establish installed-system acceptance. Each row below
requires PASS / FAIL / BLOCKED / NOT RUN, tester, timestamp and evidence path.
All installed-system rows currently remain **NOT RUN**.

## Record before requesting an execution window

| Field | Required evidence |
|---|---|
| Candidate source | Branch, exact committed SHA, clean packaged source, focused test output |
| Installer | Approved setup version/ReleaseVersion, file name, hash, byte count, pinned publisher signature |
| Baseline | Accepted signed 1.3.4 file/hash from Blue's A6 checklist, independently checked |
| Target | Explicitly approved isolated host/guest; no live Dell installation or customer footage |
| Test identity | Approved isolated signed activation/entitlement fixtures; no live cloud credentials |
| Resources | Timestamp, R430 current available/total RAM, estimated additional peak RAM, duration and basis |
| Existing VMs | Names, running states and allocations; explicitly list every VM that will remain running unchanged |
| Evidence | Test-only logs/JSON/screenshots, without passwords, private keys or raw credential files |

R430 RAM and VM inventory were not queried during preparation. No heavy window
is requested here. The earlier owner report of 4.66 GB free is not current
telemetry. Do not start/modify Hyper-V VMs or terminate processes to make room.

## Build readiness

- [ ] **B01 Inventory:** review `windows-gateway-beta-prerequisites.json` and rerun
  the presence-only auditor if inputs change. On AlexDellServer it found missing
  Inno Setup 7, 8 pinned vendor inputs and 108 wheels. Git is now resolved from
  the existing installation without PATH changes; -GitExe supports explicit selection.
  SDK makecat/signtool exist; their integrity and usability remain unverified.
  Azure signing DLL/metadata were absent at their configured/default locations;
  certificate signing variables were unset. Signing mode remains an explicit decision.
- [ ] **B02 Inputs:** approve setup/ReleaseVersion, verified build interpreter,
  public entitlement keyset and independently pinned hash, signing configuration
  and existing distribution gates from DEPENDENCIES.md. Presence is not validation.
- [ ] **B03 Source:** `python tools/run_windows_gateway_light.py` and
  `git diff --check` pass; packaged inputs match the exact candidate SHA.
- [ ] **B04 Approved build only:** after separate resource approval, use the
  existing build procedure. Preserve the 750,000,000-byte pre-signing size gate,
  dependency hashes, complete native catalog verification and pinned publisher.
  No compilation, signing, installation or download occurred in preparation.

## Installation and lifecycle

Fresh-install rows use separate approved clean test states. All editions must
share the existing VMS AppId, services and protected data paths.

| ID | Action | Pass criteria and evidence |
|---|---|---|
| I01 | Interactive Full VMS Local and Hybrid | Edition/mode choices and one-time online activation wording accurate; original behavior retained |
| I02 | Silent Full VMS, `/GatewayEdition=full_vms /ProductMode=local`, separately `hybrid` | Correct bootstrap; missing required mode refused; no invented upload override |
| I03 | Local Gateway interactive and `/GatewayEdition=local_gateway` | Local bootstrap and notice; no new upload flag enabled; existing permissions enforced |
| I04 | Cloud Gateway interactive and `/GatewayEdition=cloud_gateway` | Hybrid bootstrap and notice; event transport remains disabled until explicitly configured and entitled |
| I05 | Invalid edition or conflicting fresh edition/mode | Refusal before copied files/stopped services; before/after evidence shows original install intact |
| I06 | Signed 1.3.4 upgrade without edition/mode arguments | Existing Full VMS behavior, identity, configuration and footage preserved; no implicit edition migration |
| I07 | Repair each edition without edition/mode arguments | Edition/mode, upload/retention settings, identity/database/media preserved; normal repair restores product files |
| I08 | Request a different saved edition/mode during repair | Refused before service changes; original install still works |
| I09 | Legacy unreadable-config repair with no edition change | Blue's existing repair path retained; explicit unverified edition change refused; do not weaken ACLs to force a pass |
| I10 | Data-preserving uninstall/reinstall of each edition | Services/catalog/firewall follow A6 rules; retained identity/settings/media survive; saved edition reused |

Capture before/after using Blue's `Get-AnyAiCamInstallState.ps1`; compare with
`Compare-AnyAiCamInstallState.ps1 -Step Upgrade|Repair|Uninstall` and the correct
ExpectedVersion. Both snapshots use the candidate's tools. Edition, upload,
RETENTION_DAYS and local management/deletion switches are non-secret allowlisted
values. A changed saved value fails; unreadable data is BLOCKED. Identity hashes
prove preservation without exposing credentials. Keep comparison JSON and logs.

## Activation and camera capacity

| ID | Action | Pass criteria and evidence |
|---|---|---|
| L01 | Each edition before first activation | No usable camera capacity; UI explains one-time online activation; edition selection grants no licence |
| L02 | Approved isolated identity and correctly signed capacity | Existing trust/identity verifier accepts; camera numbering limited to accepted slots; UI agrees |
| L03 | Offline interval after acceptance | Existing accepted capacity/local operation remain usable under current offline policy; no new expiry or offline-first activation promise |
| L04 | Invalid/revoked signer, identity mismatch, zero capacity or excess camera | Existing licence enforcement fails closed; Gateway refuses unlicensed upload before credentials; media kept |
| L05 | Valid local capacity but cancelled/missing cloud entitlement | Local operation follows its local licence; fresh cloud authorization refuses upload; no cached-session fallback |

Never introduce unsigned grants or replace live trust anchors. Isolated fixture
provisioning itself needs approval. Source tests do not complete L02/L03.

## Discovery, local video, playback and notifications

Apply every row to Local Gateway, Cloud Gateway and Full VMS under their existing
permissions/licences. Use only an approved simulator or isolated test camera;
never scan a production network. Service actions require the approved window.

| ID | Action | Pass criteria and evidence |
|---|---|---|
| F01 | Discover ONVIF test camera and add within capacity | Existing discovery/credentials work; correct identity/number; no lost or duplicate camera |
| F02 | LAN live view and test reconnect | Existing supported local path resumes; distinguish LAN/P2P from blocked Gateway S3 live relay |
| F03 | Event recording, idle interval, second trigger | Completed playable event clip; correct camera/time; no unintended idle continuous recording |
| F04 | Existing continuous/scheduled local mode | Existing segments/schedules and entitled analytics remain operational; edition does not disable local recording |
| F05 | Playback/seek after offline interval and approved restart | Local segment/event plays with correct timeline/thumbnail; no cloud credential needed |
| F06 | New local event notification, read/unread and event navigation | Correct camera/tenant/permissions; persisted read state; no Gateway-induced duplicate |
| F07 | External notification providers | Mock only unless separately approved; no actual email/SMS/voice/push sent; real-provider delivery NOT RUN |
| F08 | Recording/retention settings and newest-file protection | Existing settings survive lifecycle; both legacy age sweep and managed free-space policy accounted for; no destructive test against customer media |
| F09 | Edition page and role permissions | Gateways have no bulk upload controls/polling; permission denial preserved; Full VMS retains original controls/navigation |

Preserve Blue's event-only, service-stop and post-restart playable-clip gates.
No VMS service has been started or stopped during preparation.

## Mock-backed upload and entitlement checks

These use mock capacity/maps/control plane/AWS clients and disposable media.
Never enable live uploads to complete a row. Record source SHA/test output;
mock PASS is code evidence, not real S3 or installed-system acceptance.

| ID | Input | Required result |
|---|---|---|
| U01 | Local Gateway, persisted Hybrid and all upload flags true | Zero event/recording/backup/live-relay video writes; pilot scope cannot bypass edition; local media retained |
| U02 | Cloud Gateway event opt-in unset/false | No credential request or video call; installation alone never enables upload |
| U03 | Opt-in, licensed motion camera, fresh valid credentials/region | Producer event MP4 and optional JPEG only under authorized event prefix; correct registration; remove outbox intent only after accepted/duplicate |
| U04 | Denied/cancelled entitlement, unreachable server, expired/malformed credentials | No AWS client/write; no cached-grant fallback; valid local retry intent retained |
| U05 | Bulk path, wrong event reference, unknown/non-finite/oversized duration, invalid image or event window | Refuse before credentials/upload; preserve clip; Full VMS keeps its established path |
| U06 | Uploaded bytes but deferred/failed registration | Keep retry intent; do not report success before accepted/duplicate; do not change retention to hide orphan media |
| U07 | Gateway bulk scan/queue/retry and low-level upload calls | 403/no queue state changes; low-level writes refuse; Full VMS behavior preserved |
| U08 | Full VMS cached session and explicit feature flags | Existing session reuse and flag rules unchanged; no Gateway-only gate applied |
| U09 | Event retention/allowance | Existing 7/14/30-day policy preserved; no invented six-hour event quota |

Real S3 delivery and real external provider integration remain NOT RUN and
require separate authorization. This checklist does not authorize AWS access.

## Full VMS and Blue component gates

- [ ] **V01:** Full VMS Local/Hybrid match accepted baseline for discovery,
  licensing, LAN view, recording/playback, notifications, roles and enabled analytics.
- [ ] **V02 / A1:** Tesseract/model work as LocalSystem with exact service
  environment; no App Control block; retain probe JSON/logs.
- [ ] **V03 / A3:** Windows SAPI works as LocalSystem with required audio
  properties; missing voice or blocked execution is not PASS.
- [ ] **V04 / A4:** WinSW service and children are in the Job Object with
  applied limits; fail-open availability does not prove applied-limit acceptance.
- [ ] **V05 / A5/A7:** Every native member catalog-covered or validly signed;
  no uncatalogued pip EXE wrappers; setup size gate satisfied.
- [ ] **V06 / A6:** Signed upgrade, repair and uninstall comparisons pass,
  preserving identity/data/configuration and expected services/catalog/firewall.
- [ ] **V07:** Record missing premium models/entitlements as prerequisites;
  never globally enable features or weaken checks to obtain a pass.

## Beta decision

- [ ] Attach source/candidate SHA, signatures, tests, lifecycle/probe JSON and
  per-edition video/UI evidence; record all exceptions and outstanding gates.
- [ ] Missing prerequisites, unrun installed-system tests and BLOCKED probes
  prevent claiming beta acceptance merely from focused test success.
- [ ] Obtain a separate owner decision before distribution. No push, merge,
  release, deployment, AWS/Stripe access or VM operation is implied here.
