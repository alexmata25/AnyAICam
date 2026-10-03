# Samsung physical clean-machine validation — active plan

This replaces the Dell Hyper-V plan. Dell host configuration, networking and
existing WSL/Docker services remain unchanged. Do not enable Hyper-V or reboot
Windows. Do not access Ryzen, shared staging, production, Billing, Household,
Stripe or the website. Use the existing unpublished archive, not a rebuilt one.

## Authorization and current evidence

The user explicitly authorized wiping/reinstalling the Samsung and stated that
nothing important needs preservation. That supersedes the old Samsung checkpoint's
no-reimage constraint. No wipe/repartition/install/reboot has been performed.

Authorized endpoint: alejandro-mata@100.99.177.7.
Samsung is now online and positively verified: hostname anyaicam-samsung, SAMSUNG
930MBE/P02AHK, Intel i7-8565U (4 cores/8 threads), approximately 16 GB RAM, UEFI.
Current Ubuntu is 24.04.5 LTS. System NVMe is SN520 SDAPNUW-512G-1004,
serial 193077800308, 512,110,190,592 bytes, root nvme0n1p2 and EFI p1.
Current disk capacity is sufficient; the fresh-OS free-space gate remains pending.
The user confirmed physical console help and an erasable dedicated USB.
USB is General UDisk, serial 2408200641348662589024, 31,457,280,000 bytes.
Authenticated Ubuntu ISO download and guarded USB preparation are underway.
See SAMSUNG_INVENTORY.md and SAMSUNG_PHYSICAL_RESULTS.md for live evidence.

Samsung requires a local sudo password; never send it through chat. The media
writer is open in Samsung GNOME Terminal, waits for a verified ISO, and writes
ONLY the exact authorized USB. The system disk is erased only from the booted
physical Ubuntu installer after rechecking hardware and disk identity.
No isolated portal/helper host or lab network is reserved yet.
SSH access is lost when the old Ubuntu/SSH/Tailscale state is erased.
Do NOT erase the mounted root disk through the running SSH session.

## 1. Positive identification and read-only inventory — before any erase

Use existing trusted SSH authentication; do not disable host-key checking. Record:

    hostnamectl
    cat /etc/os-release
    uname -m
    lscpu
    grep -E 'MemTotal|MemAvailable' /proc/meminfo
    sudo dmidecode -t system -t baseboard
    lsblk -b -o NAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL,TRAN,UUID
    findmnt -no SOURCE,FSTYPE,OPTIONS /
    df -B1 /
    ip -brief address
    ip route
    timedatectl

Compare chassis/model/serial with the physical Samsung and explicit endpoint.
Stop if DMI identifies Dell, if the endpoint or disk is ambiguous, or if any
secondary/internal/external disk has an uncertain role. Record the root partition,
its parent physical disk and persistent /dev/disk/by-id identifier, model, serial,
size and existing partition layout. Never substitute a guessed /dev/sda or nvme0n1.
No probing or discovery of other computers/cameras is authorized.

Inspect state read-only, using sudo only for reads when necessary:

    getent passwd anyaicam
    systemctl list-unit-files 'anyaicam*'
    systemctl list-units --all 'anyaicam*'
    docker ps -a --format '{{.Names}} | {{.Image}} | {{.Status}}'
    docker image ls --digests
    ls -ld /etc/anyaicam /opt/anyaicam /opt/anyaicam-agent /var/lib/anyaicam /var/log/anyaicam
    find /home /etc/anyaicam /var/lib/anyaicam /opt/anyaicam -xdev -maxdepth 3 -type f -name '*.db' -printf '%p %s bytes\n'

Record service status, image/build identity, mount paths and directory sizes.
Inspect no complete docker environment, .env, identity JSON, private keys, passwords,
RTSP credentials or claim proofs in chat/logs. Do not query cameras or remote portal.
Record any additional OS/data/recovery partitions, user files, recordings, database,
Docker volumes, snapshots, network/SSH/Tailscale enrollment and encryption recovery
material. Inventory may require additional targeted read-only commands after layout
is known. Lack of sudo/read access is a blocker to a complete preservation report.

Requirements: Ubuntu 24.04 x86_64, >=4 logical CPUs, >=8 GB installed RAM with the
installer's >=7.2 GiB usable allowance; 16 GB preferable for analytics. Check disk
health using already available read-only tools; install nothing for inventory.
Budget a >=180 GB system disk for comfortable fresh-OS free-space margin, preferably
256 GB or larger. Capacity is only a planning estimate: actual fresh root availability
must be >=107,374,182,400 bytes (100 GiB) after Ubuntu installation and before VMS.
A marketing disk size, current free space or unallocated secondary disk is not proof.

## 2. Loss/preservation decision

User has waived preservation. No backup is required by that decision, but record
what exists before loss. Erasing the selected system disk destroys ALL partitions
and accessible data on that disk: Ubuntu/other OS/recovery partitions, user files,
recordings, customer/camera databases, configurations, identities/credentials,
Docker images/volumes, SSH host/user keys, Tailscale enrollment, logs and rollback
points. Nothing on other disks is authorized for erasure without exact identification.
Existing remote appliance/claim records are NOT erased by wiping Samsung; do not
edit/delete them in staging or production. Do not reuse old appliance identity in
new lab enrollment. No old database/config is restored into clean acceptance state.

If the user later elects preservation: stop, identify an external encrypted backup
destination and enough free space. Quiesce AnyAiCam services only during an approved
maintenance window, make consistent database backups plus configuration/state/user
file backups or a full offline disk image, verify checksums and a restore sample,
then disconnect backup media before erase. Keep secrets out of Git/chat. No backup,
service stop or file copy on Samsung has occurred in this preparation.

## 3. Fresh Ubuntu media and wipe procedure — conditional, not executed

Recommended first reproducible test: Ubuntu **24.04.4 LTS Desktop amd64**, matching
the established Ubuntu 24.04 baseline and supporting the authenticated browser
launcher. The installer checks VERSION_ID=24.04, not a Windows/WSL distribution.
Canonical still lists the exact image (latest 24.04 Desktop is newer); use the
exact filename rather than a release-directory headline or latest symlink:

ubuntu-24.04.4-desktop-amd64.iso
https://releases.ubuntu.com/24.04.4/ubuntu-24.04.4-desktop-amd64.iso
SHA-256: 3a4c9877b483ab46d7c3fbe165a0db275e1ae3cfe56a5657e5a47c2f99a99d1e

Authenticate Canonical SHA256SUMS.gpg and compare downloaded ISO digest before
writing media. Canonical metadata signature is now authenticated; ISO download is underway.
Use a specifically identified empty/dedicated >=16 GB USB. Writing installer media
also erases that USB; never select a backup disk. Prepare media on approved equipment;
no Dell host changes or software installation. Console operator/independent boot
management is necessary; exact Samsung boot-menu key is model-specific and cannot
be assumed before model identification.

After positive machine/disk identification and all above gates:

1. Disconnect customer-camera networks and all backup/secondary drives where practical.
   Connect Samsung only to the approved isolated wired lab segment. Record isolation.
2. Boot the authenticated USB in UEFI mode. Choose Try Ubuntu first; re-check DMI,
   CPU/RAM, NIC functionality, all disks and the exact system-disk serial at console.
   Do not mount old disks read-write. Stop for driver, RAID/RST, encryption or disk
   ambiguity; do not blindly change BIOS storage mode or firmware settings.
3. Choose Install Ubuntu, Interactive/default Desktop installation. Select
   **Erase disk and install Ubuntu** on the VERIFIED system disk only. Review final
   partition-change summary: ALL old partitions on that disk are replaced. Use the
   entire disk for a fresh root filesystem; no keep-home, alongside-OS or restore.
   Use a plain ext4 layout for the reference test; record actual EFI/root allocation.
4. At the final destructive installer confirmation, re-match model/serial to inventory.
   Authorization is already given for this Samsung system disk, but any mismatch
   requires stopping. No guessed destructive shell command is supplied or executed.
5. Complete installation with a NEW normal desktop sudo user, fresh hostname
   anyaicam-samsung-clean-test and a privately stored new password. No autologin.
   Record ISO, installed OS/kernel, partition table and any chosen Ubuntu updates.
6. Reboot Samsung only, remove USB and sign in. No Dell/Windows restart.

Erase/install provides a new partition/filesystem/OS and removes prior accessible
AnyAiCam state; it is NOT a forensic SSD hardware sanitize. Do not invoke nvme format,
blkdiscard, ATA secure erase, dd or other additional sanitization blindly. If forensic
sanitization is specifically required, stop and select the model-supported method
with explicit scope; it is unnecessary to prove a fresh installer filesystem.

## 4. Prove fresh OS and resources before installing AnyAiCam

Record /etc/os-release, kernel, architecture, lscpu, MemTotal, lsblk and df -B1 /.
Verify root available >=107374182400 bytes; installer rejects one byte below.
Confirm no anyaicam account, services/units/drop-ins, Docker containers/images,
/etc/anyaicam, /opt/anyaicam, /opt/anyaicam-agent, /var/lib/anyaicam or /var/log/anyaicam.
Do not restore the old home, SSH/Tailscale identity or AnyAiCam database/configuration.
Record baseline files/service list and take an optional offline CLEAN-OS disk image
on approved external storage for physical-machine recovery (no VM snapshot assumed).
Image must have NO AnyAiCam identity and must not be confused with any old-system backup.

## 5. Isolated portal, simulator and real installer

An independent isolated helper/test host is required and NOT YET IDENTIFIED. It must
not be Dell Hyper-V, Ryzen, production or shared staging. Reserve a private wired
switch/VLAN and non-overlapping addresses after inspection; example Samsung
192.168.253.20/24, helper 192.168.253.10/24. Do not change Dell networking.
The helper serves isolated HTTPS installer-test.test and the prepared synthetic
camera. Provision only after isolation/host placement is confirmed. New SQLite
schema from a1c81e0f4926, new signing/Fernet keys and local TLS CA, scoped test owner,
owned location and approved 8-camera fixture license. No payment/webhook/API calls.
Trust CA only in Samsung/helper and the test browser; exact HTTPS origin, trusted
host, CSRF/secure cookies and all claim protections stay enabled.
Existing portal.env.example and owner-location.plan.json define the scoped setup.
No certificate, account, database, binding or portal is provisioned by this plan.

The complete offline simulator bundle and OFFLINE_SIMULATOR.md provide 251 verified
Ubuntu packages, MediaMTX v1.21.0, FFmpeg 6.1.1 and read-only Python ONVIF. Start only
on the isolated helper; no real/customer camera by default. A real test camera needs
explicit identity/authorization and isolated-network placement before connection.
The VMS installer still uses real online Ubuntu/Docker/Python/model dependencies:
validate isolated egress or helper proxy/cache, without altering source or protections.
No staging/production connections. If dependencies unexpectedly require excluded
systems, stop rather than repoint the appliance or modify those systems.

Exact unchanged unpublished artifact:
anyaicam-appliance-installer-1.1.1-vms-a1c81e0f4926.tar.gz
SHA-256: 9dd0fe3fef6b46752a74ea49b4426cf9030313b926b1795a2e50c68bc48a53ac
36,467,580 bytes; installer/VMS source a1c81e0f492633a80198503e721fc6c277e6a8b3.

Download privately through the isolated licensed-owner portal; verify checksum after
transfer and embedded manifest. Extract a NEW folder, run setup.sh as the normal
Ubuntu desktop user. Technician command, future fresh Samsung only:

    ./setup.sh --portal-url https://installer-test.test --product-mode local

Approve real Ubuntu sudo installation. No mocked installer functions or replacement
OS-install commands. Capture actual download/extract/launch usability, including any
file-manager friction. Installer/validator must complete against actual systemd,
Docker, services and filesystem before browser handoff. Browser sign-in uses the
TEST owner, choose TEST location, review device and explicitly confirm. Existing
possessed-device claim CLI completes enrollment; no code/proof/credential in URLs.
Browser failure retains manual setup-code recovery; do not bypass CSRF, origin,
nonce, expiry, ownership, possession or proof encryption to complete the test.

## 6. Exact clean-machine acceptance evidence

Record each as PASS/FAIL/BLOCKED with versions, timestamps, screenshots and redacted
logs. Hardware/OS install proof is required; existing fixture test results do not count.

- [ ] Positive Samsung hardware/serial/system-disk evidence; Dell/Ryzen excluded.
- [ ] Authenticated ISO, explicit disk erase/install summary, successful physical boot.
- [ ] Clean OS baseline/no prior AnyAiCam state; >=4 CPUs, >=8 GB RAM allowance,
      >=100 GiB root availability; actual network/time/browser/TLS work.
- [ ] Exact archive checksum + embedded source identity/manifest; no rebuild/relabel.
- [ ] Real setup launcher and sudo installation; dependencies succeed without mocks.
- [ ] validate.sh passes actual identity permissions, VMS build/self-test, agent and
      required MediaMTX/firewall/port checks; source/build matches a1c81e0f4926.
- [ ] Real authenticated owner sign-in, owned location selection and explicit confirm.
- [ ] Real device-bound claim start/status/complete, credential/identity persistence
      and enrollment into ONLY the isolated portal/customer/location.
- [ ] Read-only discovery finds synthetic camera's stable EndpointReference UUID;
      changing MessageID does not create duplicate cameras. Add camera, correct RTSP URI.
- [ ] Actual live video decoded in the browser; no screenshot-only mock.
- [ ] Record meaningful video interval, confirm files/metadata, browser playback seek.
- [ ] Basic motion analytics produces an event from moving synthetic pattern;
      do not modify LPR or unrelated analytics pipelines.
- [ ] Reboot Samsung, confirm auto-start/enrollment/identity, live view and recording
      persist; no new duplicate device claim. Record suspend behavior relevant to test.
- [ ] Disable only the test browser handoff, confirm manual code path completes a
      real claim; use isolated disposable test state and respect claim expiry.
- [ ] Wrong owner/site, absent/bad CSRF, wrong origin/nonce, expired/replayed session,
      bad possession secret and absent encryption key refuse; no security bypass.
- [ ] Default uninstall removes software/services and preserves expected identity,
      configuration/recordings; it does not automatically release remote bindings.
- [ ] Supported technician reinstall with retained state; fresh launcher correctly
      refuses existing identity. Validate preserved bindings/data and service restart.
- [ ] Physical clean-install recovery from approved clean-OS image or authenticated
      USB reinstall; fresh identity and new isolated enrollment as expected. Restoring
      a disk image is destructive and must stay on the verified Samsung system disk.
- [ ] Recovery from a failed install and manual claim timeout without corruption.
      Fresh first install has no prior running-image rollback point; any actual
      prior-image rollback needs a separately reviewed valid lab fixture, not a mock.
- [ ] Final defect list and complete evidence ledger; no investor publication/deploy.

## Stop conditions

Offline/ambiguous target, unknown disk, absent independent console/boot path,
USB/backup target ambiguity, insufficient resources/root free space, hardware I/O
errors, absent isolated helper/portal/network, production/staging URL or real-camera
exposure, or any weakened security protection: stop and report. User authorization
covers identified Samsung wipe/reinstall, not an ambiguous disk or another machine.
