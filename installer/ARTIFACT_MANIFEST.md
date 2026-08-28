# Installer Artifact Manifest

## Current status — release-driven source ready, canonical VMS release pending

Branch: `feature/release-driven-appliance-installer`

No customer installer artifact is canonical yet. The installer source is prepared
to build a self-contained artifact only after an approved canonical VMS release
is supplied. Until that input exists:

- VMS release commit: **PENDING**
- VMS release archive SHA-256: **PENDING**
- installer artifact filename: **PENDING**
- installer artifact SHA-256: **PENDING**
- disposable Ubuntu 24.04 `t3.xlarge` lifecycle validation: **NOT RUN**
- Samsung clean-install decision: **NO-GO / NOT YET VALIDATED**

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
