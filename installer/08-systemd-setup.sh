#!/usr/bin/env bash
# Install the VMS unit bundled in the exact installer artifact.

systemd_setup() {
    local unit_source="$RUNTIME_DIR/anyaicam-vms.service"
    [[ -f "$unit_source" ]] || {
        echo "[ERROR] Missing bundled VMS systemd unit: $unit_source" >&2
        return 1
    }
    log "Installing anyaicam-vms.service..."
    install -m 0644 "$unit_source" "$VMS_SERVICE_FILE"
    systemctl daemon-reload
    systemctl enable anyaicam-vms.service
    systemctl restart anyaicam-vms.service
    log "anyaicam-vms.service enabled for multi-user.target and started."
}

disable_system_suspend() {
    # Confirmed live on Samsung: a genuinely fresh Ubuntu desktop install
    # (GNOME, gnome-settings-daemon) suspended itself mid-validation --
    # LAN, SSH, Tailscale, and the VMS all dropped simultaneously, then
    # came back once someone was physically present to wake it. Root
    # cause: upower reported this appliance on-battery (whether that's a
    # real battery/UPS or an ACPI power-source misdetection), and GNOME's
    # own sleep-inactive-battery-timeout (900s, type 'suspend') put the
    # whole machine to sleep after 15 minutes idle -- an appliance that's
    # meant to run unattended 24/7 must never do this, regardless of
    # which desktop-environment setting or power-source reading is
    # responsible for triggering it.
    #
    # Masking these four systemd targets is the one universal, desktop-
    # environment-agnostic guarantee: it makes suspend/hibernate fail at
    # the systemd level (confirmed live: `systemctl suspend` now returns
    # "Call to Suspend failed: Access denied" instead of actually
    # sleeping), regardless of whether the trigger is GNOME's idle timer,
    # logind's lid-switch/idle handling, a stray ACPI event, or a
    # customer/technician clicking "Suspend" in a desktop menu. This does
    # NOT affect screen blanking/DPMS (a separate mechanism) and does not
    # touch networking. `systemctl mask` is idempotent -- safe to run on
    # every install and every repair.
    log "Disabling system suspend/hibernate (appliance must stay online 24/7)..."
    systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
    log "Suspend/hibernate disabled at the systemd level."
}
