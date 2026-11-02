# Windows Gateway build and lifecycle acceptance procedure

Prepared only. Compilation, full regression and VM acceptance are **not authorized**
and have not been executed. Source remains the shared Windows VMS 1.3.4 lineage
on `codex/windows-gateway-editions`; all editions use the same application,
AppId, services and protected data paths.

The precise beta evidence matrix is in
`windows-gateway-beta-acceptance-checklist.md`. The read-only presence inventory
is `windows-gateway-beta-prerequisites.json`; refresh it with
`python tools/audit_windows_gateway_prerequisites.py --output docs/windows-gateway-beta-prerequisites.json`.
The auditor executes no build tools, hashes no large inputs and contacts no
services. NOT_VALIDATED is not permission or readiness to build.
The follow-up `windows-gateway-prerequisite-remediation.md` records Blue's exact
7.1.0 compiler provenance, all eight vendor roles, 108-wheel/cache reconciliation,
interpreter roles and specific owner actions. Git discovery is resolved locally.

## Permitted lightweight verification

From the checkout, run `python tools/run_windows_gateway_light.py`. It uses an
explicit unittest list sequentially, without discovery or pytest. It never
starts the VMS, a simulator, a VM, a scheduled task or an AWS client. PowerShell
tests execute configuration helpers and compare tiny synthetic JSON snapshots.
Actual uploader functions are exercised with mock transports and disposable
bytes. An installed VMS is not an input to these tests.

Check `git diff --check` and repository-local identity before a local commit.
The approved author is Alejandro Mata <amata@anyaicam.com>. Do not change
global Git config. Do not run the full pytest suite to compensate for a missing
dependency. Python 3.15 parsing reports inherited invalid-escape warnings in
main.py; that is not evidence of bundled Python 3.12 runtime acceptance.

## Approval gate for later resource-intensive work

The owner's last reported R430 availability was **4.66 GB free / 31.91 GB**;
that is a dated report, not current telemetry and not permission to build.
Before proposing a build, full suite or VM acceptance session, report:

| Required field | What to record before requesting approval |
|---|---|
| Machine | Actual host name and whether the operation is on the host or an existing disposable guest |
| Current RAM | Timestamp, current free and total RAM; do not substitute the owner's earlier report |
| Estimated requirement | Peak additional RAM for the specific command/guest, including toolchain and guest allocation; state the basis and uncertainty |
| Expected duration | Estimate for that operation, with its basis; do not reuse lightweight test timing as build timing |
| Scope | Exact build/test operation, evidence destination and test-only target |
| Existing VMs | Running VM names/states/allocations and the explicit list that will remain running unchanged |

Obtain explicit approval before executing that operation. If estimates are
unknown, resolve them with the owner or existing build evidence first. No
process termination, memory reclamation, VM change, installation, restart,
AWS/Stripe modification, push, merge or deployment is authorized here.

## Build preparation checklist (do not execute compilation now)

1. Select an approved monotonically increasing Windows setup version and the
   approved customer-facing ReleaseVersion. Neither is implicitly changed by
   edition selection. Record the exact committed source SHA and clean packaged
   source state using the existing source-integrity checks.
2. Confirm Inno Setup **7** at `%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe`,
   the exact path used by build.ps1. This dependency was not present in this
   session. No installation or download was attempted.
3. Supply the approved public entitlement keyset and its independently pinned
   lowercase SHA-256. Never include private signing keys or weaken the trust
   anchor. Confirm the verified build interpreter accepted by build.ps1
   (`-PythonExe` requires Python 3.12.10), vendored dependency hashes and native
   catalog inputs. The session's Python 3.15 test interpreter is not that build
   interpreter. Resolve missing dependencies without starting the build.
4. Review `installer/windows/build.ps1`, DEPENDENCIES.md and the existing signing
   procedure. The eventual build invocation needs `-Version`, `-ReleaseVersion`,
   `-EntitlementKeyset`, `-EntitlementKeysetSha256` and an approved interpreter.
   Signing is separately controlled by the existing `-Sign` or `-AzureSign`
   workflow. Do not invent a signing identity or publish an unsigned candidate.
5. After a separately approved build: retain source SHA, setup SHA-256, byte
   count, signing/publisher verification and complete catalog membership
   evidence. Preserve Blue's 750,000,000-byte pre-signing size gate and full
   native-member verification; do not reduce coverage to a sample.

## Installer configuration matrix for later acceptance

These are candidate setup arguments, not instructions to run setup now.

| Fresh installation | Arguments | Required result |
|---|---|---|
| Full VMS Local | `/GatewayEdition=full_vms /ProductMode=local` | Existing Local bootstrap and Full VMS behavior |
| Full VMS Hybrid | `/GatewayEdition=full_vms /ProductMode=hybrid` | Existing Hybrid bootstrap and Full VMS behavior |
| Local Gateway | `/GatewayEdition=local_gateway` | Local bootstrap; all AWS video transports blocked even if switches say true |
| Cloud Gateway | `/GatewayEdition=cloud_gateway` | Hybrid bootstrap; event-upload flag remains unset/off; no video transfer merely from installation |
| Invalid or conflicting choice | Invalid edition, or Local Gateway with `/ProductMode=hybrid` (and converse) | Refuse before service changes or file installation |

For unattended execution, add the existing `/VERYSILENT /SUPPRESSMSGBOXES
/NORESTART` and a test-only log path. A fresh Full VMS silent install still needs
ProductMode. Do not add any event-upload enable switch to the acceptance machine.
Repairs/upgrades without edition or mode arguments must retain existing edition,
mode, explicit upload settings, identity, local footage, licensing and retention.
Do not reinterpret an explicit saved mode as a request to switch an edition.

## Lifecycle evidence after separate approval

Use the existing A6 procedure in
`installer/windows/acceptance/LIFECYCLE-ACCEPTANCE-CHECKLIST.md` on its isolated
test target only. Do not use a live Dell installation. No Hyper-V instructions
are authorized by this document. Coordinate test-target availability separately.

1. Verify the accepted signed 1.3.4 baseline identity and hash from A6 and the
   candidate's signature/hash. Record actual versions; never mark a baseline
   accepted merely because the source checkpoint calls it accepted.
2. For each edition, capture before/after states with
   `Get-AnyAiCamInstallState.ps1`, then compare with
   `Compare-AnyAiCamInstallState.ps1 -Step Upgrade|Repair|Uninstall` and the
   appropriate ExpectedVersion. Edition, upload and retention switches are now
   non-secret allowlisted values. Never collect raw credentials or secret env
   values. A BLOCKED result is not PASS.
3. Upgrade signed 1.3.4 without an edition selection: preserve Full VMS behavior.
   For each fresh Gateway edition, test repair, refusal of a saved-edition switch,
   data-preserving uninstall and reinstall with retained data. Verify a refusal
   leaves services/files intact. Preserve Blue's legacy unreadable-config repair
   path when no edition change is requested.
4. With approved isolated licence fixtures, verify zero camera capacity before
   activation, accepted capacity during an offline period, and failure on a
   revoked/invalid trust grant. Do not bypass signing or alter live trust stores.
   Cloud entitlement cases remain mock-backed: denied, cancelled, unavailable,
   expired credentials, wrong camera, valid event, and retained retry intent.
5. Verify UI shows the edition, licence state and disabled event-upload state.
   Discovery, local recording/playback, notifications and Full VMS navigation
   must remain available under their existing permissions and licences. Verify
   clips stay playable offline; inspect both legacy and managed local retention
   paths. Do not delete customer media to exercise policy.
6. Run Blue's A1/A3 LocalSystem probes, A4 Service Job Object check and A5 catalog
   coverage only within the separately approved acceptance session. Capture
   their JSON and logs. Existing event-only, service-stop and signed-upgrade
   acceptance gates remain required; focused source tests do not replace them.

## Policy invariants

Local Gateway: no event, recording, backup or live-relay AWS video writes.
Cloud Gateway: producer event clips only, licensed camera capacity, explicit
event opt-in, motion entitlement, fresh server authorization, valid credentials
and region. Duration must be probeable and within the existing individual clip
sanity limit; optional thumbnails must be JPEGs. Denial keeps local retry intent.
Full VMS: its existing upload authorization and session caching remain intact.

Event retention remains 7/14/30 days under existing entitlement/customer policy.
The historical six-hour constant is not a default event quota. No daily event
cap, storage-deletion policy or local retention value is introduced by this work.
