# Privileged watcher packaging/install fix — report

Closes the one item `docs/phase1-edge-validation-report.md` explicitly
left as "found, NOT fixed" (its finding #4). Commit
`8a83bf30a67b6a7434bed8fac44f16a9095d12ed`, branch
`staging/cloud-integration-repair`, pushed to origin. **Local-only
work: no deployment, no EC2 instance provisioned this round, no
Samsung/staging/production touched.**

## What was wrong

`appliance-agent/system/` (`privileged_watcher.py` plus its two systemd
units) was a completely different directory from `appliance-agent/
systemd/` (only `anyaicam-agent.service`) and was in neither
`build_release_installer.py`'s packaged paths nor installed by any
installer script. `restart_vms`/`reboot_appliance` could not function
on any real installed appliance, independent of the dispatched command
itself (fixed in the previous commit) being correct.

## What changed

- **Packaging:** `build_release_installer.py`'s inline `agent_paths`
  tuple is now the module-level `AGENT_RELEASE_PATHS` constant (testable
  from outside `main()`), with `"appliance-agent/system"` added.
- **Installation:** new `appliance-agent/scripts/lib-privileged-watcher.sh`
  defines `install_privileged_watcher()` -- a single pure function,
  installs the watcher script (`0700`, `root:root` -- least privilege,
  only root's own systemd unit ever needs to read/run it) and both
  systemd units (`0644`), runs `daemon-reload`, and enables/starts only
  the `.path` unit (the oneshot `.service` must never be enabled for
  boot directly). Fails closed if any bundled file is missing. Install
  targets are overridable via two env vars for fixture-root testing,
  defaulting to the real `/opt/anyaicam-agent/privileged` and
  `/etc/systemd/system`. `appliance-agent/scripts/install.sh` sources
  the new file and calls the function after the agent's own systemd
  setup.
- **Hardening:** the `.service` unit gained the safe subset of systemd
  sandboxing (`NoNewPrivileges`, `ProtectHome`, `ProtectHostname`,
  `ProtectClock`, `ProtectKernelTunables`, `ProtectKernelModules`,
  `ProtectKernelLogs`, `RestrictSUIDSGID`, `LockPersonality`,
  `RestrictRealtime`). `ProtectSystem=strict` was considered and
  deliberately **not** added -- it would make the whole filesystem
  read-only except explicit `ReadWritePaths`, which could plausibly
  break the Docker socket or systemd's own dbus access, and there is no
  live target in this pass to verify that assumption against. Flagging
  this as a real, conscious trade-off rather than silently skipping it.
- **A real crash, found by this pass's own new test and fixed:** a
  non-string `type` field (e.g. a JSON list) raised an unhandled
  `TypeError: unhashable type: 'list'` out of `DISPATCH.get()` instead
  of being rejected the same safe way every other unknown type already
  is. Fixed with an `isinstance` check before the lookup. A crash here
  could leave other pending markers unprocessed and the oneshot unit in
  a failed systemd state -- exactly the "unsafe failure behavior" this
  design's own docstring says never happens.

## Tests

- `appliance-agent/tests/test_privileged_watcher_install.sh` (new, 17
  cases, bash, same fixture-root/command-shadowing discipline as
  `installer/tests/run_tests.sh`): fails closed on a missing payload;
  correct least-privilege permissions *requested* (0700 root:root for
  the script/directory, 0644 for units -- the actual on-disk mode
  assertion is skipped on Windows/NTFS, which cannot represent real
  POSIX permission bits, the same documented platform gap
  `test_build_release_installer.py`'s executable-bit tests already
  carry); only the `.path` unit is enabled/started, never the
  `.service`; a repair rerun succeeds again and leaves recordings and
  an already-queued pending-action marker byte-identical.
- `installer/tests/test_build_release_installer.py` (+4): the directory
  is packaged, the sibling `systemd/` directory still is too, the real
  files exist on disk, and the `.service` unit's `ExecStart=` path is
  cross-checked against the installer's own install-target default (two
  facts, in two different files, that nothing previously enforced
  agree).
- `appliance-agent/tests/test_rdm4_privileged_actions.py` (+10):
  malformed markers (JSON array/string at top level, non-string
  type/command_id, empty object, nonexistent file) are all safely
  rejected; a `type` value shaped like a shell command is just an
  unknown type, never interpreted; no `DISPATCH` argv token contains a
  shell metacharacter or shell invocation; `subprocess.run` is never
  called with `shell=True` (a static source-inspection guard); a marker
  supplying its own `argv` field shaped exactly like a real dispatch
  value is still never read.

## Verification performed

- Full regression: **1718 passed, 35 failed (the same pre-existing set,
  unchanged from every prior run this session), 18 skipped.** No new
  regressions.
- Secret scan (private key / AWS access key / GitHub token / Stripe
  live-key patterns) against the full diff and both new files: clean.
- Rebuilt the sanitized installer package locally from this exact commit
  (`dist/anyaicam-appliance-installer-1.1.0-vms-8a83bf30a67b.tar.gz`,
  SHA-256 `72d219dbc76ff35862324b1431e935fbf80d119e391322befa36ad0a5f3157e6`)
  and confirmed by direct archive inspection that `payload/agent/system/`
  now contains `privileged_watcher.py` and both unit files. **Not
  installed or deployed anywhere** -- no EC2 instance was provisioned
  this round, per instruction; this is a local build-and-inspect only.

## What this does NOT claim

Live installation of this fix (does `install_privileged_watcher()`
actually run correctly end-to-end via `sudo ./install.sh` on a real
Ubuntu host, does the `.path` unit actually trigger the `.service` on a
real marker file, does `docker compose up -d` actually get invoked by
the real systemd-triggered watcher) was **not** re-verified live in
this pass -- that requires a disposable instance, and none was
provisioned this round per your instruction. The bash fixture-root
harness proves the function's own logic; it does not prove real systemd
`.path`-unit triggering, which only a live host can. Recommended as the
first thing to check whenever staging validation resumes.
