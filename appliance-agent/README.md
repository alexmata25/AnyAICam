# AnyAiCam Ubuntu Appliance Agent

The agent runs independently of live recording. It never uploads camera usernames, passwords, or RTSP URLs. When the portal is unavailable, updates remain in a local SQLite queue and retry with exponential backoff.

## Install on Ubuntu Desktop

```bash
cd appliance-agent
sudo bash scripts/install.sh
sudo -u anyaicam /opt/anyaicam-agent/venv/bin/anyaicam-setup
sudo ./scripts/start.sh
```

The first-run wizard accepts manual Cloud ID/token entry, a pasted value from a USB QR scanner, or a QR image through `zbarimg` when installed. It tests connectivity, activates the appliance, confirms the server-assigned customer/site, discovers cameras, saves protected configuration, and starts the service.

Development settings:

```text
ANYAICAM_AGENT_MODE=development
ANYAICAM_PORTAL_URL=http://100.x.x.x:8000
```

Production settings:

```text
ANYAICAM_AGENT_MODE=production
ANYAICAM_PORTAL_URL=https://portal.example.com
```

Use HTTPS in production. Configuration is stored under `/etc/anyaicam`, credentials and the offline queue under `/var/lib/anyaicam`, and rotating logs under `/var/log/anyaicam`.

Use `sudo bash scripts/start.sh`, `stop.sh`, `restart.sh`, `status.sh`, and `diagnostics.sh`. Uninstall preserves configuration, credentials, recordings, and logs by default. There is no remote-shell feature.
