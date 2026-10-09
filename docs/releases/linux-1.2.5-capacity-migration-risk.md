# Linux 1.2.5 camera-capacity migration risk

The 1.2.5 migration intentionally preserves the last capacity recorded by a
pre-1.2.5 installation when upgrading offline. `installer/15-capacity-fallback.sh`
and Software Update both call the same migration routine, which copies the
legacy unsigned capacity into a root-owned, identity-bound fallback. The
fallback is immutable after creation and is used only until a valid signed
capacity snapshot is accepted. This approved behavior is unchanged by the
remediation branch.

Residual provenance risk: the source value in a 1.2.4 installation was stored
in a file writable by the `anyaicam` service account. If that file was
maliciously raised before the 1.2.5 upgrade, the migration cannot distinguish
that value from the legitimate capacity the old release had been enforcing.
The copied value can therefore preserve an inflated limit while that appliance
is offline, until its next valid signed sync replaces it. Root ownership and
appliance-identity binding protect the migrated copy from later service-user
edits, but cannot establish the provenance of the original unsigned value.

This is a documented residual risk, not a behavior change in this remediation.
Do not alter the accepted migration policy without product/security-owner
approval. A future migration-policy change could fail closed until the cloud
provides a signed snapshot, but that would change offline recording behavior
for upgraded appliances and requires an explicit decision.
