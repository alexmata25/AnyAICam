# Phase 5 Sprint 2A: Edge camera discovery

Camera discovery runs only on an AnyAiCam Edge Appliance or a combined runtime.
AWS does not scan customer networks and does not connect to private camera
addresses.

```text
Private camera LAN
  |-- ONVIF WS-Discovery (UDP 3702)
  |-- bounded TCP readiness checks
  |-- best-effort ONVIF metadata and stream URI requests
  v
Edge discovery service
  |-- manufacturer and model hints
  |-- ONVIF and RTSP capability flags
  |-- local, credential-free stream URLs
  v
/opt/anyaicam/data/config/camera_inventory.json
  |
  | safe summary only
  v
Existing POST /api/appliance/cameras registration endpoint
```

## Safety boundaries

- A scan requires explicit authorization in the request.
- Only RFC1918 IPv4 ranges and IPv6 unique-local ranges are accepted.
- A scan is limited to 256 addresses and 16 ports.
- Tests inject probe implementations and never scan a real network.
- Discovery does not try default passwords or brute-force authentication.
- Usernames, passwords, credentials, and URL user information are removed
  before inventory persistence.
- Private IP addresses, ONVIF service URLs, and RTSP stream URLs remain local.
- The cloud report contains camera ID, name, state, manufacturer, model, and
  protocol flags only. It uses the existing authenticated appliance API.

## Local API

### Discover cameras

`POST /api/edge/cameras/discover`

```json
{
  "network": "192.168.1.0/24",
  "ports": [80, 443, 554, 8000, 8080, 8443, 8554],
  "authorized": true
}
```

The response includes the updated inventory and cloud-report status. If the
appliance registration environment is incomplete, discovery still succeeds and
the cloud report is marked `skipped`.

### Read inventory

`GET /api/edge/cameras/inventory`

### Retry cloud reporting

`POST /api/edge/cameras/inventory/report`

All three endpoints require the existing `manage_settings` permission. Cloud
runtime mode returns a conflict because discovery must occur at the edge.

## Detection behavior

ONVIF WS-Discovery supplies device identity, service addresses, and scopes.
The edge then makes unauthenticated, best-effort ONVIF requests for manufacturer,
model, media profiles, and stream URIs. Cameras that require ONVIF credentials
may expose only scope-based hints. When RTSP is reachable but ONVIF does not
return a stream URI, the inventory records manufacturer-specific candidate URLs
for later verification; candidates are not treated as authenticated streams.

## Configuration

- `ANYAICAM_CONFIG_ROOT` defaults to `/opt/anyaicam/data/config`.
- `ANYAICAM_CLOUD_URL` selects the existing cloud registration service.
- `ANYAICAM_APPLIANCE_API_URL` is used as the existing fallback cloud URL.
- `ANYAICAM_APPLIANCE_ID` identifies the activated appliance.
- `ANYAICAM_APPLIANCE_CREDENTIAL` authenticates outbound inventory reporting.

Docker Compose bind-mounts `./data/config` at the persistent configuration path.
The local `data/` directory is excluded from Git and Docker build context.
