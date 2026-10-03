# Local Windows EXE preparation â€” runtime adapters are missing

This directory prepares an **unsigned local test** EXE from an already prepared
offline Windows payload. It does not convert the Ubuntu installer, contact a
portal, publish, install anything on the build host, or import another branch.
The existing Windows installer branch was inspected but not combined: its VMS
service installer does not implement this device-bound owner-confirmed handoff.

## Architecture

Inno Setup packages a pinned Windows VMS/runtime payload and two future native
adapters. The administrative wizard runs InstallRuntime.exe --fresh-install,
waits, and stops on failure. After success, its finish page launches
ConnectAnyAiCam.exe as the original desktop user (interactive installs only).
Silent installs deliberately do not start a browser.

The runtime adapter must install an offline private Python/runtime, agent, media
dependencies and Windows service, generate a cryptographically random UUIDv4 and
device possession secret, enforce administrator/service-user ACLs, validate
startup, refuse existing installations, and implement rollback. It must preserve
existing activation and enrollment behavior. None of that Windows implementation
is supplied by this scaffold.

The connection adapter must ask a service-owned claim worker over authenticated,
ACL-restricted local IPC to start the existing claim flow. The service keeps the
possession secret and credentials; the desktop receives only the expiring setup
code. It must refuse an elevated desktop/browser context even if the installer
was explicitly started elevated. The worker uses existing begin/status/complete and
enrollment APIs with that protected possession secret; port the Linux service
invocation/state paths/permissions without passing secrets in arguments; open
the desktop user's browser using the same nonce-gated loopback bridge; preserve
HTTPS portal validation, exact origins, opener/window checks, challenge, expiry,
code/proof handling and manual-code recovery. Owner login, location selection
and explicit confirmation remain mandatory. It must not use a mere user-supplied
device ID or a privileged sign-in browser. Do not run setup.sh via WSL or simply
remove require_fresh_desktop_install to pretend Linux installation is native.

## Offline payload / build contract

A new payload directory must contain InstallRuntime.exe, ConnectAnyAiCam.exe,
all their offline dependencies and payload.json:

    {
      "platform": "windows-x64",
      "source_commit": "<40 lowercase hex digits of reviewed runtime source>",
      "version": "0.1.0",
      "files": [{"path": "InstallRuntime.exe", "sha256": "<full file checksum>"}, ...]
    }

List every payload file except payload.json. The stager checks source/platform,
version, safe unique paths, no symlinks, all checksums, completeness and Windows
executable signatures (MZ header only, **not Authenticode verification**).
The manifest is a consistency check, not a signature or trust authority; inputs
must come from a reviewed build. There are no adapters/payload binaries here.

Invoke PowerShell build.ps1 with -Payload, -OutputRoot (a fresh location),
-SourceCommit, and -PythonPath (local Python). Default operation stages only.
Add -Compile -InnoCompiler <local ISCC.exe> to compile AnyAiCam-test.exe.
No dependencies are downloaded automatically. Do not distribute this test
artifact. A successful build does not prove installation correctness.

## Release gates and effort

Before the first useful native test EXE: implement the runtime/service/identity
adapter and secure desktop claim adapter, prepare pinned offline dependencies,
then build and exercise in a disposable clean Windows x64 VM.
Estimate: **5â€“10 engineering days** for an engineer familiar with this repository,
assuming core VMS and required media dependencies run on Windows. Existing
Windows service work may shorten that after a separate scoped review; it has
not been validated or imported here. OS-specific VMS/agent issues can expand
the estimate.

Before customer distribution: clean-machine install and reboot/service tests,
browser blocked/COOP/login expiry/manual recovery, tampering/ACL and possession
tests, uninstall/data retention/rollback behavior, license inventory, dependency
integrity, Authenticode signing and review. Separately authorize adapting the
customer download publisher/catalog to accept an EXE (it currently only accepts
Ubuntu .tar.gz). No change to that publisher is included here.
