# Installer Artifact Manifest

Branch: `feature/customer-ready-appliance-installer`
Reconstruction status: source, tests, and packaging complete and verified
on the real Ubuntu 24.04 EC2 repo host (`/opt/anyaicam`,
ec2-54-210-36-151.compute-1.amazonaws.com). Not yet installed/run
end-to-end (no disposable validation EC2 has been launched for that, per
standing instruction).

## Tarball

- Filename: `anyaicam-installer-reconstructed-1.0.0.tar.gz`
- SHA256 (built from this exact repo working tree on the EC2 host):
  `3bd42682126e9ebe6f59c3ab61af6b52a35e977589be23b1759538cade58e028`
- Not committed to git: this is a build output, not source. Rebuild it
  at any time with `tar --sort=name --owner=0 --group=0 --numeric-owner
  -czf anyaicam-installer-reconstructed-1.0.0.tar.gz install.sh
  01-preflight.sh 02-storage-check.sh 03-detect-install.sh
  04-docker-setup.sh 05-provision-users-dirs.sh 06-deploy-vms.sh
  07-install-agent.sh 08-systemd-setup.sh 09-identity.sh validate.sh
  uninstall.sh README.md tests/run_tests.sh` from `installer/`. If a
  durable copy is wanted, attach it to a GitHub Release or S3 rather
  than committing the binary.

## Per-file SHA256 (of the 14 files packaged into the tarball)

| File | SHA256 | Validation status |
|---|---|---|
| install.sh | `2aab22c7319081d1a284c7a68fea116bddf0cd2f8edf516328cb068f0fc810a2` | bash -n pass, LF-only, mode 0755 |
| 01-preflight.sh | `45e1b0d474f734a8fde43910e7496399f0bc5abaef5652591627175d318ef1d1` | bash -n pass, LF-only, mode 0755 |
| 02-storage-check.sh | `a5446151c5ee9118a188ecc263e535e51aa1e3ec2d94888c429841b67469faa9` | bash -n pass, LF-only, mode 0755; storage-preflight branches covered by 9 mocked tests |
| 03-detect-install.sh | `a309339fb758eaaadfc4c2a0339b65762fb05d126bc4d47413d1700ea5c57cf3` | bash -n pass, LF-only, mode 0755; clean/existing/partial branches covered by 5 mocked tests |
| 04-docker-setup.sh | `401b14d772e021c4ed123144d921ff53ebe6fee6d0b3f9e0128671cbc6ba18bb` | bash -n pass, LF-only, mode 0755 |
| 05-provision-users-dirs.sh | `658a3ed34732b68789d872468e28d6fcbd660dbd9924248d0aa5dbe9b578da62` | bash -n pass, LF-only, mode 0755 |
| 06-deploy-vms.sh | `46957058bcf9b8e481039dad81c38b030b34f94baec31ce933c990f1ac0179cb` | bash -n pass, LF-only, mode 0755 |
| 07-install-agent.sh | `be0c52fceb4ce960c4172e0421489f98bc47d4169217ae2208d0e9906a3111cb` | bash -n pass, LF-only, mode 0755 |
| 08-systemd-setup.sh | `c452fbcb150bd0881a388cf1ec53fb6f90f232d6011c41decb8e26137483ebb8` | bash -n pass, LF-only, mode 0755 |
| 09-identity.sh | `723d6cb54c384f77f6fd74a8db8285674e0c261411471a6ba3e9f28607312d95` | bash -n pass, LF-only, mode 0755 |
| validate.sh | `b2ff7d070e04cf14fd71b3082a258d304a520829106eb4e601f0e7507915904f` | bash -n pass, LF-only, mode 0755 |
| uninstall.sh | `c3f8635b645803b8bd6dae066f4871587f95310ac586f294d652013a1a3c0631` | bash -n pass, LF-only, mode 0755 |
| README.md | `e5148b0f5ed278bfc80796c2dafe88f3cf678b1294f414af7515ba62a125ea11` | reconstruction/spec documentation, not a script |
| tests/run_tests.sh | `b2944273cfbb5631864dd000a771e64f618cad5d84ef9cb2955e684090a3ef85` | bash -n pass, LF-only, mode 0755; 14/14 assertions pass |

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
`$VMS_SERVICE_FILE`, `$IDENTITY_FILE`) into a disposable `mktemp -d`
tree. Sources the real, unmodified `detect_install_state()` and
`storage_preflight()` from `03-detect-install.sh` / `02-storage-check.sh`
-- no test-only reimplementation of the decision logic. Run on the real
Ubuntu 24.04 EC2 repo host: 14/14 assertions passed.

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

## Not yet done (requires the next approval)

- Not installed/run end-to-end on a disposable Ubuntu 24.04 host --
  no such host has been launched, per standing instruction.
- Live reinstall/repair/uninstall preservation behavior not yet
  exercised against a real filesystem (see above).
