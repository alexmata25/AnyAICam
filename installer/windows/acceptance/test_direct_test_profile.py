import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "app"))
import direct_test_profile as profile


@pytest.fixture
def run_id():
    return str(uuid.uuid4())


def document(program_data, run_id):
    return {
        "schema": profile.PROFILE_SCHEMA,
        "run_id": run_id,
        "data_root": str(profile.expected_data_root(program_data, run_id)),
        "service_name": profile._SERVICE_NAME,
        "http_port": profile._HTTP_PORT,
        "approved_camera_network": "192.168.50.0/24",
        "camera_interface_address": "192.168.50.10",
        "camera_interface_network": "192.168.50.0/24",
        "camera_interface_alias": "Ethernet",
    }


def test_profile_resolves_only_its_run_specific_programdata_root(tmp_path, run_id):
    result = profile.validate_profile_document(document(tmp_path / "ProgramData", run_id),
                                               program_data=tmp_path / "ProgramData")
    assert result == tmp_path / "ProgramData" / "AnyAiCam-Test" / run_id


@pytest.mark.parametrize("mutate", [
    lambda d, p: d.update(data_root=str(p / "AnyAiCam")),
    lambda d, p: d.update(data_root=str(p / "Elsewhere" / "AnyAiCam-Test" / d["run_id"])),
    lambda d, p: d.update(service_name="AnyAiCamVMS"),
    lambda d, p: d.update(http_port=8000),
    lambda d, p: d.update(camera_interface_alias=""),
    lambda d, p: d.update(unexpected="value"),
])
def test_profile_rejects_production_paths_and_unexpected_values(tmp_path, run_id, mutate):
    program_data = tmp_path / "ProgramData"
    doc = document(program_data, run_id)
    mutate(doc, program_data)
    with pytest.raises(profile.TestProfileError):
        profile.validate_profile_document(doc, program_data=program_data)


@pytest.mark.parametrize("run_id", ["", "../prod", "00000000-0000-0000-0000-000000000000", "f" * 36])
def test_profile_rejects_bad_run_identifiers(tmp_path, run_id):
    with pytest.raises(profile.TestProfileError):
        profile.expected_data_root(tmp_path / "ProgramData", run_id)


@pytest.mark.parametrize(("network", "interface", "attached"), [
    ("0.0.0.0/0", "192.168.50.10", "192.168.50.0/24"),
    ("8.8.8.0/24", "8.8.8.10", "8.8.8.0/24"),
    ("192.168.50.0/23", "192.168.50.10", "192.168.50.0/23"),
    ("192.168.51.0/24", "192.168.50.10", "192.168.50.0/24"),
    ("192.168.50.0/24", "192.168.51.10", "192.168.50.0/24"),
    ("192.168.50.0/24", "192.168.50.10", "192.168.51.0/24"),
    ("192.168.50.1/24", "192.168.50.10", "192.168.50.0/24"),
])
def test_profile_rejects_unbounded_non_private_or_unattached_camera_network(network, interface, attached, tmp_path, run_id):
    doc = document(tmp_path / "ProgramData", run_id)
    doc["approved_camera_network"] = network
    doc["camera_interface_address"] = interface
    doc["camera_interface_network"] = attached
    with pytest.raises(profile.TestProfileError, match="camera network"):
        profile.validate_profile_document(doc, program_data=tmp_path / "ProgramData")


@pytest.mark.parametrize("requested", [
    "192.168.50.0/24",
    "192.168.50.64/26",
])
def test_camera_scan_accepts_only_networks_inside_the_pinned_subnet(requested):
    assert str(profile.validate_camera_scan_network(
        requested, "192.168.50.0/24", "192.168.50.10", "192.168.50.0/24"
    )) == requested


@pytest.mark.parametrize("requested", [
    "192.168.51.0/24",
    "192.168.0.0/16",
    "8.8.8.0/24",
    "192.168.50.1/24",
])
def test_camera_scan_rejects_networks_outside_pinned_subnet(requested):
    with pytest.raises(profile.TestProfileError, match="camera scan"):
        profile.validate_camera_scan_network(
            requested, "192.168.50.0/24", "192.168.50.10", "192.168.50.0/24"
        )


def test_rtsp_input_source_binding_is_test_only_and_fail_closed(monkeypatch):
    monkeypatch.delenv("ANYAICAM_INTERNAL_TEST_BUILD", raising=False)
    assert profile.rtsp_input_options() == []
    monkeypatch.setenv("ANYAICAM_INTERNAL_TEST_BUILD", "1")
    monkeypatch.setenv("ANYAICAM_DIRECT_TEST_CAMERA_NETWORK", "192.168.50.0/24")
    monkeypatch.setenv("ANYAICAM_DIRECT_TEST_CAMERA_INTERFACE_ADDRESS", "192.168.50.10")
    monkeypatch.setenv("ANYAICAM_DIRECT_TEST_CAMERA_INTERFACE_NETWORK", "192.168.50.0/24")
    assert profile.rtsp_input_options() == ["-localaddr", "192.168.50.10"]
    monkeypatch.setenv("ANYAICAM_DIRECT_TEST_CAMERA_INTERFACE_ADDRESS", "192.168.51.10")
    with pytest.raises(profile.TestProfileError, match="interface is unavailable"):
        profile.rtsp_input_options()


def test_profile_rejects_junction_or_symlink_in_test_root_chain(tmp_path, run_id, monkeypatch):
    program_data = tmp_path / "ProgramData"
    test_base = program_data / profile.TEST_ROOT_NAME
    # Creating a real junction/symlink requires a Windows privilege not present
    # in all test sessions. Exercise the Windows reparse-point predicate without
    # changing host privileges or filesystem policy.
    original = profile._is_reparse
    def reparse_when_test_base(path):
        if profile._canonical(path) == profile._canonical(test_base):
            return True
        return original(path)
    monkeypatch.setattr(profile, "_is_reparse", reparse_when_test_base)
    with pytest.raises(profile.TestProfileError, match="reparse point"):
        profile.validate_profile_document(document(program_data, run_id), program_data=program_data)


def test_test_mode_does_not_fall_back_when_profile_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "_program_data", lambda: tmp_path / "ProgramData")
    monkeypatch.setattr(profile, "_program_files", lambda: tmp_path / "Program Files")
    monkeypatch.setenv("ANYAICAM_INTERNAL_TEST_BUILD", "1")
    install = tmp_path / "Program Files" / "AnyAiCam VMS Direct Test"
    (install / "app").mkdir(parents=True)
    with pytest.raises(profile.TestProfileError, match="missing or unreadable"):
        profile.resolve_from_app_module(install / "app" / "platform_paths.py")


def test_normal_mode_ignores_candidate_profile_without_test_build_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("ANYAICAM_INTERNAL_TEST_BUILD", raising=False)
    assert profile.resolve_from_app_module(tmp_path / "platform_paths.py") is None


def test_test_profile_is_loaded_from_install_root_not_environment_path(tmp_path, monkeypatch, run_id):
    program_data = tmp_path / "ProgramData"
    monkeypatch.setattr(profile, "_program_data", lambda: program_data)
    monkeypatch.setattr(profile, "_program_files", lambda: tmp_path / "Program Files")
    monkeypatch.setenv("ANYAICAM_INTERNAL_TEST_BUILD", "1")
    monkeypatch.setenv("ANYAICAM_TEST_DATA_ROOT", str(program_data / "AnyAiCam"))
    install = tmp_path / "Program Files" / "AnyAiCam VMS Direct Test"
    app = install / "app"
    app.mkdir(parents=True)
    doc = document(program_data, run_id)
    (install / profile.PROFILE_NAME).write_text(json.dumps(doc), encoding="utf-8")
    assert profile.resolve_from_app_module(app / "platform_paths.py") == Path(doc["data_root"])


def test_test_run_id_must_match_the_protected_install_profile(tmp_path, monkeypatch, run_id):
    program_data = tmp_path / "ProgramData"
    monkeypatch.setattr(profile, "_program_data", lambda: program_data)
    monkeypatch.setattr(profile, "_program_files", lambda: tmp_path / "Program Files")
    monkeypatch.setenv("ANYAICAM_INTERNAL_TEST_BUILD", "1")
    monkeypatch.setenv("ANYAICAM_TEST_RUN_ID", str(uuid.uuid4()))
    install = tmp_path / "Program Files" / "AnyAiCam VMS Direct Test"
    app = install / "app"
    app.mkdir(parents=True)
    (install / profile.PROFILE_NAME).write_text(json.dumps(document(program_data, run_id)), encoding="utf-8")
    with pytest.raises(profile.TestProfileError, match="run ID differs"):
        profile.resolve_from_app_module(app / "platform_paths.py")


def test_test_profile_rejects_a_caller_selected_install_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "_program_files", lambda: tmp_path / "Program Files")
    monkeypatch.setenv("ANYAICAM_INTERNAL_TEST_BUILD", "1")
    app = tmp_path / "UserWritable" / "AnyAiCam VMS Direct Test" / "app"
    app.mkdir(parents=True)
    with pytest.raises(profile.TestProfileError, match="fixed protected Program Files path"):
        profile.resolve_from_app_module(app / "platform_paths.py")


