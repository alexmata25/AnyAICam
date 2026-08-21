# Deployment Log

## 2026-08-21 — Recording pipeline rollout, diagnostics, and controlled end-to-end validation

### Scope

This entry records the real-pilot-appliance rollout and validation sequence for AnyAICam cloud recording. The rollout was intentionally gated with cloud upload disabled except for a short bounded test. The real Ryzen appliance identity, local database, credentials, persisted upload cutoff, recordings, Live View/HLS, and existing settings were preserved throughout.

### Starting safety state

- EC2 control-plane recording upload flag held at `ANYAICAM_RECORDING_UPLOAD_ENABLED=false` except during explicitly bounded tests.
- Ryzen edge-side recording uploader enabled locally so the worker could exercise the fail-closed control-plane path.
- Persisted appliance identity remained `7eb499b6d1`.
- Persisted recording-upload cutoff remained `2026-08-21T06:35:58.709000`.
- Existing local recording backlog was intentionally preserved on disk.

### 1. Backlog-cutoff safety fix

A first real cloud-enable attempt proved the AWS role/trust path and upload/catalog mechanics worked, but it also exposed an unsafe backlog-drain behavior: once enabled, the uploader began processing historical recordings that pre-dated the intended activation point.

The backlog-cutoff safety fix was deployed so the first activation establishes a persistent cutoff and historical recordings are excluded from automatic upload eligibility.

Real-appliance verification showed the historical backlog was excluded across all four cameras while new post-cutoff recordings remained eligible. At one checkpoint the appliance reported approximately 958/1000/1010/1007 completed local files across cameras 1–4 with zero post-cutoff eligible files at that instant. Later, genuine post-cutoff segments such as `camera1_2026-08-21_06-39-28.mkv` were correctly classified as eligible.

The cutoff remained persisted at:

```text
/var/lib/anyaicam/recording_upload_cutoff.json
{"cutoff": "2026-08-21T06:35:58.709000"}
```

### 2. Stalled recording-process watchdog

Cameras 1, 2, and 4 were found in a half-dead RTSP state: their recording ffmpeg processes remained alive and their TCP sessions remained `ESTAB`, but TCP byte counters stopped advancing while Camera 3 continued receiving data. This explained why several recording processes could appear healthy while no new MKV data was arriving.

A recording-mode-only progress watchdog was added and validated. It observes recording-file growth and kills only a demonstrably stalled recording ffmpeg process after the normal segment interval plus grace. Existing supervisor retry/restart logic remains responsible for recovery. Live View/HLS behavior is not changed by the watchdog.

The watchdog candidate passed the full disposable validation suite before deployment. After deployment to the real Ryzen appliance:

- container returned healthy,
- appliance identity and cutoff were unchanged,
- all four recording ffmpeg processes were running,
- all four cameras created and grew fresh recording files,
- all four Live View/HLS feeds produced fresh segments,
- live-relay and recording-upload workers started normally,
- all four cameras completed a real 5-minute rotation and opened the next segment.

### 3. Per-scan upload cap

Because the post-cutoff eligible backlog continued to grow while cloud upload was held disabled, a bounded-test mechanism was added:

```text
ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN
```

Semantics:

- unset/empty: existing behavior, no cap,
- positive integer: exact per-camera cap for each scan,
- zero/negative/non-numeric: fail safe to `1` rather than silently disabling the cap.

The implementation only slices the already cutoff-filtered pending list. It does not move, rename, delete, or alter source recordings and does not modify cutoff, S3, credential, or Live View logic.

The scan-cap test suite passed with the full existing suite, then the cap was deployed to the Ryzen and set to:

```text
ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN=1
```

The value was verified both from the live process environment (`/proc/1/environ`) and from `recording_uploader.MAX_FILES_PER_SCAN`.

### 4. Sparse-scan investigation

A controlled cloud-enable attempt then exposed a separate issue: the recording uploader did not repeat its expected ~30-second scan cycle. The worker logged `worker_started` and began scan 1, but scan 2 never appeared.

Successive diagnostic-only instrumentation was added and validated in disposable environments before each deployment:

1. `recording_upload.scan_tick_begin` heartbeat to prove loop iteration count and timing.
2. `recording_upload.http_call_begin` / `recording_upload.http_call_returned` around the two control-plane `urlopen()` calls.
3. Per-camera dispatch bracketing:
   - `known_camera_numbers_done`
   - `camera_identity_done`
   - `to_thread_dispatch_begin`
   - `relay_camera_once_entered`

The real appliance showed this decisive sequence:

```text
scan_tick_begin scan_number=1
http_call_begin path=/api/appliance/configuration
http_call_returned path=/api/appliance/configuration
known_camera_numbers_done camera=1
camera_identity_done camera=1 found=True
to_thread_dispatch_begin camera=1
```

with no matching `relay_camera_once_entered`.

This proved that `_relay_camera_once()` was queued by `asyncio.to_thread()` but never dispatched to a worker thread.

### 5. Root cause: default ThreadPoolExecutor starvation

Inventorying every `asyncio.to_thread()` call identified 12 long-lived blocking camera tasks consuming the process-wide default executor:

- 8 `process.wait()` calls: four cameras × live+recording modes,
- 4 live-mode stderr-drain loops.

Those calls are intentionally long-lived and can occupy executor workers indefinitely. Short/bounded tasks such as recording-upload scans then queue behind them and can starve.

The fix followed the existing dedicated-executor pattern already used by `live_relay_uploader.py`:

- added `_CAMERA_PROCESS_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="camera_process_wait")`,
- moved long-lived `process.wait()` and live stderr-drain work from `asyncio.to_thread()` to `run_in_executor(_CAMERA_PROCESS_EXECUTOR, ...)`,
- left short/bounded work on the shared default executor.

The dedicated pool is sized for the 12 current long-lived holders plus headroom; these threads are I/O/wait-bound rather than CPU-bound.

Validation commit lineage on `appliance-recording-migration-20260821` included:

- `148e2db185bae912a38d68dc005cbb6e8a37b2fc` — watchdog candidate deployed and verified,
- `4173cb3a58951616d0b6478dfe0984081430240a` — scan-cap candidate,
- `495e8d3b758ff56ae4f6a8947254f85cc900a01f` — scan heartbeat diagnostics,
- `4f0237d6270d8d302b1a2364b16a28b40c3fdd8e` — HTTP bracketing diagnostics,
- `0e949c52a83ba702abc2e6d2b79b8b3c403e58cf` — per-camera dispatch bracketing,
- `5f7c4eda66b12b83b28bba6a6f9aebbbea0a2685` — dedicated camera-process executor candidate (implementation in preceding commit, follow-up test-fixture correction included here).

The executor candidate passed the full disposable validation suite: `156/156` tests, zero regressions.

### 6. Real-appliance executor verification

After deploying the dedicated-executor candidate to the Ryzen appliance, the previously blocked path immediately progressed across all four cameras. With EC2 cloud upload intentionally disabled, all four credential requests reached the control plane and returned the expected `404` fail-closed responses.

Most importantly, scan 2 began roughly 32 seconds after scan 1, and all four cameras again entered `_relay_camera_once()`. This proved the uploader loop was no longer starved and the expected scan cadence had been restored.

### 7. Final bounded cloud-upload test

Before enabling cloud upload, a fresh baseline was recorded:

- EC2 `recordings` table: `48` rows,
- live Ryzen cap: `MAX_FILES_PER_SCAN=1`,
- oldest eligible files:
  - Camera 1: `camera1_2026-08-21_06-39-28.mkv`
  - Camera 2: `camera2_2026-08-21_06-39-27.mkv`
  - Camera 3: `camera3_2026-08-21_06-39-27.mkv`
  - Camera 4: `camera4_2026-08-21_06-39-27.mkv`

Eligible backlog at the baseline was approximately:

- Camera 1: 70
- Camera 2: 70
- Camera 3: 136
- Camera 4: 70

The EC2 recording-upload flag was enabled only for the bounded test. Monitoring was continuous. The target was reached exactly and the flag was immediately returned to `false` and reloaded in the live EC2 process.

Result:

```text
recordings table: 48 -> 52
```

Exactly four recordings were added — one per camera — and no unexpected filename appeared.

The four expected source MKVs were remuxed to MP4 and cataloged with the correct recording start times. All four S3 objects were confirmed present through the real read role, with `video/mp4` content type. Catalog rows used the correct tenant-scoped S3 key prefixes and `status='available'`. Presigned read URLs were successfully generated through the same read-role mechanism used by customer Playback.

This proved the complete real-hardware chain end to end:

```text
completed edge recording
  -> cutoff filtering
  -> capped scan selection
  -> temporary upload credentials
  -> codec-aware remux/staging
  -> S3 upload
  -> catalog row
  -> presigned playback read
```

### Final state after bounded test

- EC2 `ANYAICAM_RECORDING_UPLOAD_ENABLED=false`, confirmed loaded in the live process.
- Ryzen `ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN=1` remains configured.
- No further recording uploads are occurring.
- Appliance identity unchanged.
- Persisted upload cutoff unchanged.
- All four camera recording pipelines and Live View/HLS are healthy.
- Recording watchdog is deployed and verified.
- Recording uploader scan cadence is healthy after the dedicated-executor fix.
- Controlled cloud recording path is proven end to end on real hardware.

### Next checkpoint

Do not broaden cloud enablement or remove the scan cap without a separate approval. The recording pipeline is now in a known-safe, verified state suitable for moving on to the next planned workstream (analytics) while preserving the option for a later, deliberate broader recording rollout.
