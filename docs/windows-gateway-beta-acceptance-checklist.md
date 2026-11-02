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
| Disk | Current free space on staging/temp/evidence volumes and estimated peak additional space; separate approval before large dependency recovery or build |
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

## Remote cancellation, expiry and WireGuard acceptance (prepared, NOT RUN)

Execute only in a separately approved isolated installed environment. Do not
cancel a real subscription or edit Stripe. Use the existing authenticated
configuration channel with an approved test identity/control-plane fixture and
valid test signing chain; do not hand-edit grants and call that activation.
Apply R01-R08 to both Gateway editions. Local Gateway must still block every
AWS video transport even while separately entitled remote access is active.

Before each case record source/setup hashes, edition, licence validity/slots,
camera number, nonsecret effective mode/flags, configuration receive time,
remote session IDs (no capability URLs), LAN playback timestamps and existing
retention settings. Keep tokens, credentials, private keys and raw configuration
responses out of evidence. Use synthetic footage and mock upload endpoints.

| ID | Procedure | Required evidence/result |
|---|---|---|
| R01 | With a valid independent local licence, activate remote access via authenticated Hybrid configuration; open LAN viewing/playback and one brokered remote session | Accepted local capacity, working local recording, and separately authorized remote session; installer bootstrap alone cannot authorize remote access |
| R02 | Deliver explicit Local/empty/null mode from the isolated control plane to represent cancellation | Record receipt and persisted-state transition; new remote offers and media-token fetches refused; existing remote session closes; no new remote answer if cancellation arrives during offer creation |
| R03 | Trigger local motion and record/play/seek before and after R02; inspect local notifications/read state | Local licensed operation and existing footage remain; Basic Motion notifications work; retained local licence is not revoked merely by remote-plan loss. Record any existing mode-change service restart and recording gap rather than claiming zero interruption |
| R04 | Simulate remote-plan expiration at the test control plane, then poll configuration | Same refusal and local continuity criteria as R02/R03 once cancellation configuration arrives; invalid/expired local licence must still fail closed independently |
| R05 | Disconnect the isolated control plane before expiration/configuration delivery, then reconnect | Record last accepted mode and outage interval. Current implementation retains persisted grant offline: there is no independently timed remote-subscription deadline. Do not mark immediate offline expiry enforcement PASS; if beta requires it, owner-approved expiry/grace contract and authenticated expiry data are blockers |
| R06 | Fault-inject one local WHEP DELETE failure, then restore; repeat with signaling long poll held open | Session remains pending and is retried by independent guard; eventual DELETE success/404/410 removes it. Capture actual teardown latency and configured scan interval; sequential per-session DELETE timeout is 3 seconds, not a guaranteed global deadline. No LAN path/session teardown |
| R07 | Restore valid Hybrid configuration after cancellation, without changing installer bootstrap or upload flags | Dormant remote publisher resumes authorized signaling; no unnecessary application reinstall; Cloud event uploads stay off unless separately opted in/entitled |
| R08 | Repeat Full VMS Local/Hybrid baseline and authorized local analytics after remote cancellation | Full VMS legacy behavior unchanged; valid independent local analytic grants remain; unsigned/stale per-camera premium flags cannot produce new inference events |

### Existing authenticated expiry and grace rules

These are distinct existing mechanisms, not a new Gateway cancellation policy:

| Mechanism | Existing source behavior | Acceptance implication |
|---|---|---|
| Server renewal failure | `billing_status.py` uses 7 days from first unpaid renewal; repeated failures do not extend it. `sweep_grace` suspends overdue active entitlements. Initial incomplete purchases never receive this grace | Use isolated server fixtures; do not change live subscriptions. Inspect actual server entitlement state, including per-camera v2, rather than assuming an elapsed date changed a disconnected appliance |
| Product mode | `customer_entitlements.product_mode_for_customer` uses active per-camera v2 mode, then legacy active Local/Hybrid rows. Edge accepts mode via authenticated configuration and persists it without an expiry timestamp | Gateway explicit no-active-mode response revokes remote access on receipt. Persisted Hybrid alone has no offline deadline; a 7-day billing grace is not a 7-day remote cache TTL |
| Signed paid-feature snapshot | `appliance_entitlements` verifies signature, bound appliance/cloud/customer, trust and expiry on use; documented default TTL 24 hours, cloud-configurable | Paid feature expiry is separate from remote access; never reuse its TTL as an invented remote-access rule |
| Signed camera analytics | Signed identity-bound per-camera grant; maximum window 168 hours plus existing clock skew; expiry checked even during cache reuse | Expired premium analytic grants deny premium detections; no effect on Basic Motion/local recording by policy substitution |
| Accepted local camera capacity | Expiry enforced on acceptance; protected accepted capacity is reverified for trust/identity on reads with no subsequent clock-expiry enforcement | Preserve offline licensed recording after remote loss; signer revocation/trust failure remains enforceable. This does not grant remote access or paid analytics |
| Appliance login session | `appliance_identity` defaults: assertion 15 minutes, session 8 hours, offline verification grace 72 hours. Environment overrides bounded: assertion 1-60 minutes, session 1-168 hours, offline grace 1-720 hours | Login freshness is not subscription entitlement or local licence expiry. Record effective settings; do not turn login grace into a remote subscription deadline |

No new TTL, cancellation grace or auto-granted local licence is introduced.
Full VMS existing behavior and protected local capacity remain unchanged.

### WireGuard connection and proxy boundary

| ID | Procedure | Required evidence/result |
|---|---|---|
| W01 | Review test tunnel enrollment and actual route/source mapping, without exposing keys | Approved existing WireGuard installation/enrollment required separately; shared VMS setup is not proof that a Windows tunnel is installed/configured. Record tunnel addresses, gateway address, appliance port and intended route; never change to full-tunnel routing to fix connectivity |
| W02 | Compare configured `ANYAICAM_WIREGUARD_GATEWAY_ADDRESS` (default `10.70.0.1`) to actual ASGI `request.client.host` seen through the proxy | Exact effective peer match, including IPv4-mapped IPv6 normalization. NAT/source mismatch, missing tunnel or invalid address is BLOCKED; do not widen firewall rules or disable guards to obtain PASS |
| W03 | Inspect Uvicorn proxy trust and forwarding behavior on the installed candidate | Launcher uses `--proxy-headers`; inspect effective `FORWARDED_ALLOW_IPS` and proxy topology. A caller-controlled forwarded address must not disguise a tunnel peer. Wildcard trust/unverified reverse-proxy rewriting blocks acceptance pending an approved configuration/fix |
| W04 | With grant active fetch test HLS playlist/segment and event media through approved proxy; cancel, then request fresh playlist, segment and previously issued media token | Allowed only while authorized; cancelled tunnel requests/media tokens return 403. Existing browser-buffered bytes need not disappear. Same media stays available to permitted LAN viewers |
| W05 | Interrupt and restore only the approved isolated connectivity fixture | Local recording/playback continue under local licence; bounded connection failure is reported; authorized reconnect works. No production firewall, Hyper-V or live tunnel changes permitted |

### Cloud event entitlement and media protection

Use U01-U09 above plus R02/R04 with mock transports. Revoke camera A's cloud
event entitlement while camera B remains entitled: A must make no AWS write,
must not reuse old credentials, and must retain valid retry intent; B may upload
only its authorized producer event clip/optional JPEG and register it. Exercise
wrong camera/event prefix, expired credentials, cancellation between attempts,
registration failure and later authorized retry. Verify no bulk/live-relay
fallback and zero Local Gateway video writes under all flag combinations.
Test wrong-tenant media requests and cancelled/expired media tokens through
existing route authorization; never infer tenant security from peer matching
alone. Keep existing retention settings unchanged; source/mock evidence does
not prove real cloud delivery or installed lifecycle acceptance.

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
