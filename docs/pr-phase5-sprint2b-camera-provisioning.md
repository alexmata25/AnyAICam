# PR Summary — Phase 5 Sprint 2B

## Summary

Adds the Edge Appliance camera provisioning and health layer after discovery. Operators can validate RTSP and ONVIF credentials locally, preview a thumbnail, assign a friendly name and camera number, persist the configuration, monitor media health, and synchronize a sanitized inventory through the existing appliance framework.

## Changes

- Added Edge-only configuration, media probe, provisioning orchestration, and camera health services.
- Added provisioning, validation, thumbnail, health, rename, and synchronization APIs plus an operator UI.
- Integrated persistent camera configuration with the existing FFmpeg supervisor while retaining environment configuration as a fallback.
- Propagated friendly camera names to Live, camera detail, dashboard, and camera status responses.
- Preserved friendly names and provisioning state across rediscovery.
- Extended cloud inventory with sanitized health, FPS, bitrate, and recording state only.
- Added focused regression tests and Sprint 2B architecture/operations documentation.

## Security boundaries

- RTSP, ONVIF, FFmpeg, HLS, thumbnails, recordings, and credentials remain Edge-local.
- Cloud payloads exclude private addresses, URLs, usernames, and passwords.
- Camera endpoints accept only literal private/link-local addresses and reject public, loopback, and hostname targets.
- State-changing endpoints continue to use the existing authentication, permission, and CSRF enforcement.

## Verification

- 31 focused tests passed.
- Changed Python modules compile successfully.
- No production deployment was performed.

## Reviewer checklist

- Confirm persistent config volume ownership and permissions on the target appliance.
- Exercise validation and provisioning against one supported ONVIF/RTSP test camera.
- Inspect a captured cloud inventory request for sanitization.
- Restart the appliance container and confirm persistence and FFmpeg ingestion.

