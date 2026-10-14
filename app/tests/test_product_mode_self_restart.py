"""A persisted Local<->Hybrid change must actually reach the running VMS
(2026-10-14). The six governed flags are frozen at import, and the cloud's
restart_vms (`docker compose up -d`) leaves the container running when only
product_mode.json changed, so the VMS ends its own process instead and the
compose service's `restart: unless-stopped` starts it with the new mode."""
import asyncio

import pytest

import product_mode as pm

FLAG = "ANYAICAM_LIVE_RELAY_ENABLED"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "STATE_FILE", tmp_path / "product_mode.json")
    monkeypatch.setattr(pm, "_RESOLVED_AT_START", {})
    monkeypatch.setattr(pm, "_UNSUPPORTED_LOGGED", None)
    monkeypatch.setattr(pm, "_DOCKER_MARKER", tmp_path / "no-dockerenv")
    for name in (pm.PRODUCT_MODE_ENV, pm.SELF_RESTART_ENV, FLAG, "ANYAICAM_RECORDING_UPLOAD_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(pm.BOOTSTRAP_MODE_ENV, "local")


def start_process_with_flags(*flags):
    """What each governed module does at import: resolve once."""
    return {flag: pm.resolve_cloud_flag(flag) for flag in flags}


def test_a_local_process_is_stale_once_hybrid_is_persisted():
    assert start_process_with_flags(FLAG) == {FLAG: False}
    assert pm.stale_running_flags() == []
    assert pm.persist_mode("hybrid") is True
    assert pm.stale_running_flags() == [FLAG]


def test_later_resolutions_never_replace_what_the_process_started_with():
    start_process_with_flags(FLAG)
    pm.persist_mode("hybrid")
    assert pm.resolve_cloud_flag(FLAG) is True  # e.g. a status page asking now
    assert pm.stale_running_flags() == [FLAG]


def test_an_explicit_override_is_never_stale(monkeypatch):
    monkeypatch.setenv(FLAG, "false")
    start_process_with_flags(FLAG)
    pm.persist_mode("hybrid")
    assert pm.stale_running_flags() == []


def test_an_administrative_mode_override_keeps_the_process_current(monkeypatch):
    monkeypatch.setenv(pm.PRODUCT_MODE_ENV, "local")
    start_process_with_flags(FLAG)
    pm.persist_mode("hybrid")
    assert pm.stale_running_flags() == []


def test_hybrid_to_local_is_applied_the_same_way():
    pm.persist_mode("hybrid")
    start_process_with_flags(FLAG)
    pm.persist_mode("local")
    assert pm.stale_running_flags() == [FLAG]


def test_the_container_process_is_ended_when_flags_are_stale(monkeypatch):
    monkeypatch.setenv(pm.SELF_RESTART_ENV, "true")
    ended = []
    start_process_with_flags(FLAG, "ANYAICAM_RECORDING_UPLOAD_ENABLED")
    pm.persist_mode("hybrid")
    stale = pm.restart_if_running_flags_stale(terminate=lambda: ended.append(True))
    assert stale == ["ANYAICAM_LIVE_RELAY_ENABLED", "ANYAICAM_RECORDING_UPLOAD_ENABLED"]
    assert ended == [True]


def test_docker_marker_enables_self_restart_by_default(tmp_path, monkeypatch):
    marker = tmp_path / ".dockerenv"
    marker.write_text("")
    monkeypatch.setattr(pm, "_DOCKER_MARKER", marker)
    ended = []
    start_process_with_flags(FLAG)
    pm.persist_mode("hybrid")
    pm.restart_if_running_flags_stale(terminate=lambda: ended.append(True))
    assert ended == [True]


def test_nothing_is_ended_without_a_supervisor_and_it_is_logged_once(caplog):
    ended = []
    start_process_with_flags(FLAG)
    pm.persist_mode("hybrid")
    with caplog.at_level("WARNING", logger="anyaicam.product_mode"):
        for _ in range(3):
            assert pm.restart_if_running_flags_stale(terminate=lambda: ended.append(True)) == [FLAG]
    assert ended == []
    assert sum("self_restart=unavailable" in r.getMessage() for r in caplog.records) == 1


def test_self_restart_can_be_turned_off_inside_a_container(tmp_path, monkeypatch):
    marker = tmp_path / ".dockerenv"
    marker.write_text("")
    monkeypatch.setattr(pm, "_DOCKER_MARKER", marker)
    monkeypatch.setenv(pm.SELF_RESTART_ENV, "false")
    ended = []
    start_process_with_flags(FLAG)
    pm.persist_mode("hybrid")
    pm.restart_if_running_flags_stale(terminate=lambda: ended.append(True))
    assert ended == []


def test_an_unchanged_mode_never_ends_the_process(monkeypatch):
    monkeypatch.setenv(pm.SELF_RESTART_ENV, "true")
    ended = []
    start_process_with_flags(FLAG)
    assert pm.persist_mode("local") is True  # bootstrap local -> persisted local
    assert pm.restart_if_running_flags_stale(terminate=lambda: ended.append(True)) == []
    assert ended == []


def test_the_restarted_process_is_current_so_it_never_loops(monkeypatch):
    monkeypatch.setenv(pm.SELF_RESTART_ENV, "true")
    start_process_with_flags(FLAG)
    pm.persist_mode("hybrid")
    monkeypatch.setattr(pm, "_RESOLVED_AT_START", {})  # the new process
    start_process_with_flags(FLAG)
    ended = []
    assert pm.restart_if_running_flags_stale(terminate=lambda: ended.append(True)) == []
    assert ended == []


def _run_one_worker_cycle(monkeypatch, sync):
    import edge_camera_sync

    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "sync_provisioned_cameras", sync)

    async def stop_after_one_cycle(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(edge_camera_sync.asyncio, "sleep", stop_after_one_cycle)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(edge_camera_sync.camera_configuration_sync_worker())


def test_the_sync_worker_applies_a_mode_its_cycle_persisted(monkeypatch):
    monkeypatch.setenv(pm.SELF_RESTART_ENV, "true")
    ended = []
    monkeypatch.setattr(pm, "_terminate_process", lambda: ended.append(True))
    start_process_with_flags(FLAG)

    def sync():
        return {"status": "ok", "product_mode_restart_required": pm.persist_mode("hybrid")}

    _run_one_worker_cycle(monkeypatch, sync)
    assert ended == [True]


def test_a_cycle_that_fails_after_persisting_the_mode_still_applies_it(monkeypatch):
    monkeypatch.setenv(pm.SELF_RESTART_ENV, "true")
    ended = []
    monkeypatch.setattr(pm, "_terminate_process", lambda: ended.append(True))
    start_process_with_flags(FLAG)

    def sync():
        pm.persist_mode("hybrid")
        raise RuntimeError("capacity apply failed after the mode was written")

    _run_one_worker_cycle(monkeypatch, sync)
    assert ended == [True]
