# Deferred Ubuntu uninstall and recovery instructions

These actions are for the disposable test VM after real installation. None has
been executed on Dell, existing WSL, staging or appliances.

Keep the verified extracted installer folder. Save logs with credentials, setup
codes and claim proofs redacted. Record the exact installer/VMS version.

Default uninstall:

    sudo ./uninstall.sh

The pinned script removes services/software and retains /etc/anyaicam,
/var/lib/anyaicam and /var/log/anyaicam, including identity, bindings and recordings.
Preservation and restart behavior still need real-VM acceptance evidence.
The fresh desktop launcher refuses retained AnyAiCam state. A technician must
use the existing supported installer repair/reinstall path after reviewing
install.sh --help; do not delete identity simply to pass the fresh-install guard.
Do not assume uninstall releases an account's claim or license entitlement.

For a clean retry, restore the disposable VM's pre-install snapshot. This is the
preferred test reset. Purge-all is destructive and must only be separately chosen
on that VM after backing up needed state; it is not a customer default.

A fresh first installation has no previous running image to roll back to.
For an actual prior valid rollback point, review its manifest, then use:

    sudo ./rollback.sh /var/lib/anyaicam/rollback/latest.env

Database restoration is an explicit optional --restore-database action, not the
default. The script keeps the current database before replacement. Confirm the
manifest references an available previous image and valid backups before doing
this test; a synthetic/mock rollback is not real clean-machine evidence.
Use a reviewed previous-release lab fixture only; do not change Software Update.

Acceptance must prove default uninstall preservation, technician reinstall,
clean-snapshot recovery and valid prior-image rollback with actual VM services.
