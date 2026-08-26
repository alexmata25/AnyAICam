# AnyAiCam Customer-Ready Appliance Installer

## Status: reconstruction in progress

The original `feature/customer-ready-appliance-installer` branch/commit
(`e4e0008ea299452127e21893532922aa597b508c`), built and validated by
Codex on a disposable AWS EC2 instance, was never pushed to this
repository's remote before that instance was terminated during
cleanup. Only the handoff notes describing its validated behavior,
known bugs and fixes, and the one remaining release blocker survived.

This branch is a **reconstruction from that specification**, not a
recovery of the original bytes. See `MANIFEST.md` (added once the
first artifact is built) for exact commit/SHA256/validation-status
tracking going forward, so this can't happen again.

## Known specification (from the Codex handoff notes)

- 12 shell scripts, LF line endings, all executable `0755`.
- Ubuntu 24.04 target.
- A `mawk` incompatibility in the awk field-parsing loop: use `field`
  as the loop variable, never `index` (shadows awk's builtin `index()`
  function under `mawk`, Ubuntu 24.04's default `/usr/bin/awk`).
- Quarantine directory, defined once:
  `/var/lib/anyaicam/vms/recordings/quarantine`.
- Storage preflight: clean installs keep a strict 100GB-free
  requirement; an existing valid install (reinstall/repair) uses an
  installation-aware working-space + total-capacity check instead --
  never the clean-install threshold, never a globally-lowered minimum.
- Partial installs are classified as needing repair, never silently
  treated as clean.
- Configuration, identity, credentials, camera bindings, recordings,
  and customer state are preserved across reinstall/repair. Uninstall
  defaults to preserving all of the above.

## Structure (in progress)

```
install.sh                    Entrypoint + orchestrator
01-preflight.sh                OS/arch/root checks
02-storage-check.sh            Storage preflight (clean vs. existing)
03-detect-install.sh           Existing-install detection (shared)
04-docker-setup.sh             Docker Engine + compose plugin
05-provision-users-dirs.sh     System user, directories, permissions
06-deploy-vms.sh               VMS source, image build, compose config
07-install-agent.sh            Wraps appliance-agent/scripts/install.sh
08-systemd-setup.sh            systemd units, boot-before-login
09-identity.sh                 Identity/config generate-or-preserve
validate.sh                    Post-install validator
uninstall.sh                   Full-stack uninstall with preservation
```
