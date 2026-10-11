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

## Optional preloaded VMS image (`--preload-vms-image`, 2026-10-11)

By default the appliance builds the VMS image itself (`docker build` in
`06-deploy-vms.sh` `prepare_vms_image`, before downtime): CPU-heavy, and it
downloads OS/Python packages and model assets (Orange finding N1). A release
built with `--preload-vms-image` instead ships that image already built:

- built on the release machine from exactly `payload/vms` -- the context the
  installer would stage -- with labels `com.anyaicam.vms.release-commit`
  (= `VMS_RELEASE_COMMIT`), `com.anyaicam.vms.payload-sha256` (payload digest:
  SHA-256 over sorted `"<sha256>  <path>
"` lines of every payload file) and
  `com.anyaicam.vms.release-version`; `--platform` defaults to `linux/amd64`;
- `docker save`d as `anyaicam-vms-preload:<commit12>` and compressed to
  `images/anyaicam-vms-<commit12>.tar.zst` (zstd when the `zstd` command or
  Python 3.14+ is available, otherwise `.tar.gz`; `--vms-image-compression`);
- recorded in `release.env` (`VMS_PRELOAD_IMAGE_FILE`, `_SHA256`, `_BYTES`,
  `_UNPACKED_BYTES`, `_COMPRESSION`, `_REF`, `_ID` (image ID as the builder's
  Docker reports it), `_CONFIG_DIGEST`, `_PAYLOAD_SHA256`, `_PLATFORM`) and in
  `release-manifest.json` under `vms_preloaded_image`.

Trust: the archive and `release.env` are both listed in `artifact-files.json`,
which `verify_installer_payload` checks before anything changes; for Software
Update the signed manifest's package SHA-256 covers the whole installer. No
new key or unsigned path. The installer additionally, before downtime:
checks metadata/platform, that the archive is a regular file inside the
installer folder (`ANYAICAM_VMS_IMAGE_TAR` may name another file, but only
inside it), that the staged payload has the recorded payload digest, and free
space (archive copy, plus archive size + extracted layer size in Docker's
storage, + 1 GiB); copies the archive to a root-only
staging folder and checks that copy's SHA-256 BEFORE `docker load`; after
loading, checks the image ID (equal to `_ID` or `_CONFIG_DIGEST`: Docker's
containerd image store reports the OCI index digest, the classic store the
config digest) and both labels; then tags the verified ID as
`anyaicam-vms:release-<commit12>` and skips `docker build`. Any failure is
logged ("Preloaded VMS image not used: ...") and the normal build runs; a
failed check never tags the image for use. `ANYAICAM_VMS_PRELOAD=0` forces the
build. Rollback points and Software Update's own build (apply_release.py) are
unchanged. The archive makes the installer roughly 0.9 GB larger (measured for
1.2.5: 827 MiB archive, 3.4 GB extracted image); zstd barely shrinks it because
the containerd image store saves layers already gzip-compressed.

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
