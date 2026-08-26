# Installer Artifact Manifest

Branch: `feature/customer-ready-appliance-installer`
Reconstruction status: clean install (Phase 4), idempotent reinstall
(Phase 5), and repair (Phase 6) all verified end-to-end on real,
disposable Ubuntu 24.04 EC2 hosts. Phase 7 (default uninstall +
preservation) found and this branch fixed a real data-loss bug (see
below); a corrected Phase 7 re-run is the immediate next step.

## Tarball

- Filename: `anyaicam-installer-reconstructed-1.0.0.tar.gz`
- **Canonical SHA256 (deterministic build, see below):**
  `b87e1b38b3f70023c3f3779cd050a682a26503ef2e46cf1daaac25ced4847b73`
- Not committed to git: this is a build output, not source. If a durable
  copy is wanted, attach it to a GitHub Release or S3 rather than
  committing the binary.

### Deterministic rebuild command

`tar` embeds each packaged file's mtime, and a plain `git checkout`/
working-tree copy does not reproduce a fixed mtime across runs -- two
honest rebuilds of byte-identical source could previously produce
byte-different tarballs (and therefore different SHA256s), even though
nothing in the source had changed. Fixed by building only from a clean
`git archive` extraction (never the working tree) and pinning `tar`'s
mtime to the source commit's own timestamp, so the archive depends only
on the commit being built, not on when or how many times it's rebuilt:

```
COMMIT=<commit to build>
MTIME_EPOCH="$(git show -s --format=%ct "$COMMIT")"
rm -rf /tmp/installer-build && mkdir -p /tmp/installer-build
git archive "$COMMIT" -- installer | tar -x -C /tmp/installer-build
cd /tmp/installer-build/installer
chmod 755 install.sh 01-preflight.sh 02-storage-check.sh 03-detect-install.sh \
    04-docker-setup.sh 05-provision-users-dirs.sh 06-deploy-vms.sh \
    07-install-agent.sh 08-systemd-setup.sh 09-identity.sh validate.sh \
    uninstall.sh tests/run_tests.sh
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$MTIME_EPOCH" \
    -czf anyaicam-installer-reconstructed-1.0.0.tar.gz \
    install.sh 01-preflight.sh 02-storage-check.sh 03-detect-install.sh \
    04-docker-setup.sh 05-provision-users-dirs.sh 06-deploy-vms.sh \
    07-install-agent.sh 08-systemd-setup.sh 09-identity.sh validate.sh \
    uninstall.sh README.md tests/run_tests.sh
```

**Verified reproducible:** for commit `78ad8ffd2e201e114c189c5a54b1458dcb789f28`
(`MTIME_EPOCH=1787736474`), two independent runs of this command from two
separate clean `git archive` extractions produced byte-identical
archives (`cmp` reported no differences) and identical SHA256
(`b87e1b38b3f70023c3f3779cd050a682a26503ef2e46cf1daaac25ced4847b73`
both times). Per-file SHA256 of the extracted source in both builds
also matched the table below exactly.

(The same reproducibility method was verified four times already, for
commits `077e7f5294a7318976aca9e84655db4922d4c369`,
`7c203148ea54e114d2adea8e1782197df97643dd`,
`e9bc3633f11b3d1b3fa486ece5e0adb51e92ad88`, and
`3d2864d25a9dc0e1c2da38add22516d18b0bc335` -- superseded here only
because the source itself changed again, see the persistent-layout fix
below, not because the method stopped working.)

**Authoritative integrity check:** the per-file SHA256 table below is
the source-of-truth for verifying installer content; the tarball hash
above is a convenience for verifying a specific packaged build and is
only meaningful when built with the exact command above.

## Per-file SHA256 (of the 14 files packaged into the tarball)

| File | SHA256 | Validation status |
|---|---|---|
| install.sh | `aee3dfbd7167c1e71ec85671c6a4fc9c959d974ef6fea958d58279c0621eee25` | bash -n pass, LF-only, mode 0755; adds VMS_RECORDINGS_DIR/VMS_DATA_CONFIG_DIR/VMS_HLS_DIR/VMS_ENV_FILE constants (Phase 7 persistent-layout fix) |
| 01-preflight.sh | `45e1b0d474f734a8fde43910e7496399f0bc5abaef5652591627175d318ef1d1` | bash -n pass, LF-only, mode 0755 |
| 02-storage-check.sh | `a5446151c5ee9118a188ecc263e535e51aa1e3ec2d94888c429841b67469faa9` | bash -n pass, LF-only, mode 0755; storage-preflight branches covered by 9 mocked tests |
| 03-detect-install.sh | `a309339fb758eaaadfc4c2a0339b65762fb05d126bc4d47413d1700ea5c57cf3` | bash -n pass, LF-only, mode 0755; clean/existing/partial branches covered by 5 mocked tests |
| 04-docker-setup.sh | `401b14d772e021c4ed123144d921ff53ebe6fee6d0b3f9e0128671cbc6ba18bb` | bash -n pass, LF-only, mode 0755 |
| 05-provision-users-dirs.sh | `b875e2e46b3c41a5bc1aa171618ad2c88da39c47457d64e0b957d38aa1639870` | bash -n pass, LF-only, mode 0755; provisions VMS_DATA_CONFIG_DIR (Phase 7 persistent-layout fix) |
| 06-deploy-vms.sh | `41d80c122ca7919ad8bedc62bd89f9e5f71df8d68d663d485462900ab03159b6` | bash -n pass, LF-only, mode 0755; copies Dockerfile/Dockerfile.production/requirements.txt, migrates legacy persistent data out of $VMS_INSTALL_ROOT before every deploy (Phase 7 persistent-layout fix) |
| 07-install-agent.sh | `741cd11a5d0d6aabdf09006f8d65357023a0c047e8f43f9158fed6db9f2df619` | bash -n pass, LF-only, mode 0755; installs python3.12-venv prerequisite before the wrapped agent installer (Phase 4 clean-install blocker fix) |
| 08-systemd-setup.sh | `c452fbcb150bd0881a388cf1ec53fb6f90f232d6011c41decb8e26137483ebb8` | bash -n pass, LF-only, mode 0755 |
| 09-identity.sh | `723d6cb54c384f77f6fd74a8db8285674e0c261411471a6ba3e9f28607312d95` | bash -n pass, LF-only, mode 0755 |
| validate.sh | `b2ff7d070e04cf14fd71b3082a258d304a520829106eb4e601f0e7507915904f` | bash -n pass, LF-only, mode 0755 |
| uninstall.sh | `88c0e1bccf066d03106d9b73d74696d82222ae32cb4815145d780bcd609656f5` | bash -n pass, LF-only, mode 0755; refactored into a callable run_uninstall() (no behavior change for direct execution) so it's testable |
| README.md | `e5148b0f5ed278bfc80796c2dafe88f3cf678b1294f414af7515ba62a125ea11` | reconstruction/spec documentation, not a script |
| tests/run_tests.sh | `3c41862fd9927f9b327c5d1af0b6ebde8184bf5d722e0c4dd39ba372bdd80de2` | bash -n pass, LF-only, mode 0755; 42/42 assertions pass |

All 14 checksums above were verified identical between the local
reconstruction and the copies committed to this repo, and again
reproduced on the EC2 host directly.

## Known Codex fixes verified present in this reconstruction

- All 12 installer scripts are LF-only (verified via raw byte scan and
  `file(1)` on the real Ubuntu host -- zero `\r` bytes, no CRLF report).
- All 12 installer scripts are mode 0755 on the real Ubuntu host.
- `parse_kv_line()` in `install.sh` uses `field` as its awk for-loop
  variable (never `index`), avoiding the mawk builtin-shadowing bug on
  Ubuntu 24.04's default `/usr/bin/awk`.

## Local/mocked test results (installer/tests/run_tests.sh)

Mocks `id`, `docker`, `df`, `apt-get`, and `systemctl` as shell
functions; redirects every path constant (`$CONFIG_DIR`,
`$VMS_INSTALL_ROOT`, `$VERSION_MARKER`, `$VMS_SERVICE_FILE`,
`$IDENTITY_FILE`, `$REPO_ROOT`, `$VMS_RECORDINGS_DIR`,
`$VMS_DATA_CONFIG_DIR`, `$VMS_ENV_FILE`) into a disposable `mktemp -d`
tree. Sources the real, unmodified `detect_install_state()`,
`storage_preflight()`, `deploy_vms()`, `install_agent()`, and
`run_uninstall()` from `03-detect-install.sh` / `02-storage-check.sh` /
`06-deploy-vms.sh` / `07-install-agent.sh` / `uninstall.sh` -- no
test-only reimplementation of the decision logic. Run on the real
Ubuntu 24.04 EC2 repo host: 42/42 assertions passed.

- `detect_install_state()`: 0/5, 5/5, 2/5, 1/5, and 4/5 marker-presence
  scenarios all resolve to the correct one of clean/existing/partial,
  and partial is never misclassified as clean (covers the
  partial-install -> repair classification requirement).
- `storage_preflight()`: clean-install pass/fail at the 100GB threshold
  (covers the clean-install minimum), existing-install pass/fail at the
  15GB working-space threshold including the exact 98GB-free regression
  case that was the original reported release blocker (covers the
  installation-aware working-space rule), the 90% total-capacity floor
  passing/failing correctly (covers the total-capacity sanity floor),
  and the no-recorded-baseline case skipping the capacity-floor check
  while still enforcing working space.
- `deploy_vms()`: against a fake minimal `REPO_ROOT` fixture (Docker
  mocked, no real image build), asserts `Dockerfile`,
  `Dockerfile.production`, and `requirements.txt` all land in
  `VMS_INSTALL_ROOT` and that the `docker compose build` step is
  invoked -- regression coverage for two of the clean-install release
  blockers found and fixed in Phase 4 validation (see below).
- `install_agent()`: against a fake stand-in for the reused
  `appliance-agent/scripts/install.sh` that deliberately fails unless
  a python3.12-venv marker was already written, asserts the
  prerequisite is apt-get installed AND that it's installed *before*
  the wrapped script runs -- regression coverage for the third
  clean-install release blocker found and fixed in Phase 4 validation.
- `migrate_legacy_persistent_data()` / `migrate_legacy_persistent_file()`:
  moves and verifies legacy data byte-exact into a not-yet-existing
  destination and removes the legacy copy only after that; never
  overwrites data already present at the destination and retains the
  unresolved legacy file rather than dropping it; idempotent on a
  second run (no-op once the legacy source is gone); same three
  guarantees covered separately for the single-file (`.env`) case --
  regression coverage for the Phase 7 persistent-layout fix.
- `run_uninstall()` (default, no `--purge-all`): against a fixture with
  both replaceable software (`$VMS_INSTALL_ROOT`) and persistent data
  (`$VMS_RECORDINGS_DIR`, `$VMS_DATA_CONFIG_DIR`, `$IDENTITY_FILE`)
  populated, asserts the software is removed, the wrapped
  appliance-agent uninstall script ran, and every persistent path
  survives byte-identical -- regression coverage for the Phase 7
  release blocker (default uninstall previously destroyed real
  customer recordings).

## Preservation behavior

- Identity (`09-identity.sh`): `identity_provision()` returns
  immediately, logging the existing file's sha256, whenever
  `$IDENTITY_FILE` already exists -- never regenerated on
  reinstall/repair. **Verified live**: identical appliance ID and
  identity-file hash across a clean install, an idempotent reinstall
  (Phase 5), a repair (Phase 6), and a default uninstall (Phase 7).
- Config/version marker: same file, same rule. Verified live alongside
  identity in the same phases.
- VMS recordings, database, and application state
  (`$VMS_RECORDINGS_DIR`, `/var/lib/anyaicam/vms/recordings`) and VMS
  data-config (`$VMS_DATA_CONFIG_DIR`): as of the Phase 7
  persistent-layout fix, these live outside `$VMS_INSTALL_ROOT`
  entirely, so `uninstall.sh`'s default `rm -rf "$VMS_INSTALL_ROOT"`
  cannot touch them. Pre-fix, this was a confirmed real bug (see
  below) -- default uninstall destroyed live VMS recordings and the
  application's own SQLite database despite its own log message
  claiming they were preserved.
- VMS environment config (`$VMS_ENV_FILE`, `/etc/anyaicam/vms.env`):
  same category, same fix.
- HLS output (`$VMS_HLS_DIR`, `/var/lib/anyaicam/vms/hls`):
  intentionally NOT given the same preservation guarantee -- classified
  as regenerable runtime streaming state, just needed to stop living
  unsafely inside the replaceable software directory.
- `06-deploy-vms.sh`: `rsync -a --update` never overwrites a
  newer-or-equal destination file and never touches destination-only
  files; `.env`/`vms.env` is created only if absent, never overwritten.
- `uninstall.sh`: defaults to preserving `$CONFIG_DIR` (`/etc/anyaicam`),
  `/var/lib/anyaicam`, and `/var/log/anyaicam`; only `--purge-all`
  removes them.

## Known fixes found and applied during live disposable-host validation

- **Clean-install blocker #1 (found in Phase 4 clean-install validation
  on a genuine, disposable Ubuntu 24.04 host):** `docker-compose.yml`
  uses `build: .`, which Compose resolves to a file literally named
  `Dockerfile`, but `06-deploy-vms.sh` originally only copied
  `Dockerfile.production`. A from-fresh clean install had nothing for
  `docker compose build` to read and failed outright. Fixed by also
  copying the plain `Dockerfile` -- confirmed against the real
  production instance, where both files already exist side by side and
  the plain `Dockerfile` is the one actually built from. No change to
  `docker-compose.yml`.
- **Clean-install blocker #2 (found on re-validation with fix #1
  applied):** the plain `Dockerfile` does
  `COPY requirements.txt /tmp/requirements.txt`, and that file is
  tracked only at repo root, not under `app/`. `06-deploy-vms.sh` did
  not copy it, so the build failed on that `COPY` step. Fixed by also
  copying `requirements.txt` -- confirmed the plain Dockerfile
  references no other repo-root files before making this change, and
  confirmed `/opt/anyaicam/requirements.txt` exists in production too.
- **Clean-install blocker #3 (found on re-validation with fixes #1-#2
  applied -- the VMS image build succeeded for the first time here):**
  the reused `appliance-agent/scripts/install.sh` runs
  `python3 -m venv`, which fails on a fresh Ubuntu 24.04 host with
  "ensurepip is not available" because `python3.12-venv` is not
  installed by default. Read that script end-to-end before fixing:
  its only other OS-level touchpoints (`useradd`, `install`, `chmod`,
  `chown`, pip, `systemctl`) are all present on any base Ubuntu
  install. Fixed by installing `python3.12-venv` in the reconstructed
  `07-install-agent.sh` wrapper, before it invokes the reused script
  -- not by patching the reused script itself.
- **Clean-install blocker #4 (found on re-validation with fixes #1-#3
  applied, `install.sh` reached exit 0 for the first time here):**
  `appliance-agent/pyproject.toml` declared `dependencies = []`, but
  `updater/verify.py` unconditionally imports `cryptography`, and that
  import is reachable from the `anyaicam-agent` service entrypoint at
  process start (`service.py` -> `updater.factory` ->
  `updater.state_machine` -> `updater.verify`). Confirmed via a real
  `ModuleNotFoundError: No module named 'cryptography'` when the
  service was explicitly started (not just checked "enabled") on a
  genuine Ubuntu 24.04 host. Before fixing, inventoried every import
  statement in the whole `appliance-agent` package (all indentation
  levels, plus dynamic `__import__()` calls) and confirmed
  `cryptography` is the only undeclared third-party dependency
  anywhere in it. Fixed by adding it to `pyproject.toml`'s
  `dependencies` -- the package manifest, not a workaround in
  `updater/verify.py`. Re-verified live: `ModuleNotFoundError` is gone;
  the agent now reaches real application logic and correctly reports
  `RuntimeError: Appliance is not activated. Run anyaicam-setup first.`
  when started unenrolled -- expected behavior for a clean install with
  no customer enrollment performed, not a failure.
- **Phase 7 blocker (found in live default-uninstall validation, after
  a genuinely clean install + idempotent reinstall + repair all passed
  clean):** `docker-compose.yml` bind-mounted VMS recordings
  (`./recordings`), data-config (`./data/config`), and env (`.env`)
  from paths relative to `$VMS_INSTALL_ROOT`. `uninstall.sh`'s default
  (non-`--purge-all`) path does `rm -rf "$VMS_INSTALL_ROOT"`
  unconditionally -- this silently destroyed a live VMS-recordings
  sentinel file and the real `partner_portal.db` (confirmed by hash
  comparison: present before, `sha256sum: No such file or directory`
  after) despite the uninstall's own log message claiming recordings
  were preserved. Inventoried every runtime-writable/persistent path
  under `/opt/anyaicam` before fixing (see the "Persistent-layout fix"
  section above and the commit history) and confirmed the full list:
  VMS recordings/database/application-state (all children of one
  `RECORDINGS_FOLDER` constant), `data/config` (confirmed unused by
  current app code), `.env`, and HLS output (classified separately as
  regenerable, not protected). Fixed by pointing `docker-compose.yml`
  at absolute paths under `/var/lib/anyaicam/vms/` and `/etc/anyaicam/`
  instead, with an idempotent, verify-before-delete migration path for
  installs that predate the fix.

## Not yet done (requires the next approval)

- Corrected Phase 7 (default uninstall + reinstall-after-uninstall,
  with the persistent-layout fix applied) not yet re-run on a fresh
  disposable host -- next step.
- Phases 8+ (reboot, network-disconnect, synthetic enrollment,
  `--purge-all`) not yet attempted.
