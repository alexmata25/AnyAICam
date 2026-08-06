# Sprint 2B Verification Report

## Result

Phase 5 Sprint 2B is implemented on `feature/phase5-sprint2b-camera-provisioning`. The focused regression suite passes. No authentication, CSRF, licensing, billing, Cloudflare, or cloud networking behavior was changed.

## Automated verification

Command:

```text
PYTHONPATH=app python -m unittest \
  tests.test_camera_provisioning_health \
  tests.test_edge_camera_discovery \
  tests.test_edge_streaming \
  app.tests.test_appliance_protocol -v
```

Result: **31 tests passed**.

Coverage includes:

- Private Edge URL restrictions, including public, hostname, and loopback rejection.
- RTSP codec, FPS, and bitrate parsing without shell execution.
- ONVIF WS-Security password-digest authentication without plaintext password transmission.
- Persistent secret redaction, including legacy URLs containing user information.
- Friendly-name and provisioning-state preservation across rediscovery.
- Unique camera number validation and persistent configuration.
- HLS freshness, RTSP state, recording state, FPS, bitrate, and latest recording health.
- Sanitized cloud inventory fields.
- Edge-only route registration and persistent configuration integration with the FFmpeg supervisor.
- Existing camera discovery, Edge streaming, and appliance protocol regressions.

Python compilation completed successfully for the changed application modules. `git diff --check` reported no whitespace errors; only the repository's existing line-ending notices were emitted.

A broader `tests/` discovery run executed 49 tests. The Sprint 2B, CSRF, discovery, and streaming tests passed; the run also exposed 10 unrelated failures in existing suites caused by absent partner/customer database migrations and pre-existing exact-source assertions. This branch does not modify the reported security, partner, PWA, or unified-customer modules. The focused 31-test result above is the acceptance result for this change.

## Manual deployment verification

The following checks require a real Edge Appliance and camera and are intentionally not simulated in the unit suite:

1. Confirm `/opt/anyaicam/data/config` is mounted persistently and writable only by the appliance service account.
2. Discover an authorized test camera and open `/edge/camera-provisioning`.
3. Verify correct credentials pass RTSP/ONVIF validation and incorrect credentials fail without being returned in any response or log.
4. Capture a thumbnail and confirm the response is not cached.
5. Provision and restart the container; confirm the friendly name and stream configuration survive.
6. Confirm FFmpeg/HLS uses the provisioned URL and the Live page displays the friendly name.
7. Confirm health changes between healthy, degraded, stale, and offline as RTSP/HLS/recording conditions change.
8. Inspect the cloud request and confirm it contains no IP address, URL, username, or password.

No production deployment or live camera access was performed from this workspace.
