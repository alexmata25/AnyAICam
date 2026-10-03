# Deferred Hyper-V and clean-machine procedure

PREPARED ONLY. None of these host commands have been executed.
Requires explicit approval for Hyper-V enablement, scheduled restart, VM storage
and virtual-switch creation. Do not stop/reconfigure existing WSL or Docker now.

## Host prerequisites and approval

Observed Dell: Windows Pro edition, ~32 GB RAM, 16 logical CPUs, ~289 GiB free C:.
WSL2 is installed and currently used by CloudAdapter and Docker. Full Hyper-V
features, service and management tools are disabled/absent. Current shell is not
an elevated administrator. CPU virtualization flags reported by Win32_Processor
are inconclusive while a hypervisor is already active; do not change BIOS based
on those flags alone.

When the user approves, open PowerShell **Run as administrator**:

    Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All -NoRestart

Review RestartNeeded. Save active work and schedule interruption/restoration of
CloudAdapter/Docker with the user, THEN:

    Restart-Computer

Do not run wsl --shutdown, stop Docker/CloudAdapter, change .wslconfig, enable
Windows firewall rules or alter the physical NIC as part of today's work.

After the approved restart:

    Get-Module -ListAvailable Hyper-V
    Get-Service vmms
    Get-VM
    wsl --list --verbose

## ISO and isolated VM creation (later elevated administration)

Download Ubuntu 24.04.4 Desktop amd64 from Canonical's official release directory.
Verify its SHA-256 against dependencies.lock.json AND authenticate the published
SHA256SUMS signature using Ubuntu's image signing key before booting.
The ISO is NOT downloaded here; metadata was retrieved only.

Choose an empty VM storage directory and verify the private subnet is not already
routed anywhere in the intended lab. Do not overwrite an existing VM/switch/VHD.

    $vmName = 'AnyAiCam-Installer-Test'
    $switchName = 'AnyAiCam-Installer-Private'
    $vmRoot = 'C:\AnyAiCam-TestVMs\Installer-Test'
    $isoPath = 'C:\AnyAiCam-TestVMs\ISO\ubuntu-24.04.4-desktop-amd64.iso'
    if (Get-VM -Name $vmName -ErrorAction SilentlyContinue) { throw 'VM name already exists' }
    if (Get-VMSwitch -Name $switchName -ErrorAction SilentlyContinue) { throw 'Inspect existing switch before reuse' }
    if (Test-Path -LiteralPath $vmRoot) { throw 'Use a new empty VM directory' }
    New-Item -ItemType Directory -Path $vmRoot | Out-Null
    New-VMSwitch -Name $switchName -SwitchType Private
    New-VM -Name $vmName -Generation 2 -MemoryStartupBytes 12GB -Path $vmRoot -NewVHDPath "$vmRoot\Ubuntu.vhdx" -NewVHDSizeBytes 180GB -SwitchName $switchName
    Set-VMProcessor -VMName $vmName -Count 4
    Set-VMMemory -VMName $vmName -DynamicMemoryEnabled $false
    Set-VMFirmware -VMName $vmName -EnableSecureBoot On -SecureBootTemplate MicrosoftUEFICertificateAuthority
    Add-VMDvdDrive -VMName $vmName -Path $isoPath
    $dvd = Get-VMDvdDrive -VMName $vmName
    Set-VMFirmware -VMName $vmName -FirstBootDevice $dvd
    Set-VM -Name $vmName -AutomaticStartAction Nothing -AutomaticStopAction ShutDown

The private switch has no host network adapter, physical bridge, DHCP or NAT.
Prepare a separate helper VM on it: 2 vCPU, fixed 4 GB RAM, 40 GB disk, same ISO.
Use static private guest addresses after identifying the actual guest NIC:
installer 192.168.253.20/24, helper 192.168.253.10/24.
No gateway is needed between the isolated guests.

Installation still requires dependency access. A separately approved temporary
Default Switch NIC on the helper can provide an HTTP(S) dependency proxy/cache;
do NOT bridge the private test segment to the customer LAN. Restrict proxy access
to the test subnet and required public package registries. The appliance remains
on the private NIC. The exact proxy allowlist must be validated against the
installer/Docker dependency requests before the clean run; it is a prerequisite,
not supplied/claimed functional here. Do not silently relax camera/claim network
protections to make downloads work. This is preferable to modifying WinNAT or
the physical host NIC while CloudAdapter/Docker are in use.

Starting/installing either VM and adding its NICs happen only after approval.
Install a standard Ubuntu desktop user. Verify >=100 GiB real free root space,
4+ CPUs, 8+ GB usable RAM allowance and no AnyAiCam markers. Take a clean snapshot.

## Portal prerequisites — prepared, not provisioned

Use portal.env.example and owner-location.plan.json on the helper only.
Export exact fixed app/schema files from a1c81e0f4926... (based on d5af9c53...); do not reuse an existing
staging/prod checkout, environment, account database or bindings.
Create a new guest-local CA/certificate for installer-test.test, trust it inside
both disposable guests, and map the hostname to the helper address.
Reverse proxy HTTPS to its local portal process. Set the application's exact
public origin, trusted host, HTTPS-only/secure cookie and CSRF flags. Generate
new ANYAICAM_APP_SECRETS, ANYAICAM_PORTAL_SECRET and Fernet claim-flow key at
provisioning time; no real keys are stored in this repository.

Initialize the exact pinned database schema only after approval; create the
planned owner/customer/site and scoped identity grant through supported model
helpers. Apply approved test camera_slots_local/vms_license fixtures (8 cameras)
only to that isolated database, without payment API/webhook calls.
Use the existing private installer download publisher only against isolated
guest-local storage if needed for the browser download test; never publish the
artifact to the public website. No database, owner account, certificate or portal
instance has been created in this preparation step.

## Clean-machine acceptance checklist

For every step record pass/fail, command/UI evidence, source version and logs.
Redact setup codes, possession secrets, credentials, cookies and claim proofs.

- [ ] Authenticate ISO signature/checksum; install clean Ubuntu on a new VHD.
- [ ] Confirm clean state, CPU/RAM/free disk and no existing service/user markers.
- [ ] Take baseline snapshot. Start only the isolated portal and simulator.
- [ ] Download exact versioned archive through the isolated licensed-owner portal.
- [ ] Match outer checksum, embedded file manifest, release.env and source commits.
- [ ] Extract fresh directory; run setup.sh as desktop user; allow real sudo install.
- [ ] Real installer provisions dependencies, directories, identity and services.
- [ ] Existing validate.sh succeeds against actual OS/Docker/VMS startup.
- [ ] Browser opens; actual owner sign-in returns to claim page.
- [ ] Choose owned location, review device and explicitly confirm.
- [ ] Claim status/complete requires the actual device possession secret.
- [ ] Agent completes real enrollment; VMS and agent start and show exact build.
- [ ] Discovery sees ONLY the synthetic ONVIF device, including metadata/profile URI.
- [ ] Add synthetic camera; verify RTSP transport, stream liveness and camera state.
- [ ] Live view works through the supported local path; record actual decoded frames.
- [ ] Record test video; locate generated media and prove browser playback.
- [ ] Basic motion analytics detects moving test pattern and produces an event.
- [ ] Guest reboot preserves identity/bindings, services, stream and recording.
- [ ] Popup/origin/COOP failure preserves manual setup-code recovery.
- [ ] Refuse wrong owner/site, missing/bad CSRF, wrong origin, expired/replayed claim,
      incorrect possession secret and missing encryption key.
- [ ] Corrupt package fails before privileges/mutations.
- [ ] Verify exact 100 GiB byte boundary on disposable VM; verify the corrected exact-byte boundary.
- [ ] Default uninstall preserves documented state; reinstall uses supported technician
      install/claim flow, since the fresh launcher refuses retained identity.
- [ ] Restore clean VM snapshot for fresh-install recovery; do not pretend initial
      install has a previous-version rollback point.
- [ ] Separately exercise an actual valid rollback point with reviewed fixture
      previous release, database preservation and optional restore. No Software
      Update changes or production deployments.
- [ ] Freeze exact 1.1.1 investor archive/checksum plus install/uninstall/recovery docs.
- [ ] Mark clean-machine acceptance complete only with actual OS lifecycle evidence.
- [ ] Publish nothing; Windows native implementation waits for this acceptance.

The offline camera-simulator bundle includes MediaMTX and the complete FFmpeg/Python
package closure. Use its manifest and --no-download guest-only install instructions.
The appliance installer still requires its existing Ubuntu/Docker/Python/VMS dependency
network access; this simulator bundle does not convert the VMS installer to offline mode.
