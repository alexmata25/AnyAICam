# AnyAiCam VMS Debian package

Build with `./installer/linux-deb/build-deb.sh`. The Ubuntu 24.04 amd64
package retains the validated Docker Compose runtime. Software under `/opt` is
replaceable; configuration and customer state under `/etc/anyaicam` and
`/var/lib/anyaicam` survive upgrades and normal removal.

The appliance agent is installed and enabled but remains stopped until
`anyaicam-setup` writes its activation credential. Normal removal deletes the
package's exact-version Docker image while retaining configuration, databases,
recordings, and appliance identity. Deliberate data deletion requires
`anyaicam-purge-data --yes-i-understand`.

Run static validation with `test-package.sh`. On a disposable Ubuntu 24.04 host,
run the destructive lifecycle check as root with
`ANYAICAM_DESTRUCTIVE_PACKAGE_TEST=1 test-lifecycle.sh <deb-path>`.
