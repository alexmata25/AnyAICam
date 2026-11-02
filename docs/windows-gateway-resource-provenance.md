# Gateway build and acceptance resources: reuse record

2026-11-02, ALEXDELLSERVER. Read-only documentation and exact-path inspection;
no build, installer execution, extraction, VM operation or signing session.
Existing branches, toolchain pins, trust settings and customer data are preserved.

## Existing resources to reuse

| Resource | Exact location / identity | What is established |
|---|---|---|
| Accepted baseline setup | `C:\AnyAiCam-Transfer\R430-win1341-release-handoff-579578b\installer\AnyAiCam-VMS-Setup-1.3.4-e3d7eda.exe` | Present, 618,425,712 bytes. Recorded SHA-256 `cafc1d1be9f457561094fbf756255d64266a249ae75c994c78a14a3e725df034`; not rehashed/executed in this pass. For lifecycle baseline, not dependency recovery. |
| Integrated foundation bundle | Same handoff, `git\anyaicam-vms-1341-codex-579578b-full.bundle` | Present, 26,962,939 bytes. Existing source review is sufficient; retain current Gateway commits. No new clone/import needed. |
| Approved helper installer | `C:\AnyAiCam-Transfer\R430-win1341-supplement-20261028\tools\python-3.12.10-amd64.exe` | Present, 26,964,224 bytes. Previously verified SHA-256 `67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb`, also in supplement `SHA256SUMS.txt`. Reuse after approved installation; this EXE cannot be passed as `-PythonExe` and is not the embedded runtime ZIP. |
| SDK tools | `C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe` and `makecat.exe` | Present, 543,144 and 55,720 bytes respectively. Presence only; signing identity/current authorization unverified. |
| Dependency specifications | `installer/windows/build.ps1`, `installer/windows/wheels.lock.sha256`, `installer/windows/DEPENDENCIES.md` | Eight pinned vendor inputs and 108 wheels. Recorded wheel-lock SHA-256 `cd4fe6364634e16e4ee82f3ceb1a64bb5da6b43cd9ea06149c1be4f678470248` verifies the specification, not wheel bytes. |

The handoff's baseline acceptance includes failed/incomplete reruns. Its accepted
release identity does not establish clean lifecycle, licensing or service results
for either Gateway. Retest on the approved isolated target.

## Inputs still needed, with provenance

| Input | Documented recovery lead / expected identity | Exact finding and owner action |
|---|---|---|
| Sealed vendor/wheel/compiler payload | `C:\AnyAiCam-Transfer\R430-win134-parity-d6b4244\`; original `SHA256SUMS.txt` SHA-256 `e9d6d867a764e48c07f6090d196ef0660dfac6c30f1dd91e6e49267e32bd43a0` | Exact folder absent; installer worktree `installer/windows/vendor` absent. Provide existing unchanged package or exact authorized source path; independently confirm seal before checking every member. |
| Tesseract bundle, if not in payload | Original relative lead `anyaicam-blue\windows-parity-scope-20261023\handoff-artifacts-d6b4244\`; `tesseract-5.5.2-win64-anyaicam.zip` SHA-256 `221b4599a9310737d8f3a2d8a93477eb3c495d41b5eca8af6ed7b79e0781fbf7` | Relative lead does not identify an accessible absolute host path. Owner supplies original bundle location; do not rebuild/substitute it to bypass recovery. |
| Public entitlement keyset | `C:\Users\Alejandro Mata\anyaicam-red\windows-1.2.7-build\inputs\entitlement-signing-public-keys.json`, or `C:\VMS134Build\rc11-e3d7edab\installer\windows\output\entitlement-signing-public-keys.json`; historical SHA-256 `2389a655fb3e5ed91e8a4a9216f9c45b97feb3847571b0536384b8305979af7c` | Both exact files absent. Transfer existing public-only file, independently confirm environment/digest, then canonical-validate. No replacement keys. |
| Inno 7.1.0 | `%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe` (current expansion `C:\Users\Administrator\AppData\Local\Programs\Inno Setup 7\ISCC.exe`) | Absent. Recover approved installer/hash via sealed package; installation scope needs approval. Existing build guard requires PE version 7.1.0.0. |
| Installed Python helper | `%LOCALAPPDATA%\Programs\Python\Python312\python.exe` or `C:\Program Files\Python312\python.exe` | Both previously checked exact paths absent. Reuse available approved installer; approve install path/scope. Build explicitly requires Python 3.12.10 when `-PythonExe` is supplied. |
| Current signing and metadata | Existing `-Sign` or `-AzureSign` flow; planned setup 1.3.4.1/customer release 1.2.7 | Historical baseline signer expired October 27; no current session inspected. Owner confirms exact metadata and authorizes current secure signing flow. Existing publisher pins remain. |

The supplement's focused licensing harness contains test-key generation code.
It is not an approved keyset source and was not run. The sealed baseline setup
also does not replace the independent public-keyset provenance requirement.

## Windows acceptance target and rollback evidence

`installer/windows/acceptance/LIFECYCLE-ACCEPTANCE-CHECKLIST.md` names historical
R430 VM **AAC-WinVMS-1341-Test**, Server 2022 / Private switch, and requires a
`pre-1341-lifecycle` checkpoint. Its unchecked precondition is a procedure,
not evidence that this checkpoint exists or that the VM is disposable now.

New bounded historical review found:

- `C:\AAC-Dev-Reports\History\2026-11-01_2121_aac-dev-resource-coordination.md`
  records that VM running with 4,096 MB at 03:20 UTC; host free RAM 6,121 MB.
  These historical measurements cannot authorize or size a new run.
- `C:\AAC-Dev-Reports\History\2026-10-31_1201_windows-uninstall-preserved.md`
  explicitly requires approval before using that VM. Its preserved MSI patch
  belongs to the separate Cloud Gateway adapter repository and was not validated
  on Windows; do not import it into this Inno VMS setup as accepted work.
- `C:\AnyAiCam-Lifecycle-Handoff\01b-host-create-vm-OFF-private.ps1` describes
  creation of **AAC-Linux1271-Lifecycle**, Ubuntu, powered off, 8 GB / 120 GB VHD.
  It explicitly preserves the Windows VM; it supplies no Windows rollback proof.
- `C:\AAC-Dev-Reports\Archive-Dell-to-R430-Handoff-20261030\HANDOFF-R430-GATEWAY-DEVELOPER.md`
  describes adapter source/reference bundles, not the sealed Windows toolchain.

Owner/operator must confirm whether AAC-WinVMS-1341-Test is the disposable
target, its current workload/data owners, isolated network/test identities, and
an existing recoverable checkpoint/image with exact identifier and restore
procedure. Otherwise supply another named disposable Windows target. No current
Windows target or rollback readiness is proven by the available records.

After inputs and target are confirmed, measure fresh RAM/disk on the named build
host and test guest; estimate peak additional RAM, staging/evidence disk and
duration for that exact workload before requesting execution. Build one shared
setup, then test `/GatewayEdition=local_gateway` and `cloud_gateway` independently
using existing lifecycle/UI/cancellation procedures and synthetic media. Record
candidate signature/hash and keep failures/BLOCKED evidence. No service, VM,
retention or billing change is authorized by this resource report.

This investigation adds exact provenance and reuse distinctions; it does not
repeat the exhausted broad artifact searches or claim global absence.
