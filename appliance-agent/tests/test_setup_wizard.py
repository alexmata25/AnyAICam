"""_upsert_vms_env_key(): setup_wizard.main() calls this after a
successful (re-)activation to write ANYAICAM_CLOUD_URL into vms.env --
the exact env var recording_uploader.py/live_relay_uploader.py/
analytics_sync.py/event_media_uploader.py all read for their own
control-plane calls. These tests prove the one property that matters
most for a file every other installed setting also lives in: touching
this ONE key can never disturb any other line already there.
"""
from pathlib import Path

from anyaicam_agent.config import AgentConfig
from anyaicam_agent.setup_wizard import _upsert_vms_env_key


def _config(tmp_path):
    return AgentConfig(config_dir=str(tmp_path))


def test_creates_file_with_key_when_none_exists(tmp_path):
    config = _config(tmp_path)

    _upsert_vms_env_key(config, "ANYAICAM_CLOUD_URL", "https://portal-staging.anyaicam.com")

    content = (tmp_path / "vms.env").read_text(encoding="utf-8")
    assert content == "ANYAICAM_CLOUD_URL=https://portal-staging.anyaicam.com\n"


def test_preserves_every_unrelated_existing_line(tmp_path):
    config = _config(tmp_path)
    env_file = tmp_path / "vms.env"
    env_file.write_text(
        "ANYAICAM_RUNTIME_ROLE=edge\n"
        "ANYAICAM_ENV=production\n"
        "ANYAICAM_APP_SECRETS=deadbeef\n"
        "ANYAICAM_CAMERA_CREDENTIAL_KEY=abc123\n",
        encoding="utf-8",
    )

    _upsert_vms_env_key(config, "ANYAICAM_CLOUD_URL", "https://portal-staging.anyaicam.com")

    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert "ANYAICAM_RUNTIME_ROLE=edge" in lines
    assert "ANYAICAM_ENV=production" in lines
    assert "ANYAICAM_APP_SECRETS=deadbeef" in lines
    assert "ANYAICAM_CAMERA_CREDENTIAL_KEY=abc123" in lines
    assert "ANYAICAM_CLOUD_URL=https://portal-staging.anyaicam.com" in lines
    assert len(lines) == 5


def test_replaces_rather_than_duplicates_an_existing_value(tmp_path):
    config = _config(tmp_path)
    env_file = tmp_path / "vms.env"
    env_file.write_text(
        "ANYAICAM_RUNTIME_ROLE=edge\n"
        "ANYAICAM_CLOUD_URL=https://old-portal.example.test\n",
        encoding="utf-8",
    )

    _upsert_vms_env_key(config, "ANYAICAM_CLOUD_URL", "https://portal-staging.anyaicam.com")

    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines.count("ANYAICAM_RUNTIME_ROLE=edge") == 1
    assert sum(1 for line in lines if line.startswith("ANYAICAM_CLOUD_URL=")) == 1
    assert "ANYAICAM_CLOUD_URL=https://portal-staging.anyaicam.com" in lines
    assert "ANYAICAM_CLOUD_URL=https://old-portal.example.test" not in lines


def test_creates_config_dir_if_missing(tmp_path):
    config = AgentConfig(config_dir=str(tmp_path / "does" / "not" / "exist" / "yet"))

    _upsert_vms_env_key(config, "ANYAICAM_CLOUD_URL", "https://portal-staging.anyaicam.com")

    assert Path(config.config_dir, "vms.env").is_file()
