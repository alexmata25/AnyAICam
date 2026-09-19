"""Captures the 2026-09-15 staging analytics-sync fix in source control,
so it's no longer just a manual `docker recreate` on portal-green.

Root cause (confirmed live against anyaicam-staging, deploy-portal:
865e49d): appliance_cloud.py's ANALYTICS_SYNC_ENABLED reads
ANYAICAM_ANALYTICS_SYNC_ENABLED and defaults to false -- when false,
POST /api/appliance/analytics/{camera_id}/events 404s uniformly for
every camera (confirmed identical for camera 1's own id and cameras
2-5's), not just a pilot subset. This was the real blocker for
Playback markers ever seeing real event data; it has nothing to do
with camera discovery/recording, which was already healthy on all 5
Ryzen channels.

Recording upload for Cameras 2-5 stays intentionally pilot-gated --
this fix is scoped to analytics-sync only, a deliberately independent
toggle (see analytics_sync.py's own module docstring). These tests
guard both halves: the staging example file now documents the
approved value, and the recording-upload gates it must NOT touch stay
exactly as they were (absent, i.e. default-false/no pilot widening).

STAGING ONLY, matching this project's own established convention (see
test_hardware_return_policy_finalization.py's identical framing):
deploy/.env.staging.example is the one place this value is set;
production is untouched by this file.
"""

from pathlib import Path

STAGING_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / "deploy" / ".env.staging.example"
PRODUCTION_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / "deploy" / ".env.production.example"
APPLIANCE_CLOUD_SOURCE = Path(__file__).resolve().parents[1] / "appliance_cloud.py"


def _parsed_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def test_staging_example_now_documents_analytics_sync_enabled():
    values = _parsed_env(STAGING_ENV_EXAMPLE)
    assert values.get("ANYAICAM_ANALYTICS_SYNC_ENABLED") == "true"


def test_staging_example_does_not_widen_recording_upload_gates():
    # The whole point of this fix being analytics-only: adding the
    # analytics-sync line must never silently also flip on bulk
    # recording upload or widen the pilot-camera allowlist for
    # Cameras 2-5. Both stay genuinely absent (default false / empty),
    # exactly as they were before this change.
    values = _parsed_env(STAGING_ENV_EXAMPLE)
    assert "ANYAICAM_RECORDING_UPLOAD_ENABLED" not in values
    assert "ANYAICAM_RECORDING_UPLOAD_PILOT_CAMERAS" not in values


def test_production_example_is_untouched_by_this_staging_only_fix():
    values = _parsed_env(PRODUCTION_ENV_EXAMPLE)
    assert "ANYAICAM_ANALYTICS_SYNC_ENABLED" not in values


def test_the_documented_flag_name_genuinely_matches_the_cloud_source():
    # Guards against the env-file comment/value drifting from the real
    # os.getenv() key appliance_cloud.py actually reads -- read as
    # source text, not imported, so this has no import-time coupling to
    # the rest of that module's dependencies.
    source = APPLIANCE_CLOUD_SOURCE.read_text(encoding="utf-8")
    assert "os.getenv('ANYAICAM_ANALYTICS_SYNC_ENABLED','false')" in source
