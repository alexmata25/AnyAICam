"""Automatic talk-down capability discovery: tests for
talk_down_discovery.py's ONVIF response parsing, the three-way probe
outcome (supported/unsupported/error), reporting semantics (an error
outcome must never be reported as supported:false), and the discovery
cycle's per-camera isolation.

Pure/mocked tests only -- no real network, no real ONVIF device, no
Docker needed (this module has no main.py dependency at all).
"""

import asyncio
from xml.etree import ElementTree as ET

import pytest

import talk_down_discovery as discovery


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    monkeypatch.setattr(discovery, "_camera_map", {})
    discovery.talk_down_discovery_state = {"worker_status": "disabled", "last_scan_at": None, "last_results": {}}
    yield


def _xml(text: str) -> ET.Element:
    return ET.fromstring(text)


# ------------------------------------------------------------- GetProfiles parsing

AUDIO_CAPABLE_PROFILES = """
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <trt:GetProfilesResponse>
      <trt:Profiles token="Profile_1" fixed="true">
        <tt:Name>MainStream</tt:Name>
        <tt:AudioOutputConfiguration token="AudioOutputConfig_1">
          <tt:Name>AudioOutput</tt:Name>
          <tt:OutputToken>AudioOutput_1</tt:OutputToken>
          <tt:SendPrimacy>www.onvif.org/ver20/HalfDuplex/Server</tt:SendPrimacy>
        </tt:AudioOutputConfiguration>
      </trt:Profiles>
      <trt:Profiles token="Profile_2" fixed="true">
        <tt:Name>SubStream</tt:Name>
      </trt:Profiles>
    </trt:GetProfilesResponse>
  </s:Body>
</s:Envelope>
"""

NO_AUDIO_PROFILES = """
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <trt:GetProfilesResponse>
      <trt:Profiles token="Profile_1" fixed="true">
        <tt:Name>MainStream</tt:Name>
      </trt:Profiles>
    </trt:GetProfilesResponse>
  </s:Body>
</s:Envelope>
"""

# Same content as AUDIO_CAPABLE_PROFILES, but AudioOutputConfiguration is
# bound to the trt (media-wsdl) namespace instead of tt (schema) -- a
# real, observed variance across ONVIF camera vendors.
AUDIO_CAPABLE_PROFILES_ALT_NAMESPACE = """
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl">
  <s:Body>
    <trt:GetProfilesResponse>
      <trt:Profiles token="Profile_1" fixed="true">
        <trt:AudioOutputConfiguration token="AudioOutputConfig_1">
          <trt:OutputToken>AudioOutput_1</trt:OutputToken>
        </trt:AudioOutputConfiguration>
      </trt:Profiles>
    </trt:GetProfilesResponse>
  </s:Body>
</s:Envelope>
"""

AUDIO_OUTPUT_CONFIGURATIONS = """
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <trt:GetAudioOutputConfigurationsResponse>
      <trt:Configurations token="AudioOutputConfig_1">
        <tt:Name>AudioOutput</tt:Name>
        <tt:OutputToken>AudioOutput_1</tt:OutputToken>
        <tt:SendPrimacy>www.onvif.org/ver20/HalfDuplex/Server</tt:SendPrimacy>
      </trt:Configurations>
    </trt:GetAudioOutputConfigurationsResponse>
  </s:Body>
</s:Envelope>
"""


def test_parse_profiles_finds_audio_output_token():
    profiles = discovery._parse_profiles(_xml(AUDIO_CAPABLE_PROFILES))
    audio_profile = next(p for p in profiles if p["profile_token"] == "Profile_1")
    assert audio_profile["audio_output_token"] == "AudioOutputConfig_1"
    other_profile = next(p for p in profiles if p["profile_token"] == "Profile_2")
    assert other_profile["audio_output_token"] is None


def test_parse_profiles_no_audio_output_when_absent():
    profiles = discovery._parse_profiles(_xml(NO_AUDIO_PROFILES))
    assert all(p["audio_output_token"] is None for p in profiles)


def test_parse_profiles_robust_to_alternate_namespace_binding():
    profiles = discovery._parse_profiles(_xml(AUDIO_CAPABLE_PROFILES_ALT_NAMESPACE))
    assert profiles[0]["audio_output_token"] == "AudioOutputConfig_1"


def test_parse_audio_output_configurations_extracts_send_primacy():
    configs = discovery._parse_audio_output_configurations(_xml(AUDIO_OUTPUT_CONFIGURATIONS))
    assert configs["AudioOutputConfig_1"]["send_primacy"] == "www.onvif.org/ver20/HalfDuplex/Server"
    assert configs["AudioOutputConfig_1"]["output_token"] == "AudioOutput_1"


# ------------------------------------------------------------- probe_talk_down_capability outcomes

def test_probe_returns_supported_with_metadata(monkeypatch):
    calls = []

    def fake_soap_call(host, username, password, body, action):
        calls.append(action)
        if "GetAudioOutputConfigurations" in action:
            return _xml(AUDIO_OUTPUT_CONFIGURATIONS)
        return _xml(AUDIO_CAPABLE_PROFILES)

    monkeypatch.setattr(discovery, "_soap_call", fake_soap_call)
    result = discovery.probe_talk_down_capability("host", "user", "pass")
    assert result["outcome"] == "supported"
    assert result["metadata"]["profile_token"] == "Profile_1"
    assert result["metadata"]["audio_output_token"] == "AudioOutputConfig_1"
    assert result["metadata"]["send_primacy"] == "www.onvif.org/ver20/HalfDuplex/Server"
    assert len(calls) == 2  # GetProfiles, then the enrichment call


def test_probe_returns_unsupported_when_no_audio_output_and_skips_enrichment(monkeypatch):
    calls = []

    def fake_soap_call(host, username, password, body, action):
        calls.append(action)
        return _xml(NO_AUDIO_PROFILES)

    monkeypatch.setattr(discovery, "_soap_call", fake_soap_call)
    result = discovery.probe_talk_down_capability("host", "user", "pass")
    assert result == {"outcome": "unsupported", "metadata": None}
    assert len(calls) == 1  # never bothers calling GetAudioOutputConfigurations


def test_probe_returns_error_on_get_profiles_network_failure(monkeypatch):
    def fake_soap_call(*args, **kwargs):
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(discovery, "_soap_call", fake_soap_call)
    result = discovery.probe_talk_down_capability("host", "user", "pass")
    assert result["outcome"] == "error"
    assert result["metadata"] is None
    assert "TimeoutError" in result["error_reason"]


def test_probe_returns_error_on_unparseable_get_profiles_response(monkeypatch):
    def fake_soap_call(host, username, password, body, action):
        return "not an xml element"  # will break _parse_profiles

    monkeypatch.setattr(discovery, "_soap_call", fake_soap_call)
    result = discovery.probe_talk_down_capability("host", "user", "pass")
    assert result["outcome"] == "error"
    assert result["metadata"] is None


def test_probe_stays_supported_when_enrichment_call_fails(monkeypatch):
    def fake_soap_call(host, username, password, body, action):
        if "GetAudioOutputConfigurations" in action:
            raise ConnectionResetError("reset")
        return _xml(AUDIO_CAPABLE_PROFILES)

    monkeypatch.setattr(discovery, "_soap_call", fake_soap_call)
    result = discovery.probe_talk_down_capability("host", "user", "pass")
    # A failed enrichment call must never downgrade an already-confirmed
    # "supported" outcome to "error" -- the core finding (audio output
    # token present) already came from a successfully parsed GetProfiles.
    assert result["outcome"] == "supported"
    assert result["metadata"]["audio_output_token"] == "AudioOutputConfig_1"
    assert "send_primacy" not in result["metadata"]  # enrichment genuinely didn't happen, but that's not an error


# ------------------------------------------------------------- reporting: error must never become supported:false

def test_report_capability_never_posts_for_error_outcome(monkeypatch):
    calls = []
    monkeypatch.setattr(discovery, "_control_plane_post", lambda path, payload: calls.append((path, payload)) or None)
    reported = discovery._report_capability("cam-1", {"outcome": "error", "metadata": None, "error_reason": "timeout"})
    assert reported is False
    assert calls == []  # no POST at all -- the cloud's existing value (including "never verified") is left completely alone


def test_report_capability_posts_supported_true_with_metadata(monkeypatch):
    calls = []
    monkeypatch.setattr(discovery, "_control_plane_post", lambda path, payload: calls.append((path, payload)) or {"status": "accepted"})
    reported = discovery._report_capability("cam-1", {"outcome": "supported", "metadata": {"audio_output_token": "AudioOutput_1"}})
    assert reported is True
    assert len(calls) == 1
    path, payload = calls[0]
    assert path == "/api/appliance/cameras"
    camera_item = payload["cameras"][0]
    assert camera_item["id"] == "cam-1"
    assert camera_item["talk_down"]["supported"] is True
    assert camera_item["talk_down"]["metadata"]["audio_output_token"] == "AudioOutput_1"


def test_report_capability_posts_supported_false_with_no_metadata_key(monkeypatch):
    calls = []
    monkeypatch.setattr(discovery, "_control_plane_post", lambda path, payload: calls.append((path, payload)) or {"status": "accepted"})
    discovery._report_capability("cam-1", {"outcome": "unsupported", "metadata": None})
    _, payload = calls[0]
    talk_down = payload["cameras"][0]["talk_down"]
    assert talk_down == {"supported": False}  # no stray "metadata": None key


# ------------------------------------------------------------- discovery cycle: per-camera isolation

def test_run_discovery_cycle_skips_camera_with_no_credentials_configured(monkeypatch):
    monkeypatch.setattr(discovery, "_camera_map", {1: {"camera_id": "cam-1", "site_id": "site-1"}})
    monkeypatch.delenv("CAMERA1_HOST", raising=False)
    probed = []
    monkeypatch.setattr(discovery, "probe_talk_down_capability", lambda *a: probed.append(a) or {"outcome": "supported", "metadata": {}})
    results = discovery._run_discovery_cycle()
    assert results[1] == "not_configured"
    assert probed == []


def test_run_discovery_cycle_reports_each_camera_independently(monkeypatch):
    monkeypatch.setattr(discovery, "_camera_map", {
        1: {"camera_id": "cam-1", "site_id": "site-1"},
        2: {"camera_id": "cam-2", "site_id": "site-1"},
    })
    monkeypatch.setenv("CAMERA1_HOST", "10.0.0.1")
    monkeypatch.setenv("CAMERA1_USERNAME", "user1")
    monkeypatch.setenv("CAMERA1_PASSWORD", "pass1")
    monkeypatch.setenv("CAMERA2_HOST", "10.0.0.2")
    monkeypatch.setenv("CAMERA2_USERNAME", "user2")
    monkeypatch.setenv("CAMERA2_PASSWORD", "pass2")

    def fake_probe(host, username, password):
        if host == "10.0.0.1":
            return {"outcome": "supported", "metadata": {"audio_output_token": "AudioOutput_1"}}
        return {"outcome": "unsupported", "metadata": None}

    monkeypatch.setattr(discovery, "probe_talk_down_capability", fake_probe)
    reported = []
    monkeypatch.setattr(discovery, "_report_capability", lambda camera_id, result: reported.append((camera_id, result["outcome"])) or True)

    results = discovery._run_discovery_cycle()
    assert results == {1: "supported", 2: "unsupported"}
    assert ("cam-1", "supported") in reported
    assert ("cam-2", "unsupported") in reported


def test_run_discovery_cycle_does_not_report_a_camera_with_an_error_outcome(monkeypatch):
    monkeypatch.setattr(discovery, "_camera_map", {
        1: {"camera_id": "cam-1", "site_id": "site-1"},
        2: {"camera_id": "cam-2", "site_id": "site-1"},
    })
    monkeypatch.setenv("CAMERA1_HOST", "10.0.0.1")
    monkeypatch.setenv("CAMERA1_USERNAME", "user1")
    monkeypatch.setenv("CAMERA1_PASSWORD", "pass1")
    monkeypatch.setenv("CAMERA2_HOST", "10.0.0.2")
    monkeypatch.setenv("CAMERA2_USERNAME", "user2")
    monkeypatch.setenv("CAMERA2_PASSWORD", "pass2")

    def fake_probe(host, username, password):
        if host == "10.0.0.1":
            return {"outcome": "error", "metadata": None, "error_reason": "timeout"}
        return {"outcome": "supported", "metadata": {}}

    monkeypatch.setattr(discovery, "probe_talk_down_capability", fake_probe)
    reported = []
    monkeypatch.setattr(discovery, "_report_capability", lambda camera_id, result: reported.append(camera_id) or True)

    results = discovery._run_discovery_cycle()
    assert results == {1: "error", 2: "supported"}
    assert reported == ["cam-2"]  # camera 1's error never triggers a report; camera 2 is entirely unaffected by it


# ------------------------------------------------------------- worker gating

@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_worker_does_nothing_while_disabled(monkeypatch):
    monkeypatch.setattr(discovery, "TALK_DOWN_DISCOVERY_ENABLED", False)
    calls = []
    monkeypatch.setattr(discovery, "_run_discovery_cycle", lambda: calls.append(1) or {})
    task = asyncio.ensure_future(discovery.talk_down_discovery_worker())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert calls == []
    assert discovery.talk_down_discovery_state["worker_status"] == "disabled"


# ------------------------------------------------------------- no camera-number/model hardcoding

def test_no_camera_number_or_model_hardcoded():
    import inspect
    import re
    source = inspect.getsource(discovery)
    forbidden_models = ("CMIP3342WI", "CMIP1042W", "CMIP3342WI-28SDL", "CMIP1042W-28MA")
    for model in forbidden_models:
        assert model not in source
    assert not re.search(r"camera_number\s*(==|in)\s*[\(\d]", source)
    # Camera identity is only ever used to select which CAMERA{N}_* env
    # var prefix to read -- never to branch behavior by number.
    assert "if camera_number ==" not in source
    assert "if camera_number in" not in source


# ------------------------------------------------------------- structural isolation

def test_module_never_touches_recording_analytics_yolo_hls():
    import ast
    import inspect
    forbidden = {
        "ai_person_detector", "motion_detector", "save_yolo_events", "append_analytics_event",
        "analytics_rules_engine", "start_recording", "start_live_stream", "recording_uploader",
    }
    tree = ast.parse(inspect.getsource(discovery))
    referenced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            referenced.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            referenced.add(node.module)
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
    overlap = referenced & forbidden
    assert not overlap, f"talk_down_discovery.py unexpectedly references {overlap}"


def test_module_never_sends_audio_or_touches_rtsp():
    import inspect
    source = inspect.getsource(discovery)
    for forbidden in ("subprocess", "ffmpeg", "rtsp://", "AudioSource", "SendAudio"):
        assert forbidden not in source
