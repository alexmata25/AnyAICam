# Current evidence supersedes earlier preparation status

See SAMSUNG_FRESH_OS_LAB_PREFLIGHT.md: fresh physical Ubuntu 24.04.4 verified,
18 helper preflight assertions passed; Samsung installer still pending local sudo/download.

# Samsung physical acceptance evidence — 2026-10-03

## Live status

Wipe authorization persists. Remote steps are paused at the user request while Samsung boots USB and the operator completes installation. Physical console availability and dedicated USB erasure
were confirmed by the user. No system-disk erase, Ubuntu reinstall, AnyAiCam install
or physical application acceptance result has occurred yet.

| Gate | Result | Evidence |
| --- | --- | --- |
| Positive target identity | PASS | anyaicam-samsung; SAMSUNG ELECTRONICS CO., LTD.; model 930MBE/P02AHK |
| CPU/RAM compatibility | PASS | i7-8565U, 4 cores/8 threads; MemTotal 16,224,052 KiB |
| System disk | PASS | NVMe SN520 SDAPNUW-512G-1004, serial 193077800308, 512,110,190,592 bytes; root nvme0n1p2, EFI p1 |
| Current OS/space | INVENTORIED | Ubuntu 24.04.5 LTS, UEFI; approximately 416 GiB free before ISO download; fresh-OS 100 GiB gate still pending |
| Dedicated USB identity | PASS | General UDisk, serial 2408200641348662589024; 31,457,280,000 bytes; USB transport; /dev/disk/by-id/usb-General_UDisk_2408200641348662589024-0:0 |
| Ubuntu signed metadata | PASS | SHA256SUMS.gpg authenticated with Ubuntu CD Image signing fingerprint 843938DF228D22F7B3742BC0D94AA3F0EFE21092 |
| ISO download/checksum | INTERRUPTED / UNCONFIRMED | Ubuntu 24.04.4 Desktop amd64; expected SHA-256 3a4c9877b483ab46d7c3fbe165a0db275e1ae3cfe56a5657e5a47c2f99a99d1e |
| USB image write/readback | UNCONFIRMED | Guarded script opened in Samsung GNOME Terminal; waits for verified ISO, requires local sudo password |
| Fresh OS/old-state absence | NOT RUN | Physical USB boot and installer required |
| Investor installer physical acceptance | NOT RUN | Exact 1.1.1 archive remains unchanged/unpublished |
| Isolated portal/camera host | PROVISIONING PAUSED | Authorized disposable AWS helper created; portal/database/simulator not provisioned |

## Media preparation

Prepared on Samsung only in
/home/alejandro-mata/anyaicam-clean-test-media-20261003.
ISO is downloaded to .partial, hashed against authenticated metadata, and renamed
only after success. ISO becomes inaccessible after the system disk is erased;
write and verify the dedicated USB first.

The writer checks hostname, DMI vendor/model, root partition, NVMe serial, USB
persistent identifier, USB serial, transport and exact capacity. It unmounts only
filesystems on that authorized USB, writes the checked ISO, flushes it, and compares
all ISO bytes against the USB. It NEVER writes the NVMe/system disk.
The administrator password is entered in Samsung's physical terminal, never chat.
bash -n passed for the transferred script. These checks are media preparation,
not proof of Ubuntu installation or VMS functionality.

Existing AnyAiCam services remain untouched during download/media preparation.
Dell host settings/services/networking are unchanged. No excluded systems accessed.

## Recover remote access after fresh Ubuntu installation

Use a new normal desktop sudo user and hostname anyaicam-samsung-clean-test.
Do not restore old AnyAiCam, SSH host identity or Tailscale credentials.
A console operator must establish access on the fresh OS: install openssh-server
from Ubuntu repositories if required and authorize only the existing Samsung
client PUBLIC key for the new user. Keep passwords/private keys out of chat.
Record the fresh SSH host Ed25519 fingerprint at the physical console
(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub).
Verify it independently before connecting. Use a dedicated lab known-hosts file;
do not bypass strict checks or overwrite Dell's global known-hosts file.
Record the fresh LAN endpoint on the isolated lab network. Do not reuse the old
Tailscale IP unless a separate authorized test enrollment has been arranged.

## Acceptance tracking

Use every item in CLEAN_MACHINE_PROCEDURE.md section 6 with actual physical
evidence. Prior WSL/harness/browser results are preliminary and cannot be counted
as successful physical installation, claim, camera or reboot acceptance.

## Interrupted media preparation and operator USB boot

The ISO transfer SSH session closed at approximately 97%; checksum completion,
final ISO rename, USB write and byte readback were NOT received. Reconnect timed
out. User reports Samsung is now booting from USB and explicitly requests all
remote steps pause until fresh Ubuntu installation/network recovery.
No agent command erased the system disk. Operator installation is in progress,
not yet proven complete. Because media verification was interrupted, check the
booted USB Ubuntu version before erase and resolve media provenance afterward.

## Authorized AWS helper — provisioning paused

User explicitly authorized a new disposable EC2 lab, separate from staging,
production, Dell, Ryzen and customer resources. Used only the dedicated
anyaicam-validation profile; default root credentials were not used for mutations.
Created resources are recorded individually in AWS_LAB_RESOURCES.json.
Instance i-0360bac15559416c1 is running, public IP 100.27.41.127, private IP
10.253.77.16, Canonical Ubuntu 24.04 x86_64 AMI ami-0045d7fc2ad003464.
t3.large, standard CPU credits, encrypted 60 GiB gp3 root volume
vol-01a33df1c0a730d2d, delete-on-termination; no IAM instance profile; IMDSv2 required.
Ingress SSH/HTTPS/WireGuard restricted to observed Samsung egress 66.60.254.93/32.
No public RTSP/ONVIF ports. Egress limited to HTTP/HTTPS dependency downloads and
lab DNS; SMTP not permitted. New lab-only SSH private key is stored in an ignored
local dist directory and never printed or committed.
No portal/database/test owner/license/camera simulator deployed. No production,
staging, Stripe, email, S3 or customer credentials copied. No EC2 SSH connection made.
Validation profile lacks ssm:GetParameter (Canonical AMI lookup used public-owner
describe-images instead) and ec2:GetConsoleOutput; authenticated initial SSH host-key
verification is unresolved. Do not bypass host-key checks to proceed.
All remote lab/Samsung actions paused per user request. Instance remains running;
all resources must be removed after validation. No cleanup has been performed.