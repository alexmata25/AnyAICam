"""Windows 1.3.2 (Blue): the VMS reads exactly the files the cloud agent writes.

The VMS (app/) and the agent (appliance-agent) are separate services that share files: the agent writes the cloud
credential, the live-relay commands and the media-fetch secret; the VMS's Hybrid workers read them. On Windows both
locations come from the launchers (ANYAICAM_STATE_DIR = %ProgramData%\\AnyAiCam\\agent for both services) and from
platform_paths (the agent's config_dir = %ProgramData%\\AnyAiCam\\config). Before 1.3.2 the VMS looked in
C:\\var\\lib\\anyaicam and Hybrid silently did nothing. This pins the contract module by module, so a new Hybrid
worker or a moved agent file cannot drift apart again.
"""
import importlib
import os
from pathlib import Path

import pytest

from anyaicam_agent.config import AgentConfig

CREDENTIAL_READERS = ("live_relay_uploader", "recording_uploader", "analytics_sync", "facial_embedding_sync",
                      "local_storage_manager", "webrtc_publisher", "talk_down_discovery", "talk_audio_relay_client")


@pytest.fixture()
def shared_state(tmp_path, monkeypatch):
    """Both services started with the same ANYAICAM_STATE_DIR, as the two Windows launchers do."""
    state = tmp_path / "ProgramData" / "AnyAiCam" / "agent"
    monkeypatch.setenv("ANYAICAM_STATE_DIR", str(state))
    monkeypatch.delenv("ANYAICAM_CREDENTIAL_FILE", raising=False)
    modules = {name: importlib.import_module(name) for name in CREDENTIAL_READERS}
    reloaded = {name: importlib.reload(module) for name, module in modules.items()}
    agent = AgentConfig(config_dir=str(tmp_path / "ProgramData" / "AnyAiCam" / "config"), state_dir=str(state))
    yield reloaded, agent
    monkeypatch.undo()
    for module in modules.values():
        importlib.reload(module)                                        # restore the default module state


def test_every_hybrid_worker_reads_the_credential_the_agent_writes(shared_state):
    modules, agent = shared_state
    for name, module in modules.items():
        assert Path(module.CREDENTIAL_FILE) == agent.credential_file, name


def test_live_relay_commands_are_read_where_the_agent_writes_them(shared_state):
    modules, agent = shared_state
    assert modules["live_relay_uploader"].RELAY_COMMANDS_FILE == agent.live_relay_commands_file


@pytest.mark.skipif(os.name != "nt", reason="the Windows layout")
def test_windows_media_fetch_secret_is_read_where_the_agent_keeps_it(monkeypatch):
    import appliance_media_fetch
    import platform_paths
    monkeypatch.delenv("ANYAICAM_MEDIA_FETCH_SECRET_FILE", raising=False)
    config = platform_paths._windows_program_data() / "AnyAiCam" / "config"   # agent-launcher.ps1 ANYAICAM_CONFIG_DIR
    try:
        reloaded = importlib.reload(appliance_media_fetch)
        assert reloaded.MEDIA_FETCH_SECRET_FILE == AgentConfig(config_dir=str(config)).media_fetch_secret_file
    finally:
        monkeypatch.undo()
        importlib.reload(appliance_media_fetch)


@pytest.mark.skipif(os.name != "nt", reason="the Windows layout")
def test_windows_feature_entitlement_snapshot_lives_in_the_protected_config_folder(monkeypatch):
    """edge_camera_sync stores the cloud's signed feature-entitlement snapshot here; on 1.3.0 it was C:\\opt\\..., where
    any signed-in user could delete it or put back an older, still-valid snapshot."""
    import appliance_entitlements
    import platform_paths
    monkeypatch.delenv("ANYAICAM_FEATURE_ENTITLEMENTS_FILE", raising=False)
    try:
        reloaded = importlib.reload(appliance_entitlements)
        assert reloaded.STATE_FILE == platform_paths._windows_program_data() / "AnyAiCam" / "config" / "feature_entitlements.json"
    finally:
        monkeypatch.undo()
        importlib.reload(appliance_entitlements)
