# Installer Artifact Manifest

## Current status — disposable EC2 lifecycle validation PASSED

Branch: `claude/customer-provisioning-phase1`

- VMS release commit: `019428ebfa897ad2e403d8683834767464806a7d`
- VMS release archive SHA-256: `fc32366069a8b6aebae13ea5e46c3690fb2ab6c64180e54ba3ddf21173402c53`
- installer artifact filename: `anyaicam-appliance-installer-1.1.0-vms-019428ebfa89.tar.gz`
- installer artifact SHA-256: `ff571ae082c0db3e39ab35a804a2d3bbad085297227056c43253e6566e12bbfe`
- disposable Ubuntu 24.04 `t3.xlarge` lifecycle validation (2026-09-10, us-east-1
  sandbox account, instance since terminated): **PASSED** -- clean install, repair/
  reinstall, reboot recovery, service enable/active/restart-safety, and default
  uninstall (protected-state preservation verified byte-identical) all confirmed.
  Two real bugs were found and fixed during this run: `requirements-cpu.txt` was
  missing from `REQUIRED_RELEASE_PATHS`, so no built release could ever complete
  its Docker image build; and `GET /ready` 500'd unconditionally on every call
  (`camera_status()` called with a missing required argument from ~10 call
  sites). Both are fixed and covered by regression tests.
- Samsung clean-install decision: **GO**, pending the user's explicit approval to
  proceed on the physical device. This validation covered install mechanics
  only on a synthetic/unconfigured host -- it did not exercise the staging
  cloud endpoint, activation, check-in, or entitlement refresh, which remain
  to be verified against the real Cloud ID on Samsung itself.

The historical August 24 `e4e0008...` handoff and its old tarball hashes are
retained only as reconstruction history. They are not a default application
release, are not embedded in the release builder, and must not be used as the
VMS version for a new appliance build.

## Release identity model

A built artifact contains:

- the exact VMS payload selected from one 40-character commit or one
  SHA-256-verified release archive;
- `release.env`, which pins the exact VMS commit and installer source commit;
- `release-manifest.json`, which records source hashes and service provenance;
- `artifact-files.json`, which records per-file SHA-256/mode/size;
- the appliance agent from the exact installer source commit;
- the versioned VMS systemd unit.

Runtime install does not read `app/`, Dockerfiles, compose, requirements, agent
code, or service files from a surrounding repository checkout.

## Preserved state

Default reinstall/repair and uninstall preserve:

- `/etc/anyaicam`
- `/var/lib/anyaicam`
- `/var/lib/anyaicam/vms/recordings`
- `/var/lib/anyaicam/vms/data-config`
- `/var/log/anyaicam`

`/opt/anyaicam` and `/opt/anyaicam-agent` are replaceable software. HLS output
is regenerable runtime state.

## Validation gate

After the approved canonical release is supplied, validation is restricted to
one fresh disposable Ubuntu 24.04 `t3.xlarge`. Production EC2, Ryzen, and
Samsung are out of scope. The final report must include the exact installer
source commit, exact VMS release commit, artifact name/SHA-256, LF/mode checks,
full lifecycle results, cleanup confirmation, limitations, and explicit
GO/NO-GO for Samsung. Samsung remains blocked until the user explicitly
approves a GO.
