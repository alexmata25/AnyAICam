# Windows VMS 1.3.4.1 direct-test preparation

Date: 2026-11-09  
Branch: `codex/windows-vms-1341-direct-isolated-test`  
Base: `99b6dc57025ca61dbc54cdaf4689c25c7283438b`  
Installer/build: not produced. No installation or host firewall rules were applied.

## Candidate work completed

- Direct-test networking pins one canonical RFC1918 camera subnet (`/24` through `/32`) to one attached IPv4 interface. Setup refuses ambiguous interface matches. Discovery targets are constrained to that subnet. VMS TCP probes, ONVIF TCP probes, ONVIF multicast send/receive, and FFmpeg/FFprobe RTSP inputs bind to the selected interface address.
- Credentialed HTTP/HTTPS/RTSP/ONVIF verification rejects addresses outside the pinned subnet, binds connections to the selected source address, disables system/environment proxies, and refuses redirects.
- The run-scoped executable firewall policy blocks IPv4 destinations outside camera CIDR, loopback, and ONVIF multicast; blocks IPv6; constrains camera and multicast rules to the selected local address/interface; and checks the effective outbound default action. Application and service startup fail closed unless `WeakHostSend` is disabled on every active IPv4 interface. Rule installation/removal verify run ownership, executable paths, local/remote address filters, interface filters, and active policy state.
- Direct-test management stays on `127.0.0.1:18767`. Test configuration disables cloud/recording/event/analytics uploads, facial sync, relay, and P2P; removes proxy/AWS environment settings; and disables AWS metadata lookup. No firewall rules have been applied, so effective host policy and real camera routing remain unverified.
- No approved signed licensing trio is present in the candidate. The existing Codex 2-reviewed R1 host signer is restricted to the `sandbox-test-50296db437b2` synthetic key and `sign_feature_entitlement` requests through the fixed Sandbox mailbox/capability flow. It can issue feature-entitlement snapshots; the direct installer separately requires a synthetic appliance identity and accepted-capacity state. No direct-Dell fixture assembly/signing workflow or approved fixture files were found. No private key was opened or used, and no signature/key was created.
- Backup plan and fixture script use only a unique temporary NTFS fixture. Ordinary file copy/restore hashes passed. Full metadata/SACL/ADS backup/restore did not pass: the current token lacked SeBackupPrivilege, SeRestorePrivilege, and SeSecurityPrivilege/Manage Auditing; ADS and SACL preservation were therefore not verified. No encrypted destination was confirmed. No production ProgramData was read.

## Verification

- Direct-test isolation/profile/licensing contract tests: **49 passed**.
- Credentialed camera network binding tests: **4 passed** (source validation, HTTP/HTTPS source address, RTSP source address, and no system-proxy lookup).
- Native Windows PowerShell firewall policy: **PASS**; 58 CIDR blocks cover the unapproved IPv4 complement, and tests reject an alternate active NIC with `WeakHostSend=Enabled`.
- Native Windows PowerShell parser: **8/8 changed scripts parsed**.
- Existing camera-capability tests: **13 passed**, 2 pre-existing Pydantic deprecation warnings.
- Existing AI substream frame tests: **27 passed, 1 skipped**.
- Existing camera-address-recovery regression progressed through at least 92% with no observed assertion failure, then stalled in an event-loop/socket-gating case. The rerun excluding that case also stalled before producing a final pytest summary; treat the suite result as **incomplete**, not passed.
- Existing live-stream-copy regression stalled during its async ffprobe-probe case. A selected non-async subset emitted passing cases but also stalled before a final summary; treat the suite result as **incomplete**, not passed.
- Python `compileall` and `git diff --check`: **passed**.
- Codex 2 independent source review: **PASS WITH NOTES**. The reviewer found the source-binding/proxy/WeakHostSend corrections present and no new code blocker. It did not rerun the tests or apply firewall policy. It reiterates backup and licensing blockers.

## Read-only Dell network observation and required CIDR decision

`Get-NetAdapter`/`Get-NetIPAddress` inspection was denied by the session's CIM access boundary. Read-only `ipconfig` showed:

- `Wireless LAN adapter Wi-Fi`: `192.168.0.56/24` (`192.168.0.0/24`)
- `vEthernet (AnyAiCam-Release-Test-NAT)`: `172.31.252.1/24` (`172.31.252.0/24`)
- `vEthernet (Default Switch)`: `172.17.96.1/20`
- `Tailscale`: `100.119.39.95/32`

None of these observations proves which segment carries the approved synthetic camera fixture. Do not approve the broad Wi-Fi subnet by inference. Owner must name the camera/simulator CIDR and matching interface, or confirm that the test-NAT interface contains only the approved synthetic camera endpoints.

## Remaining owner gates

1. Confirm the exact synthetic-camera CIDR and interface. The build is not ready until this is pinned.
2. Provide approved signed identity, feature-entitlement, and accepted-capacity inputs, or separately authorize a run-bound direct-Dell adaptation of the reviewed R1 host-signer workflow. Do not generate signatures or access the private key during preparation. The R1 mailbox client is Sandbox-path-bound and its signer creates feature-entitlement snapshots, not the complete direct installer input trio.
3. Select an encrypted NTFS backup destination with `VolumeStatus=FullyEncrypted`, `ProtectionStatus=On`, and `EncryptionPercentage=100`. Have an administrator run the fixture rehearsal with SeBackupPrivilege, SeRestorePrivilege, and SeSecurityPrivilege/Manage Auditing enabled. Require hash and metadata manifest equality for copy and restore; do not access retained production ProgramData during rehearsal.
4. After these gates, obtain separate authorization to build and install the isolated candidate on the Dell. No build or installation has been performed.

Work remains in the isolated direct-test candidate; nothing was changed on Blue's integration branch.
