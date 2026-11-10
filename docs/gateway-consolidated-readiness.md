# AnyAiCam Local Gateway and Cloud Gateway readiness

2026-11-02. **Source validated; compilation and installed beta acceptance blocked.**
Both existing agents completed a final bounded package-independent review. No
further proven defect or implementable missing feature with a resolved contract
was identified. Development stops safely at the external gates below. This is
not a claim that all possible defects have been excluded or a beta was accepted.

## Preserved source and validation

| Checkout | Preserved validated source |
|---|---|
| Main, `codex/windows-gateway-editions` | `b485832e8c9811c08740c608e2b6501ff6e47f6a` before this report-only commit |
| Application, `codex/gateway-sprint-app` | `731683841ec14c7ca549b682e80f2a2006d92939` |
| Installer, `codex/gateway-sprint-installer` | `2efcfeaa9ad0dbac7c1d6340d67cd664355b06f8` |

Existing branches/commits are preserved. The agent branches contain their owned
changes; main contains their compatible integration and coordinator fixes.
The final review made no runtime changes and repeated no package search, blocked
build attempt or unchanged test run. Working trees were clean before this report.

Latest integrated validation: **130 tests passed in 34.894s** using
`python -W ignore::SyntaxWarning tools/run_windows_gateway_light.py`.
Latest focused application notification run: **8 passed in 3.002s**. Latest
focused installer configuration/lifecycle run: **13 passed in 25.771s**. These
sets overlap and must not be added as independent coverage totals. Diff checks
passed. Existing Python invalid-escape warnings were filtered in the final run.

## Implemented functionality and evidence limits

| Area | Implemented behavior | Evidence and practical limit |
|---|---|---|
| Shared architecture | One VMS source/setup with Local Gateway, Cloud Gateway and Full VMS edition selection. Existing explicit settings and Full VMS behavior preserved. | Isolated mode/configuration and installer-source checks; no compiled candidate. |
| Local Gateway transport | Blocks event, recording, backup and S3 live-relay video writes even with conflicting upload flags. Local licence does not grant remote access. | Mock-backed transport/authorization checks; no real cloud writes. |
| Cloud Gateway transport | Explicit event opt-in, licensed camera, producer clip reference, existing motion entitlement, fresh authorization/credentials, duration/JPEG validation; bulk/live video transport refused. | Actual functions with mocked control plane/media/AWS; no real upload or delivery acceptance. |
| Local recording/viewing/playback | Licensed camera source and local-first playback remain independent of remote cancellation; LAN HLS passage survives denial. Unlicensed camera access stays denied. | Actual functions with synthetic capacity/database/path fixtures. No camera, FFmpeg recording, decoder, seek, real LAN stream or installed restart test. |
| Notifications | Local Basic Motion inbox, deduplication, recipient/tenant scope, read state, selected/all-camera rules and explicit alert denial. Removed cameras do not consume active notification capacity. | SQLite fanout/inbox and permission regressions. No external email/SMS/voice/push or installed browser delivery test. |
| Remote cancellation | Authenticated mode receipt, persisted-grant requirement, tunnel/media/P2P gates, independent teardown retries and reactivation. Atomic mode writes; received denial remains active in-process if writing fails. | Actual guard/middleware/session functions with mocked transport. Process-local latch does not prove durable revocation after restart. |
| Offline remote expiry | Authority data path and secure signed-grant proposal documented; no arbitrary deadline/grace introduced. | Not implemented: existing mode cache supplies no independent offline deadline. Signed feature/analytics contracts remain separate. |
| Gateway UI | Edition, local licence, event opt-in and last-received remote authorization/offline uncertainty displayed; unavailable bulk controls suppressed. | Rendered HTML parsing and source checks, not browser/installed UI acceptance. |
| Retry state | Durable event-media outbox serialization, atomic writes, denial/backoff/registration retry and resume behavior. | Small controlled fixtures and mocks; no load/stress or live outage test. |
| Installer preflight | Edition/mode/role validation, silent missing-mode refusal before mutation, pinned Inno metadata/source integrity gates. | Source and small PowerShell fixture checks; compiler has not run. |
| Lifecycle evidence | Detects settings/identity changes, unreadable identity evidence, aggregate recording shrink and missing byte evidence; binds snapshots to same computer/install/data roots. | Synthetic before/after snapshots. Aggregate counts/bytes do not prove individual file integrity or actual rollback. |

## Untested or unresolved acceptance

- Actual shared Inno 7.1.0 compilation, signed-uninstaller output, signatures,
  native catalog completeness and setup size gates on a new candidate.
- Fresh Local/Cloud Gateway install, upgrade, repair, refusal paths,
  data-preserving uninstall/reinstall and owner-managed rollback on Windows.
- Installed discovery, physical/simulated camera recording, live viewing,
  playable clips/seek after restart/offline periods, notifications and browser UI.
- LocalSystem Tesseract/SAPI/model probes, Smart App Control, WinSW child-process
  Job Object limits and signed baseline-to-candidate acceptance.
- Installed tunnel/proxy source trust, tenant media protection through the real
  topology, established-session teardown timing and reconnect behavior.
- Production-trust licence acceptance with approved isolated identities. Synthetic
  source fixtures do not satisfy production-key acceptance.
- Real cloud/provider delivery, broad full-application suites, stress/resource
  profiles, offline remote deadline enforcement and restart-durable revocation.

All installed-system checklist rows remain NOT RUN. No tests against production,
R430/Ryzen workloads, VMs or hardware were performed in this final review.

## Resources: reuse existing assets, recover only missing inputs

| Resource | Current evidence / action |
|---|---|
| Python 3.12.10 helper installer | Already present at `C:\AnyAiCam-Transfer\R430-win1341-supplement-20261028\tools\python-3.12.10-amd64.exe`, previously hash-verified. Needs later install-scope approval, not a replacement download. It is not the embedded runtime ZIP or an installed interpreter. |
| SDK tools | `C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\makecat.exe` and `signtool.exe` present. Integrity/current signing usability not established by presence alone. |
| Baseline/source | Existing `R430-win1341-release-handoff-579578b` baseline setup and source bundle remain reusable under their verification requirements. No replacement requested. |
| Sealed payload | Eight pinned vendor inputs, 108 locked wheels, Inno 7.1.0 and original public keyset remain unavailable in documented accessible locations. No further search or blocked build was attempted. |
| Windows environment | Historical `AAC-WinVMS-1341-Test` is a lead. Current disposable status, isolation and recoverable rollback checkpoint/image are unproven; no VM was queried or changed in this review. |

Last documented package origin: section 6 of the 2026-10-28 AlexDellLaptop handoff
names `C:\AnyAiCam-Transfer\R430-win134-parity-d6b4244`, sealed 2026-10-27.
This is not a currently verified recovery location. No accessible candidate
manifest/member bytes have been found. The original `SHA256SUMS.txt` must hash to
`e9d6d867a764e48c07f6090d196ef0660dfac6c30f1dd91e6e49267e32bd43a0`.
Every recovered member must then pass its existing integrity/provenance gates.

The original production public-key requirement is unchanged:
`2389a655fb3e5ed91e8a4a9216f9c45b97feb3847571b0536384b8305979af7c`.
Existing build documentation explicitly labels it production. Recover the approved
public-only bytes and confirm the intended current environment/digest before
canonical validation. No generated/replacement or synthetic DO-NOT-SHIP keyset
may satisfy production acceptance; no signing or authorization check is waived.

## Exact remaining owner decisions, staged

1. **Next compilation input:** identify/authorize an exact accessible original or
   backup package location, or transfer the unchanged package and original manifest
   into an authorized development location. Independently confirm its seal and
   intended production-key environment. Request standalone public keyset or pinned
   Tesseract only if package verification proves it absent. Do not resend existing
   Python/SDK/baseline/source assets.
2. **After recovered inputs verify:** approve exact Python 3.12.10/Inno 7.1.0
   installation scope/path. Confirm setup version 1.3.4.1 and customer release
   1.2.7, or provide the intended approved values; neither is a built candidate.
   Authorize the current existing `-Sign` or `-AzureSign` secure signing workflow.
   The historical leaf expired October 27 and is not assumed usable for new signing.
3. **Before build or RAM-intensive testing:** choose the machine/workload. Obtain
   fresh available RAM/disk, estimate additional peak RAM/disk and duration, then
   request explicit owner approval. No resource window is requested or implied now.
4. **Before installed acceptance:** name the disposable Windows target, owner,
   isolated network, approved test identities/media and recoverable checkpoint/image
   identifier/restore procedure. Approve exact installation/service/rollback actions
   separately. No VM/hardware alteration is authorized by the development request.
5. **Remote expiry authority/policy:** decide which v2/legacy sources authorize
   remote access, their precedence/fallback, scheduled cancellation/downgrade and
   renewal/failed-payment handling. Set maximum offline revalidation, deadline
   inclusivity/skew, acceptable source freshness and naive-timestamp timezone.
   Existing seven-day server grace is not an approved remote cache TTL.
6. **Remote expiry security/rollout:** approve signing purpose and compatible
   production trust-registry rollout; generation/identity-transfer/key-lifecycle
   rules; missing/invalid document and client/server downgrade migration, including
   Full VMS scope; clock/disk-restore threat model and durable-denial mechanism;
   maximum teardown delay and authenticated proxy/tunnel topology. No unresolved
   value has been selected by the agents.

Local camera licensing, recording, LAN viewing and playback remain independent
of remote subscription expiry/cancellation under the approved design. Remote
authority must never rewrite accepted local capacity or retention settings.

## References and safe resumption

- `windows-gateway-resource-provenance.md`: exact eight inputs, 108-wheel lock,
  hashes, historical paths and completed targeted-search evidence.
- `gateway-offline-remote-deadline-proposal.md`: source trace and security design.
- `windows-gateway-beta-acceptance-checklist.md`: unexecuted acceptance matrix.
- `gateway-development-handoff.md` and `windows-gateway-editions-checkpoint.md`:
  integration history and preserved next-step context.

Resume on new actionable evidence or an owner decision above. Do not repeat
package searches, rebuild completed components or invent tasks to avoid stopping.
No deployment, live billing, retention, VM/hardware or production changes occurred.
