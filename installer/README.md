# AnyAiCam Customer-Ready Appliance Installer

## Release-driven installer

This branch no longer treats the VMS application in the surrounding repository
checkout as the install payload. A customer installer must first be built with
`build_release_installer.py` from either:

1. one exact 40-character VMS Git commit, or
2. one release archive whose SHA-256 is supplied and verified.

The exact VMS commit is required in both modes because it is the canonical build
identifier stamped into the appliance and passed to the VMS as
`ANYAICAM_VMS_COMMIT` / `ANYAICAM_BUILD_ID`.

The historical August 24 `e4e0008...` value is documentation only. It is not a
default and is not embedded by the builder.

### Build from an approved Git commit

```bash
python3 installer/build_release_installer.py \
  --vms-commit <40-character-approved-commit> \
  --vms-repo /path/to/AnyAICam \
  --output-dir dist
```

### Build from an approved release archive

```bash
python3 installer/build_release_installer.py \
  --vms-commit <40-character-approved-commit> \
  --release-archive /path/to/vms-release.tar.gz \
  --release-sha256 <sha256-of-that-archive> \
  --output-dir dist
```

An optional non-secret environment template can be added with
`--env-template /path/to/vms.env.template`. Existing `/etc/anyaicam/vms.env`
is never replaced during reinstall/repair; only installer-owned build identity
keys are updated.

## Required VMS release content

The builder requires and packages the exact release copies of:

- `app/` (recursively, including required templates/PWA/static content inside it)
- `requirements.txt`
- `Dockerfile`
- `Dockerfile.production`
- `docker-compose.yml`
- migrations (`migrations/` or `app/db_migrations.py`)
- static assets (`app/static/` or root `static/`)

Root `migrations/`, `static/`, `templates/`, and `systemd/` directories are
included when present. If the VMS release contains
`systemd/anyaicam-vms.service`, that exact unit is installed; otherwise the
versioned installer fallback in `installer/runtime/anyaicam-vms.service` is
used.

The appliance agent is packaged from the exact installer source commit. Runtime
install, repair, and uninstall do not reach back into a Git checkout.

## Secret/state packaging guard

The builder uses a release allowlist rather than archiving the repository root.
That means unrelated files such as a root `aws.env` cannot enter the installer.
It also rejects common private-key/access-token patterns, `.env`, private-key
files, SQLite `.db` files, and backup-like development snapshots inside the
release payload. An environment template is copied only when explicitly
provided and is scanned for the same high-risk secret patterns.

## Runtime layout and preservation

Replaceable software:

- `/opt/anyaicam` — exact mirror of the built VMS payload
- `/opt/anyaicam-agent` — agent venv plus installed source copy
- `/etc/systemd/system/anyaicam-vms.service`

Preserved across reinstall/repair and default uninstall:

- `/etc/anyaicam` — identity, VMS/agent configuration, release marker
- `/var/lib/anyaicam/vms/recordings` — recordings/application state
- `/var/lib/anyaicam/vms/data-config` — protected VMS data/config
- `/var/lib/anyaicam` — agent/customer state and bindings
- `/var/log/anyaicam`

`/var/lib/anyaicam/vms/hls` is regenerable runtime streaming output and is not
treated as protected customer history.

Default uninstall preserves all protected paths above. `--purge-all` removes
them explicitly.

## Preserved installer corrections

- installer shell scripts are LF-only and executable `0755`
- Ubuntu 24.04 `mawk` parser uses `field`, never `index`
- minimum 4 vCPU preflight
- RAM preflight is restored with an 8 GiB floor; the surviving Aug-24 notes do
  not specify a RAM threshold, while the AnyAiCam x86 hardware baseline is
  16 GiB or higher, so final appliance-class acceptance remains a validation
  item rather than being falsely attributed to the historical handoff
- clean install retains the strict 100 GB free-space requirement
- reinstall/repair uses the installation-aware working-space/capacity rule
- Docker, compose, rsync, and Python venv dependencies are installed as needed
- clean Ubuntu 24.04 remains the validation target
- corrected quarantine path remains
  `/var/lib/anyaicam/vms/recordings/quarantine`

## Version identity

`/etc/anyaicam/installed_version` is the installer version.

`/etc/anyaicam/vms_release.json` records the exact VMS commit, verified source
archive SHA-256, installer source commit, and install timestamp.

The runtime environment includes:

```text
ANYAICAM_VMS_COMMIT=<exact-40-character-commit>
ANYAICAM_BUILD_ID=<exact-40-character-commit>
```

The canonical VMS release is still responsible for making `/version` report
that build identity. Disposable-host release validation must fail if `/version`
does not report the approved commit exactly.

## Phase 4 gate

No EC2 validation should run until an approved canonical VMS commit is supplied.
The validation target is one fresh disposable Ubuntu 24.04 `t3.xlarge`, with
synthetic/local mocks only. Production EC2, Ryzen, and Samsung are out of scope.
Samsung clean-install testing remains blocked until explicit user approval
after the disposable validation report.

## Local and Hybrid product setup

The appliance installer (`install.sh`), not the customer portal or the agent's
activation wizard, selects product mode before storage/package/deployment changes.
For a fresh interactive install, answer `local` or `hybrid` when prompted. Blank
and invalid answers fail. Unattended installs must use one of:

```sh
sudo ./install.sh --product-mode=local
sudo ./install.sh --product-mode=hybrid
```

An explicitly supplied `ANYAICAM_PRODUCT_MODE` environment variable also works
(if preserved through sudo); the command-line choice takes precedence. The
installer writes the authoritative value to `/etc/anyaicam/vms.env`, which the
VMS Compose service already loads. Product mode is separate from
`ANYAICAM_ENV`, `ANYAICAM_RUNTIME_ROLE`, and the agent development/production mode.

The installer writes only `ANYAICAM_PRODUCT_MODE` itself -- it never
materializes an individual `ANYAICAM_*_ENABLED` flag. `app/product_mode.py`'s
`resolve_cloud_flag()`/`FLAG_REGISTRY` is the one place that defines what each
mode implies for every cloud-dependent flag it governs (currently
`ANYAICAM_ANALYTICS_SYNC_ENABLED`, `ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED`,
`ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED`, `ANYAICAM_LIVE_RELAY_ENABLED`,
`ANYAICAM_LIVE_P2P_ENABLED`, `ANYAICAM_RECORDING_UPLOAD_ENABLED` -- Local
defaults every one of them off, Hybrid defaults every one of them on), so the
installer and the running application can never disagree about what a mode
means. An explicit per-feature value already present in the environment file
or release template still always wins over the mode, exactly as before. This
does not grant entitlements, change subscriptions, or configure AWS. The agent
remains installed for camera/control-plane operation in both modes; its cloud
activation wizard is unchanged. Local here disables these cloud features by
default, not all network traffic.

### Existing installations and reconciliation

Repair/reinstall reuses a valid saved product mode. A conflicting requested mode,
blank/invalid saved mode, or duplicate mode entries fails before deployment.
Mode values must be unquoted, exact lowercase `local` or `hybrid`.

For an existing or partial installation without the key, the installer leaves
`ANYAICAM_PRODUCT_MODE` completely **unset** rather than assigning a guessed
compatibility label -- `resolve_cloud_flag()`'s own `legacy_default=False`
already reproduces this exact appliance's prior behavior for every governed
flag with no shell-side involvement at all, and leaving the variable itself
unset is what keeps the appliance eligible for a future cloud-driven
entitlement to apply a real mode automatically the first time one is resolved
for it (see `docs/product-mode-local-hybrid-2026-09-21.md`'s own automatic-
restart design) -- an explicit env var here would otherwise permanently block
that. Existing flags are preserved untouched either way. The legacy
`/opt/anyaicam/.env` is inspected before migration too. An explicitly
requested mode on a legacy installation is written normally, the same as a
fresh install. Changing a saved mode is an operator reconciliation task, not
an installer/repair side effect.

Reconciled 2026-09-21 against `app/product_mode.py`'s runtime resolver:
this file originally also materialized four individual flags here (one of
which, `ANYAICAM_CLOUD_UPLOAD_ENABLED`, gates a retired no-op worker on this
branch -- see `app/tests/test_cloud_upload_worker_retired.py`), duplicating
the mode-to-flag mapping in a second place and risking drift from the
runtime's own definition. It now writes only the mode value, described above.

Regression checks (Python plus Bash/coreutils; set TEST_BASH to Git Bash on Windows):

```sh
python -m unittest discover -s installer/tests -v
```

Tests execute configuration functions in temporary directories only. They do not
install packages, start services or connect to a deployment. A real Ubuntu fresh
install/repair smoke test remains a release qualification step -- see
`UBUNTU_VALIDATION_CHECKLIST.md` for the exact, current list of what still
needs disposable-Ubuntu re-validation and why the Windows-host Bash harness
result (11 pre-existing failures, 2 skips, both due to missing Unix tooling/
semantics -- `rsync`, POSIX execute bits -- not a code defect) is not
sufficient for release sign-off on its own.
