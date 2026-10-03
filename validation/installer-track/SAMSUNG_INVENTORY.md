# Samsung live read-only inventory — 2026-10-03 (America/Chicago)

Authorized endpoint: alejandro-mata@100.99.177.7. Existing Samsung-specific SSH key
and strict known-host checks used; no Dell SSH-agent/service/config changes.

Positive identity: hostname anyaicam-samsung; DMI vendor SAMSUNG ELECTRONICS CO., LTD.;
model 930MBE; product version P02AHK; architecture x86_64. Not Dell or Ryzen.
DMI chassis serial requires elevated read access and remains unavailable.

OS: Ubuntu 24.04.5 LTS (VERSION_ID=24.04); boot mode UEFI.
CPU: Intel Core i7-8565U, 4 cores / 8 logical processors. RAM MemTotal 16,224,052 KiB
(~15.47 GiB usable), compatible with installer CPU/RAM minimums.

Only physical disk observed: /dev/nvme0n1, 512,110,190,592 bytes, model
SN520 SDAPNUW-512G-1004, serial 193077800308, NVMe.
Persistent system-disk identifier:
/dev/disk/by-id/nvme-SN520_SDAPNUW-512G-1004_193077800308
EFI: nvme0n1p1, 1,127,219,200 bytes, vfat, /boot/efi.
Root: nvme0n1p2, 510,980,521,984 bytes, ext4, /.
Current filesystem available: 447,182,098,432 bytes (~416.47 GiB).
Capacity is sufficient for the planned fresh Ubuntu installation. Actual available
space MUST be rechecked after reinstall: >=107,374,182,400 bytes, not assumed.
No USB/install ISO was observed in the bounded hardware/home inventory.

Networking: wired USB NIC enx00e04c80037c 192.168.0.162/24; Wi-Fi wlo1
192.168.0.160/24; Tailscale 100.99.177.7; default routes via 192.168.0.1.
Current LAN is NOT established as an isolated test network. No interface/routes
changed, multicast discovery sent or cameras contacted.

Existing state: anyaicam system user; VMS, agent and privileged-watcher units.
Agent running; watcher path waiting; VMS service active/exited. Docker container
anyaicam-vms running/healthy with image anyaicam-vms:f5a6d87. /etc/anyaicam,
/opt/anyaicam, /opt/anyaicam-agent, /var/lib/anyaicam and /var/log/anyaicam exist.
No credentials/identity JSON/environment contents or database records read.
No services stopped, backups made, files uploaded/deleted, reboot or erase performed.
User has waived preservation. Wipe loses all existing system-disk OS/files/recordings/
configuration/identity/Docker state and access credentials; remote bindings stay untouched.

Execution blockers: sudo -n fails (password required); no independent physical
console/USB boot or remote management confirmed; no isolated portal/helper host
reserved. SSH access alone is insufficient to safely reinstall the running root disk.
ipmitool/amttool not available; their absence does not prove remote management absent.
Existing mokutil is present; no firmware or Secure Boot setting changed.

Use the unchanged 1.1.1 archive with SHA-256
9dd0fe3fef6b46752a74ea49b4426cf9030313b926b1795a2e50c68bc48a53ac.
Fresh Ubuntu 24.04.4 Desktop amd64 procedure/checklist is in CLEAN_MACHINE_PROCEDURE.md.
Wipe authorization persists; blocked on access/boot prerequisites, not new approval.
