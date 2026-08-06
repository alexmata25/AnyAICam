# Edge appliance ingestion model

AnyAiCam treats private camera networks as an edge responsibility. An AWS
deployment must not attempt to connect directly to RFC1918 camera addresses.

```text
Private LAN cameras
        |
        | RTSP (LAN only)
        v
AnyAiCam Edge Appliance
  - RTSP readiness probe
  - FFmpeg ingestion and recording
  - HLS generation and freshness monitoring
  - local camera/AI health
        |
        | authenticated outbound communication
        v
AWS services
  - appliance metadata and health
  - events and AI results
  - selected recordings or clips
  - explicitly relayed streams (future)
```

## Runtime boundary

`ANYAICAM_RUNTIME_ROLE=edge` and `combined` may probe camera RTSP endpoints and
launch FFmpeg. The `cloud` role never starts camera ingestion or recording
workers. Camera credentials and private LAN routes remain on the appliance.

## Stream readiness

The appliance reports five states:

- `Connecting`: the worker is starting or has reached the camera and is waiting
  for the first HLS playlist.
- `Live`: both the playlist and newest referenced segment are within the
  configured freshness window.
- `Offline`: the RTSP TCP endpoint is not reachable from the appliance.
- `Stale`: an HLS playlist exists but is no longer advancing.
- `Error`: startup or FFmpeg failed for a reason other than endpoint readiness.

The process being alive is not sufficient to report `Live`. The canonical
client URL is `/static/hls/cameraN.m3u8`, and HLS responses are marked
`no-store` so browsers and intermediary caches do not replay old playlists.

At edge startup, manifests from the previous process are removed before camera
workers launch. Media segments are left to FFmpeg's normal segment-retention
policy; recordings and customer data are not removed.

## Configuration

- `ANYAICAM_HLS_FRESHNESS_SECONDS` (default `15`)
- `ANYAICAM_RTSP_READINESS_TIMEOUT_SECONDS` (default `2`)
- `ANYAICAM_CAMERA_RETRY_SECONDS` (default `10`)
- `CAMERA<N>_PORT` (default `554`)

The camera host, credentials, and RTSP path continue to use the existing
`CAMERA<N>_*` configuration.
