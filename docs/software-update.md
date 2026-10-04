# Software Update (Settings → System)

Status: implemented on branch `feature/software-update-activation-20261003` (2026-10-03). Not deployed.

## What was wrong

The appliance updater (RDM, `appliance-agent/anyaicam_agent/updater/`) verified and extracted a package into
`/var/lib/anyaicam/updates/versions/<v>/` and switched `updates/current_version.txt`. Nothing reads that
pointer. The VMS runs as Docker Compose from `/opt/anyaicam`, and its compose file bind-mounts
`./app:/app`, so the running container executes whatever is in `/opt/anyaicam/app`. Switching the pointer
never changed the running VMS, the "restart" only restarted the agent, and the health check only probed
the agent. The installer's repair path also rsynced new code into that bind-mounted tree while the VMS was running.

## How it works now

1. **Owner confirms** in Settings → System (`app/software_update.py`). The server checks the account
   owner role, that the appliance is theirs, that the release is the currently published offline-signed
   one, and that it is newer. It write-locks the appliance row (one update at a time), queues one
   `install_update` command naming the exact release (update_id, version, sha256), and records `requested` in
   `appliance_update_results`. Partners and the Admin Portal bridge get 403 for `install_update`.
2. **Agent stages** it (`updater/owner_update.py`, unprivileged): signature, target, platform, architecture,
   newer version, migration flag, free disk, download, SHA-256 and size. It writes manifest, signature,
   package and request to `updates/staged/<update_id>/`, then queues the privileged `apply_release` marker.
   It never extracts, activates or restarts anything. The legacy pointer-flip pipeline is unreachable from
   every command path.
3. **Watcher** (`system/privileged_watcher.py`) runs its one fixed `apply_release` argv
   (`systemd-run --unit=anyaicam-software-update … /usr/bin/python3 -I /opt/anyaicam-agent/privileged/apply_release.py`),
   but only after checking that every file on that path is root-owned and not writable by group or others.
4. **Root applier** (`system/apply_release.py`):
   - Treats every staged byte as untrusted, re-verifies everything with the system `openssl` and the
     root-owned key, and refuses unsafe archives, non-additive migrations, and unexpected data in the live tree.
   - Builds `/opt/anyaicam.next`. While the old release keeps running, it builds the image, backs up the
     database and tags the current image as `rollback-<build>`.
   - Then it stops the VMS, renames `/opt/anyaicam → /opt/anyaicam.previous` and
     `.next → /opt/anyaicam`, retags the image, rewrites only the identity keys in `vms.env`, and starts the VMS.
   - Validation: `/version` must report the release's version AND full build_id; `/static/release-identity.json`
     (served from the bind-mounted tree) must name the same build; `/health` and the `/ready` self-test must pass.
   - Only then does it record the release (root's `installed_release.json`, then `/etc/anyaicam/vms_release.json`).
   - On any failure it restores the previous tree, image and identity, restarts, and validates that the
     previous build is the one serving. If that fails too, it reports `rollback_failed` with recovery details.
5. **Relay**: the agent reports progress and outcome from the root-owned result file to the update ledger,
   which keeps the current state per update and appliance. A final outcome is never overwritten.

## Manual rollback (supported, 2026-10-04)

The automatic rollback above covers an update that fails validation. To go back from a release that *did*
validate (for example 1.2.2 → 1.2.1), an operator runs on the appliance:

```bash
sudo anyaicam-rollback            # restores the newest rollback point; keeps the current database
sudo anyaicam-rollback --restore-database   # also puts back the database copy taken at upgrade time
```

- **Rollback points** live in root's `/var/lib/anyaicam-update/rollback/` (0750). The root applier writes one
  after every validated in-app update, for the release it replaced; the installer writes one before every
  repair/upgrade. Each holds the code archive, the image tag, the database backup, the build, the product version
  and a copy of the installed-release record. The applier keeps its newest three; the installer's are kept.
- **What is restored:** code, image, `ANYAICAM_BUILD_ID`/`ANYAICAM_VMS_COMMIT`/`ANYAICAM_VERSION`, and the
  installed-release record for the agent (`/etc/anyaicam/vms_release.json`) and for root (so the newer release
  can be installed again later). Recordings, settings, credentials and MediaMTX are never touched. The command
  then waits for `/version` to report the restored build and `/health` to pass.
- **Stale points are refused.** A point belongs to the release that replaced it. If the appliance runs any
  other build (neither that release nor the rollback build), restoring it would skip releases, so it stops
  before changing anything (`--allow-stale` overrides). Re-running a point on its own rollback build is allowed.
- **Where the command comes from:** the installer installs `rollback.sh` as `/usr/local/sbin/anyaicam-rollback`.
  An in-app update replaces only `/opt/anyaicam` -- never the agent, the root applier or this command -- so an
  appliance gets these from the installer (install or `--repair`). An appliance whose agent predates this
  change (for example one installed with the 1.2.0 installer) must run the newer installer with `--repair`
  first; that repair also takes a correct rollback point for the release it replaces.
- The older location `/var/lib/anyaicam/rollback/` is read only when nothing newer exists, and only files
  owned by root are used.

## Where things live

| Path | Owner | Contents |
|---|---|---|
| `/etc/anyaicam-update/trusted_signing_key.pem` | root 0644 (dir root 0755) | release-signing **public** key (installer step `12-update-signing-key.sh`) |
| `/var/lib/anyaicam-update/` | root 0755 | `results/`, `work/` (0700), `audit.log`, `installed_release.json` |
| `/opt/anyaicam-agent/` | root 0755 | agent venv; `privileged/` (root 0700) holds the watcher and the applier |
| `/opt/anyaicam{,.next,.previous,.failed}` | root | application trees (code only) |
| `/var/lib/anyaicam`, `/etc/anyaicam` | anyaicam | agent state, VMS data, configuration: never touched by activation |

## Releasing (owner)

1. Generate an RSA key pair offline once. Keep the private key off every server and out of this repository.
2. Build: `installer/build_release_installer.py … --release-version 1.2.0 --update-signing-public-key public.pem`
   (build_id is the exact `--vms-commit`).
3. Sign offline: `installer/sign_update_release.py --installer <tarball> --previous-installer <current release tarball>
   --signing-key <offline private key> --out-dir out/`. It refuses non-additive database changes and a key that
   does not match the one the installer provisions.
4. Publish from inside the cloud portal container, which holds only the public key
   (`ANYAICAM_UPDATE_SIGNING_PUBLIC_KEY_FILE`). Use `--verify-only` first: it runs every check and stores nothing.
   `python publish_update_release.py --manifest manifest.json --signature manifest.sig --package <installer> [--verify-only]`
   The command verifies the signature, SHA-256, size, target and channel before storing anything.
   On staging, the key folder `/etc/anyaicam-staging/update-signing/` (root 0755,
   key 0444). `deploy/staging_bluegreen_cutover.py` mounts it read-only at `/run/secrets/update-signing`.
   Appliances download packages from presigned S3 links, so Software Update has its own storage setting.
   The general `ANYAICAM_STORAGE_BACKEND` (thumbnails, clips, …) is unchanged:
   `ANYAICAM_UPDATE_STORAGE_BACKEND=s3` with bucket `ANYAICAM_UPDATE_S3_BUCKET` (default `ANYAICAM_S3_BUCKET`) and
   region `ANYAICAM_UPDATE_S3_REGION`; objects go under `updates/`. The cloud's AWS role needs
   `s3:GetObject` and `s3:PutObject` on `arn:aws:s3:::<bucket>/updates/*`. The appliance endpoint answers 503
   rather than hand out an undownloadable link: a local backend's `/storage/…` paths are relative.

## Tests

- `appliance-agent/tests/test_release_checks.py`, `test_owner_update_staging.py`,
  `test_software_update_root_applier.py`
- `app/tests/test_software_update_owner.py`, `app/tests/test_appliance_updates_latest.py`
- `installer/tests/test_software_update_installer.py`
- Disposable E2E (throwaway container, no network):
  `appliance-agent/tests/e2e/run_software_update_e2e.sh` (old → new → bad release → automatic rollback → old).
