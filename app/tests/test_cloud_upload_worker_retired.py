"""cloud_upload_worker() is retired in favor of recording_uploader.py.

Before this fix, main.py started two independent, fully automatic
recording-upload pipelines as separate background tasks: the old
cloud_upload_worker() (a flat, non-tenant-scoped scan-and-upload loop
gated by ANYAICAM_CLOUD_UPLOAD_ENABLED) and the new
recording_uploader.recording_upload_worker() (tenant/IAM-scoped,
codec-aware, retention-swept, gated by ANYAICAM_RECORDING_UPLOAD_ENABLED).
If both env vars were ever set at once, the same recordings could be
uploaded to S3 by both systems simultaneously.

cloud_upload_worker() now never scans or uploads, regardless of
ANYAICAM_CLOUD_UPLOAD_ENABLED, so that risk cannot recur even if the
legacy env var is set again by mistake. These tests prove that directly,
rather than just checking the env-var default.
"""
import asyncio

import pytest

import main


def _fail_if_called(*_args, **_kwargs):
    raise AssertionError("cloud_upload_worker() must never scan or upload again")


async def _run_briefly(coro_factory):
    task = asyncio.create_task(coro_factory())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_cloud_upload_worker_never_scans_or_uploads_even_if_legacy_flag_is_enabled(monkeypatch):
    # The legacy flag being True must not matter -- that is exactly the
    # scenario that used to cause a duplicate active uploader.
    monkeypatch.setattr(main, "CLOUD_UPLOAD_ENABLED", True)
    monkeypatch.setattr(main, "scan_recordings_for_cloud_upload", _fail_if_called)
    monkeypatch.setattr(main, "upload_cloud_recording_job", _fail_if_called)
    monkeypatch.setattr(main, "cloud_upload_state", {"worker_status": "unset"})

    asyncio.run(_run_briefly(main.cloud_upload_worker))

    assert main.cloud_upload_state["worker_status"] == "retired_superseded_by_recording_uploader"


def test_cloud_upload_worker_never_scans_or_uploads_when_legacy_flag_is_disabled(monkeypatch):
    # And the "normal" disabled case must behave identically -- there is
    # no code path left that reaches the old scan/upload logic at all.
    monkeypatch.setattr(main, "CLOUD_UPLOAD_ENABLED", False)
    monkeypatch.setattr(main, "scan_recordings_for_cloud_upload", _fail_if_called)
    monkeypatch.setattr(main, "upload_cloud_recording_job", _fail_if_called)
    monkeypatch.setattr(main, "cloud_upload_state", {"worker_status": "unset"})

    asyncio.run(_run_briefly(main.cloud_upload_worker))

    assert main.cloud_upload_state["worker_status"] == "retired_superseded_by_recording_uploader"


def test_cloud_upload_worker_placeholder_still_delegates_to_the_retired_worker(monkeypatch):
    # main()'s startup still creates cloud_upload_task via
    # cloud_upload_worker_placeholder() -- confirm that indirection still
    # reaches the permanently-inert worker rather than silently no-op-ing
    # on its own.
    monkeypatch.setattr(main, "cloud_upload_state", {"worker_status": "unset"})

    asyncio.run(_run_briefly(main.cloud_upload_worker_placeholder))

    assert main.cloud_upload_state["worker_status"] == "retired_superseded_by_recording_uploader"


def test_recording_uploader_is_the_only_worker_started_for_recording_upload(monkeypatch):
    # Belt-and-suspenders: assert main.py's own module source no longer
    # wires cloud_upload_worker() into anything except the retired
    # placeholder, so a future edit can't quietly resurrect a second
    # active call site without this test catching it.
    import inspect

    source = inspect.getsource(main)
    call_sites = [
        line for line in source.splitlines()
        if "cloud_upload_worker(" in line and "def cloud_upload_worker(" not in line
    ]
    assert call_sites == ["    await cloud_upload_worker()"], (
        "cloud_upload_worker() must only ever be called from "
        "cloud_upload_worker_placeholder() -- found: %r" % call_sites
    )
