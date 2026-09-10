#!/usr/bin/env bash
# Installs the RDM4 privileged watcher (privileged_watcher.py plus its
# two systemd units) from the bundled appliance-agent/system/ payload.
#
# Confirmed live during edge-appliance validation: appliance-agent/
# system/ (this watcher) was a completely different directory from
# appliance-agent/systemd/ (just anyaicam-agent.service) and was never
# included in build_release_installer.py's packaged paths at all --
# restart_vms/reboot_appliance could never function on a real installed
# appliance, independent of anything in privileged_watcher.py itself
# being correct. See docs/phase1-edge-validation-report.md.
#
# Kept in its own file, as a single pure function with no top-level
# side effects, deliberately separate from scripts/install.sh (which
# runs unconditionally top-to-bottom the moment it's sourced or
# executed, including useradd/venv/pip) -- so this one function can be
# sourced and exercised in isolation by
# appliance-agent/tests/test_privileged_watcher_install.sh without
# ever touching a real system, exactly the same sourcing discipline
# installer/tests/run_tests.sh already uses for the outer installer's
# own functions.

install_privileged_watcher() {
    local source_dir="$1"
    local watcher_src="$source_dir/system/privileged_watcher.py"
    local path_unit_src="$source_dir/system/anyaicam-privileged-watcher.path"
    local service_unit_src="$source_dir/system/anyaicam-privileged-watcher.service"
    # Overridable, real-path-defaulting -- same fixture-redirection
    # convention installer/*.sh's own path constants already use, so
    # appliance-agent/tests/test_privileged_watcher_install.sh can point
    # both at a disposable fixture root instead of the real /opt and
    # /etc/systemd/system.
    local watcher_dir="${PRIVILEGED_WATCHER_INSTALL_DIR:-/opt/anyaicam-agent/privileged}"
    local systemd_dir="${PRIVILEGED_WATCHER_SYSTEMD_DIR:-/etc/systemd/system}"

    # Fail closed, exactly like installer/07-install-agent.sh's own
    # missing-payload check -- never silently skip installing the one
    # component with real root privilege just because it happened not
    # to be present in whatever payload was built.
    local f
    for f in "$watcher_src" "$path_unit_src" "$service_unit_src"; do
        if [[ ! -f "$f" ]]; then
            echo "[ERROR] Missing bundled privileged-watcher file: $f" >&2
            return 1
        fi
    done

    # root:root, 0700, no group/other access at all -- least privilege.
    # Only root (the systemd unit that execs it, itself already
    # necessarily root -- see the .service file's own comment) ever
    # needs to read or run this. Re-running `install` with identical
    # mode/owner/content on every repair is itself the idempotency
    # contract here: never conditional on "does it already exist",
    # since the source of truth is always this release's own bundled
    # copy, the same convention 08-systemd-setup.sh already uses for
    # anyaicam-vms.service.
    install -d -m 0700 -o root -g root "$watcher_dir"
    install -m 0700 -o root -g root "$watcher_src" "$watcher_dir/watcher.py"
    install -m 0644 -o root -g root "$path_unit_src" "$systemd_dir/anyaicam-privileged-watcher.path"
    install -m 0644 -o root -g root "$service_unit_src" "$systemd_dir/anyaicam-privileged-watcher.service"

    systemctl daemon-reload
    # Enable/start the .path unit only. anyaicam-privileged-watcher.
    # service is Type=oneshot with no [Install] section of its own --
    # it must never be enabled for boot directly, only ever triggered
    # by the .path unit when /var/lib/anyaicam/pending_actions goes
    # non-empty (that directory is created and owned by the unprivileged
    # anyaicam-agent process itself, never by this function -- nothing
    # here creates, reads, or writes it). `enable --now` is idempotent:
    # rerunning this on a repair does not restart an already-running
    # watch, does not touch a currently-pending marker, and does not
    # disturb anyaicam-agent.service.
    systemctl enable --now anyaicam-privileged-watcher.path
}
