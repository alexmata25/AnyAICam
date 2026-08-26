# Installer Artifact Manifest

Branch: `feature/customer-ready-appliance-installer`
Reconstruction status: source, tests, and packaging complete and verified
on the real Ubuntu 24.04 EC2 repo host (`/opt/anyaicam`,
ec2-54-210-36-151.compute-1.amazonaws.com). Not yet installed/run
end-to-end (no disposable validation EC2 has been launched for that, per
standing instruction).

## Tarball

- Filename: `anyaicam-installer-reconstructed-1.0.0.tar.gz`
- **Canonical SHA256 (deterministic build, see below):**
  `301db2d353d161fb047afb86edf690c701be2e6b2e6ddfd82d1c2cb327e05522`
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

**Verified reproducible:** for commit `7c203148ea54e114d2adea8e1782197df97643dd`
(`MTIME_EPOCH=1787728672`), two independent runs of this command from two
separate clean `git archive` extractions produced byte-identical
archives (`cmp` reported no differences) and identical SHA256
(`301db2d353d161fb047afb86edf690c701be2e6b2e6ddfd82d1c2cb327e05522`
both times). Per-file SHA256 of the extracted source in both builds
also matched the table below exactly.

(The same reproducibility method was verified once already for the
prior commit `077e7f5294a7318976aca9e84655db4922d4c369`, hash
`cffa43406cd5aa51179b3eac6070eefea308c0491dcf36e84b5e05576a635e98` --
superseded here only because the source itself changed, see the
Dockerfile-copy fix below, not because the method stopped working.)

**Authoritative integrity check:** the per-file SHA256 table below is
the source-of-truth for verifying installer content; the tarball hash
above is a convenience for verifying a specific packaged build and is
only meaningful when built with the exact command above.

## Per-file SHA256 (of the 14 files packaged into the tarball)

| File | SHA256 | Validation status |
|---|---|---|
| install.sh | `2aab22c7319081d1a284c7a68fea116bddf0cd2f8edf516328cb068f0fc810a2` | bash -n pass, LF-only, mode 0755 |
| 01-preflight.sh | `45e1b0d474f734a8fde43910e7496399f0bc5abaef5652591627175d318ef1d1` | bash -n pass, LF-only, mode 0755 |
| 02-storage-check.sh | `a5446151c5ee9118a188ecc263e535e51aa1e3ec2d94888c429841b67469faa9` | bash -n pass, LF-only, mode 0755; storage-preflight branches covered by 9 mocked tests |
| 03-detect-install.sh | `a309339fb758eaaadfc4c2a0339b65762fb05d126bc4d47413d1700ea5c57cf3` | bash -n pass, LF-only, mode 0755; clean/existing/partial branches covered by 5 mocked tests |
| 04-docker-setup.sh | `401b14d772e021c4ed123144d921ff53ebe6fee6d0b3f9e0128671cbc6ba18bb` | bash -n pass, LF-only, mode 0755 |
| 05-provision-users-dirs.sh | `658a3ed34732b68789d872468e28d6fcbd660dbd9924248d0aa5dbe9b578da62` | bash -n pass, LF-only, mode 0755 |
| 06-deploy-vms.sh | `b14397eac2e5406674e35531cc3d82d04570ae8e6810296f77aef738b8390991` | bash -n pass, LF-only, mode 0755; now copies both Dockerfile and Dockerfile.production (Phase 4 clean-install blocker fix) |
| 07-install-agent.sh | `be0c52fceb4ce960c4172e0421489f98bc47d4169217ae2208d0e9906a3111cb` | bash -n pass, LF-only, mode 0755 |
| 08-systemd-setup.sh | `c452fbcb150bd0881a388cf1ec53fb6f90f232d6011c41decb8e26137483ebb8` | bash -n pass, LF-only, mode 0755 |
| 09-identity.sh | `723d6cb54c384f77f6fd74a8db8285674e0c261411471a6ba3e9f28607312d95` | bash -n pass, LF-only, mode 0755 |
| validate.sh | `b2ff7d070e04cf14fd71b3082a258d304a520829106eb4e601f0e7507915904f` | bash -n pass, LF-only, mode 0755 |
| uninstall.sh | `c3f8635b645803b8bd6dae066f4871587f95310ac586f294d652013a1a3c0631` | bash -n pass, LF-only, mode 0755 |
| README.md | `e5148b0f5ed278bfc80796c2dafe88f3cf678b1294f414af7515ba62a125ea11` | reconstruction/spec documentation, not a script |
| tests/run_tests.sh | `55b32e1d809fc51458823a6e976d413ee5fef04115f4f57a29e8172f1bafdde2` | bash -n pass, LF-only, mode 0755; 17/17 assertions pass |

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

Mocks `id`, `docker`, and `df` as shell functions; redirects every path
constant (`$CONFIG_DIR`, `$VMS_INSTALL_ROOT`, `$VERSION_MARKER`,
`$VMS_SERVICE_FILE`, `$IDENTITY_FILE`, `$REPO_ROOT`) into a disposable
`mktemp -d` tree. Sources the real, unmodified `detect_install_state()`,
`storage_preflight()`, and `deploy_vms()` from `03-detect-install.sh` /
`02-storage-check.sh` / `06-deploy-vms.sh` -- no test-only
reimplementation of the decision logic. Run on the real Ubuntu 24.04
EC2 repo host: 17/17 assertions passed.

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
  mocked, no real image build), asserts both `Dockerfile` and
  `Dockerfile.production` land in `VMS_INSTALL_ROOT` and that the
  `docker compose build` step is invoked -- regression coverage for
  the clean-install release blocker found and fixed in Phase 4
  validation (see below).

## Preservation behavior (verified by code inspection, not yet by a live install/reinstall run)

- Identity (`09-identity.sh`): `identity_provision()` returns
  immediately, logging the existing file's sha256, whenever
  `$IDENTITY_FILE` already exists -- never regenerated on reinstall/repair.
- Config/version marker: same file, same rule.
- Credentials, camera bindings, recordings, customer state
  (`06-deploy-vms.sh`): `rsync -a --update` never overwrites a
  newer-or-equal destination file and never touches destination-only
  files; `.env` is created only if absent, never overwritten.
- `uninstall.sh`: defaults to preserving `$CONFIG_DIR` (`/etc/anyaicam`),
  `/var/lib/anyaicam`, and `/var/log/anyaicam`; only `--purge-all`
  removes them.
- This behavior has not yet been exercised end-to-end against a real
  install/reinstall cycle -- that requires an actual host to install
  onto, which is explicitly deferred (no disposable validation EC2
  launched yet).

## Known fixes found and applied during live disposable-host validation

- **Clean-install blocker (found in Phase 4 clean-install validation on
  a genuine, disposable Ubuntu 24.04 host):** `docker-compose.yml` uses
  `build: .`, which Compose resolves to a file literally named
  `Dockerfile`, but `06-deploy-vms.sh` originally only copied
  `Dockerfile.production`. A from-fresh clean install had nothing for
  `docker compose build` to read and failed outright. Fixed by also
  copying the plain `Dockerfile` -- confirmed against the real
  production instance, where both files already exist side by side and
  the plain `Dockerfile` is the one actually built from. No change to
  `docker-compose.yml`.

## Not yet done (requires the next approval)

- Not yet installed/run end-to-end to a *successful* completion on a
  disposable Ubuntu 24.04 host -- the first attempt hit the blocker
  above; re-validation with the fix is the next step.
- Live reinstall/repair/uninstall preservation behavior not yet
  exercised against a real filesystem (see above).
