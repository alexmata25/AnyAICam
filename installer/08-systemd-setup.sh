#!/usr/bin/env bash
# systemd unit for the VMS container + boot-before-login wiring. The
# agent's own unit is already installed by 07-install-agent.sh (via
# appliance-agent/scripts/install.sh) -- this only adds the VMS side,
# using the same WantedBy=multi-user.target pattern already proven by
# anyaicam-agent.service and the RDM4 privileged watcher this session:
# multi-user.target starts before any desktop session, unlike
# graphical.target.

systemd_setup() {
    log "Installing anyaicam-vms.service..."
    {
        echo "[Unit]"
        echo "Description=AnyAiCam VMS (Docker Compose)"
        echo "After=docker.service network-online.target"
        echo "Requires=docker.service"
        echo "Wants=network-online.target"
        echo
        echo "[Service]"
        echo "Type=oneshot"
        echo "RemainAfterExit=yes"
        echo "WorkingDirectory=$VMS_INSTALL_ROOT"
        echo "ExecStart=/usr/bin/docker compose up -d"
        echo "ExecStop=/usr/bin/docker compose down"
        echo "TimeoutStartSec=300"
        echo
        echo "[Install]"
        echo "WantedBy=multi-user.target"
    } > "$VMS_SERVICE_FILE"
    systemctl daemon-reload
    systemctl enable anyaicam-vms.service
    systemctl start anyaicam-vms.service
    log "anyaicam-vms.service enabled (starts at boot, before any desktop login) and started."
}
