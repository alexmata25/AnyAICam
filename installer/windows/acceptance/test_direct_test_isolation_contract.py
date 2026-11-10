import os
import sys
from pathlib import Path
import pytest

APP = Path(__file__).resolve().parents[3] / "app"
INSTALLER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))


def test_direct_test_mediamtx_is_loopback_only_and_production_config_is_unchanged(monkeypatch):
    import webrtc_publisher

    monkeypatch.delenv("ANYAICAM_INTERNAL_TEST_BUILD", raising=False)
    production = webrtc_publisher.render_mediamtx_config(windows=True)
    assert "webrtcLocalUDPAddress: :8189" in production
    monkeypatch.setenv("ANYAICAM_INTERNAL_TEST_BUILD", "1")
    isolated = webrtc_publisher.render_mediamtx_config(windows=True)
    assert "webrtcLocalUDPAddress: 127.0.0.1:8189" in isolated
    assert "webrtcAdditionalHosts:" not in isolated
    assert "webrtcICEServers2:" not in isolated
    assert "apiAddress: 127.0.0.1:9997" in isolated
    assert "webrtcAddress: 127.0.0.1:8889" in isolated


def test_direct_test_setup_uses_run_scoped_data_and_never_provisions_production_cloud():
    iss = (INSTALLER / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
    runtime = (INSTALLER / "install-runtime.ps1").read_text(encoding="utf-8")
    launcher = (INSTALLER / "service-launcher-direct-test.ps1").read_text(encoding="utf-8")
    assert "{commonappdata}\\AnyAiCam-Test\\{#TestRunId}" in iss
    assert "DisableDirPage=yes" in iss
    files_section = iss.split("[Files]", 1)[1].split("[Icons]", 1)[0]
    direct_files = files_section.split("#ifdef DirectDellTestCandidate", 1)[1].split("#endif", 1)[0]
    assert "AnyAiCamVMSDirectTest.xml" in direct_files and "AnyAiCamVMSDirectTest.exe" in direct_files
    assert "AnyAiCamAgent.exe" not in direct_files
    assert "DIRECT_TEST_INSTALL_PATH_REFUSED" in (INSTALLER / "direct-test-install-guard.ps1").read_text(encoding="utf-8")
    assert "-DirectTest -TestRunId" in iss
    assert "cloud-link.ps1" in runtime and "if (-not $DirectTest)" in runtime
    assert "$values['ANYAICAM_AGENT_MODE'] = 'disabled'" in runtime
    assert "$values['ANYAICAM_PRODUCT_MODE'] = 'local'" in runtime
    for flag in ("ANYAICAM_CLOUD_UPLOAD_ENABLED", "ANYAICAM_RECORDING_UPLOAD_ENABLED",
                 "ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED", "ANYAICAM_ANALYTICS_SYNC_ENABLED",
                 "ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED", "ANYAICAM_LIVE_RELAY_ENABLED",
                 "ANYAICAM_LIVE_P2P_ENABLED"):
        assert f"$values['{flag}'] = 'false'" in runtime
    assert "firewall.ps1" in runtime and "if (-not $DirectTest)" in runtime
    assert "127.0.0.1" in launcher and "AnyAiCam-Test" in launcher
    assert "ANYAICAM_INTERNAL_TEST_BUILD = '1'" in launcher
    assert "vms_capacity_accepted.json" in runtime
    assert "DIRECT_TEST_CAPACITY_TRUST_REFUSED" in runtime


def test_direct_test_build_refuses_non_synthetic_or_unpinned_license_inputs():
    build = (INSTALLER / "build.ps1").read_text(encoding="utf-8")
    runtime = (INSTALLER / "install-runtime.ps1").read_text(encoding="utf-8")
    assert "DirectDellTestCandidate requires -InternalTestAzureSign" in build
    assert "TestApplianceIdentitySha256" in build and "TestAcceptedCapacitySha256" in build
    assert "TestFeatureEntitlementsSha256" in build
    assert "registryDocument.environment -cne 'synthetic'" in build
    assert "DIRECT_TEST_SYNTHETIC_TRUST_REQUIRED" in runtime
    assert "DIRECT_TEST_IDENTITY_NOT_SYNTHETIC" in runtime
    assert "DIRECT_TEST_ENTITLEMENT_TRUST_REFUSED" in runtime


def test_direct_catalog_identity_is_separate_for_generation_registration_and_removal():
    build = (INSTALLER / "build.ps1").read_text(encoding="utf-8")
    iss = (INSTALLER / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
    runtime = (INSTALLER / "runtime-catalog.ps1").read_text(encoding="utf-8")
    install_runtime = (INSTALLER / "install-runtime.ps1").read_text(encoding="utf-8")
    assert "AnyAiCamVmsDirectTestRuntime.cat" in build
    assert 'RuntimeCatalogFileName "AnyAiCamVmsDirectTestRuntime.cat"' in iss
    assert 'RuntimeCatalogFileName "AnyAiCamRuntime.cat"' in iss
    assert "-Remove -DirectTest" in iss
    assert "if ($DirectTest) { 'AnyAiCamVmsDirectTestRuntime.cat' } else { 'AnyAiCamRuntime.cat' }" in runtime
    assert "GetFileName($CatalogPath) -cne $CatalogBaseName" in runtime
    assert "-DirectTest:$DirectTest" in install_runtime


def test_direct_test_skips_unshipped_cloud_agent_and_uses_matching_loopback_url():
    runtime = (INSTALLER / "install-runtime.ps1").read_text(encoding="utf-8")
    launcher = (INSTALLER / "service-launcher-direct-test.ps1").read_text(encoding="utf-8")
    iss = (INSTALLER / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
    agent_preflight = runtime.index("AnyAiCam agent preflight passed")
    guard = runtime.rfind("if (-not $DirectTest)", 0, agent_preflight)
    assert guard >= 0
    assert "$values['ANYAICAM_PUBLIC_URL'] = $(if ($DirectTest) { 'http://127.0.0.1:18767' }" in runtime
    assert "$env:ANYAICAM_PUBLIC_URL = 'http://127.0.0.1:18767'" in launcher
    assert "--port 18767" in launcher
    assert "verify-direct-test-licensing.py" in iss


def test_direct_test_blocks_public_internet_but_preserves_consented_lan_camera_traffic():
    runtime = (INSTALLER / "install-runtime.ps1").read_text(encoding="utf-8")
    iss = (INSTALLER / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
    firewall = (INSTALLER / "firewall-direct-test.ps1").read_text(encoding="utf-8")
    files_section = iss.split("[Files]", 1)[1].split("[Icons]", 1)[0]
    direct_section = files_section.split("#ifdef DirectDellTestCandidate", 1)[1].split("#endif", 1)[0]
    assert "firewall-direct-test.ps1" in direct_section
    assert "-Apply -InstallRoot $InstallRoot -RunId $TestRunId -CameraNetwork $TestCameraNetwork" in runtime
    assert "-Remove -InstallRoot" in iss and "{#TestRunId}" in iss and "{#TestCameraNetwork}" in iss
    assert "Get-DirectTestIPv4ComplementCidrs" in firewall
    assert "'127.0.0.0/8', '239.255.255.250/32'" in firewall
    assert "'BlockIPv4OutsideAllowlist','BlockAllIPv6','BlockCameraFromOtherLocalIPv4','AllowCameraOnPinnedInterface','AllowLoopback','AllowOnvifMulticastOnPinnedInterface'" in firewall
    assert "-RemoteAddress $spec.Addresses" in firewall
    assert "-LocalAddress $spec.LocalAddresses -InterfaceAlias $spec.InterfaceAliases" in firewall
    assert "Get-NetFirewallInterfaceFilter" in firewall
    assert "BlockCameraFromOtherLocalIPv4" in firewall and "AllowCameraOnPinnedInterface" in firewall
    assert "-InterfaceAlias $spec.InterfaceAliases" in firewall
    assert "'::/0'" in firewall
    assert "New-NetFirewallRule" in firewall and "Remove-NetFirewallRule" in firewall
    assert "-PolicyStore ActiveStore" in firewall and "DIRECT_TEST_FIREWALL_RULE_NOT_EFFECTIVE" in firewall
    assert "DIRECT_TEST_FIREWALL_PARTIAL_CLEANUP_UNCONFIRMED" in firewall
    assert "DIRECT_TEST_FIREWALL_RULE_NAME_COLLISION" in firewall
    assert "Test-Rule $persistent[0] $spec" in firewall
    assert "DIRECT_TEST_FIREWALL_REMOVE_ACTIVE_RULE_UNCONFIRMED" in firewall
    assert "if (@(Get-NetFirewallRule -PolicyStore PersistentStore -Name $spec.Name" in firewall
    assert "Action = 'Allow'" in firewall and "RemoteAddress Any" not in firewall
    assert "DIRECT_TEST_FIREWALL_CAMERA_NETWORK_REFUSED" in firewall
    assert "Camera-subnet and ONVIF multicast egress require the recorded interface and local address" in firewall
    assert "DIRECT_TEST_FIREWALL_RULE_VERIFICATION_FAILED" in firewall
    assert "Get-NetIPInterface -AddressFamily IPv4" in firewall
    assert "DefaultOutboundAction" in firewall
    assert "Assert-DirectTestStrongHostSend" in firewall
    assert "WeakHostSend is Disabled on all active IPv4 interfaces" in firewall
    setup = (INSTALLER / "prepare-direct-test-profile.ps1").read_text(encoding="utf-8")
    assert "DIRECT_TEST_CAMERA_NETWORK_INTERFACE_AMBIGUOUS" in setup


def test_direct_test_network_features_remain_local_and_cloud_uploads_are_forced_off():
    launcher = (INSTALLER / "service-launcher-direct-test.ps1").read_text(encoding="utf-8")
    main = (APP / "main.py").read_text(encoding="utf-8")
    discovery = main.split("def v110_validate_network", 1)[1].split("@app.post(\"/api/camera-discovery/scan\")", 1)[0]
    onvif = (APP / "onvif_discovery.py").read_text(encoding="utf-8")
    assert "$env:ANYAICAM_BIND_HOST = '127.0.0.1'" in launcher
    assert "$env:ANYAICAM_HTTP_PORT = '18767'" in launcher
    assert "$env:ANYAICAM_CLOUD_UPLOAD_ENABLED = 'false'" in launcher
    for flag in ("ANYAICAM_ANALYTICS_SYNC_ENABLED", "ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED",
                 "ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED", "ANYAICAM_LIVE_RELAY_ENABLED",
                 "ANYAICAM_RECORDING_UPLOAD_ENABLED", "ANYAICAM_LIVE_P2P_ENABLED"):
        assert f"$env:{flag} = 'false'" in launcher
    assert "Remove-Item -LiteralPath (\"Env:\" + $proxyName)" in launcher
    assert "$env:AWS_SHARED_CREDENTIALS_FILE = Join-Path $config 'aws-credentials-disabled'" in launcher
    assert "$env:AWS_CONFIG_FILE = Join-Path $config 'aws-config-disabled'" in launcher
    assert "$env:AWS_EC2_METADATA_DISABLED = 'true'" in launcher
    profile = (APP / "direct_test_profile.py").read_text(encoding="utf-8")
    assert "validate_camera_scan_network" in discovery and "V110_MAX_HOSTS" in discovery
    assert "requested.subnet_of(approved)" in profile and "requested.is_private" in profile
    assert "source_address=source_address" in main
    assert "DIRECT_TEST_CAMERA_INTERFACE_NO_LONGER_ACTIVE" in launcher
    for module_name in ("main.py", "camera_video_probe.py", "camera_address_recovery.py",
                        "analytics_frames.py", "motion_source.py", "main_frame_grab.py", "webrtc_publisher.py"):
        module = (APP / module_name).read_text(encoding="utf-8")
        assert "rtsp_input_options()" in module, module_name
    assert "payload.consent_confirmed" in main
    assert "239.255.255.250" in onvif and "3702" in onvif
    assert "RTSP_PORTS = (554, 8554)" in onvif
    assert "address.is_private" in onvif


def test_credentialed_camera_verification_is_pinned_to_test_network_and_interface():
    main = (APP / "main.py").read_text(encoding="utf-8")
    start = main.index("def _v111_direct_test_source_address")
    end = main.index("@app.post(\"/api/camera-verification/verify\")", start)
    verification = main[start:end]
    assert "validate_camera_scan_network" in verification
    assert "SourceBoundHTTPConnection" in verification and "SourceBoundHTTPSConnection" in verification
    assert "source_address=source" in verification
    assert "NoRedirectHandler" in verification
    assert "urllib.request.ProxyHandler({})" in verification
    handler = main[end:main.index("authenticated_protocols =", end)]
    assert "_v111_direct_test_source_address(str(selected_ip))" in handler
    assert "source_address=camera_source_address" in handler


def test_onvif_camera_discovery_accepts_only_consented_private_lan_fixture():
    import onvif_discovery

    private_probe = ('<e:Envelope xmlns:e="x" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
                     '<d:EndpointReference><d:Address>uuid:12345678-1234-4234-8234-123456789abc</d:Address>'
                     '</d:EndpointReference><d:XAddrs>http://192.168.50.22:8080/onvif/device_service</d:XAddrs></e:Envelope>')
    public_probe = private_probe.replace("192.168.50.22", "8.8.8.8")
    tcp_calls = []

    def tcp_probe(host, port, timeout):
        tcp_calls.append((host, port))
        return True

    found = onvif_discovery.probe(send_probe=lambda: [private_probe, public_probe], tcp_probe=tcp_probe,
                                  allowed_network="192.168.50.0/24", interface_address="192.168.50.10",
                                  interface_network="192.168.50.0/24")
    assert len(found) == 1
    assert found[0]["ip_address"] == "192.168.50.22"
    assert found[0]["rtsp_reachable"] and found[0]["onvif_reachable"]
    assert tcp_calls == [("192.168.50.22", 554), ("192.168.50.22", 80)]


def test_onvif_multicast_probe_is_bound_to_the_approved_interface(monkeypatch):
    import socket
    import onvif_discovery

    class FakeSocket:
        def __init__(self):
            self.options = []
            self.sent = []
            self.closed = False

        def setsockopt(self, level, option, value):
            self.options.append((level, option, value))

        def settimeout(self, timeout):
            self.timeout = timeout

        def bind(self, address):
            self.bound = address

        def sendto(self, payload, address):
            self.sent.append((payload, address))

        def recvfrom(self, size):
            raise socket.timeout()

        def close(self):
            self.closed = True

    fake = FakeSocket()
    monkeypatch.setattr(onvif_discovery.socket, "socket", lambda *args: fake)
    assert onvif_discovery.default_send_probe(interface_address="192.168.50.10") == []
    assert (socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("192.168.50.10")) in fake.options
    assert fake.bound == ("192.168.50.10", 0)
    assert fake.sent[0][1] == ("239.255.255.250", 3702)
    assert fake.closed


def test_onvif_tcp_probes_bind_to_the_approved_source_address(monkeypatch):
    import socket
    import onvif_discovery

    xml = ('<e:Envelope xmlns:e="x" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
           '<d:Address>uuid:12345678-1234-4234-8234-123456789abc</d:Address>'
           '<d:XAddrs>http://192.168.50.22:8080/onvif/device_service</d:XAddrs></e:Envelope>')
    calls = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def connect(address, timeout=None, source_address=None):
        calls.append((address, timeout, source_address))
        return FakeConnection()

    monkeypatch.setattr(socket, "create_connection", connect)
    result = onvif_discovery.probe(
        send_probe=lambda: [xml], allowed_network="192.168.50.0/24",
        interface_address="192.168.50.10", interface_network="192.168.50.0/24",
    )
    assert len(result) == 1 and result[0]["rtsp_reachable"] and result[0]["onvif_reachable"]
    assert calls == [
        (("192.168.50.22", 554), onvif_discovery.TCP_CONNECT_TIMEOUT_SECONDS, ("192.168.50.10", 0)),
        (("192.168.50.22", 80), onvif_discovery.TCP_CONNECT_TIMEOUT_SECONDS, ("192.168.50.10", 0)),
    ]


def test_onvif_discovery_refuses_network_not_attached_to_profile_interface():
    import onvif_discovery

    with pytest.raises(ValueError, match="verified attached interface"):
        onvif_discovery.probe(send_probe=lambda: [], allowed_network="192.168.60.0/24",
                              interface_address="192.168.50.10", interface_network="192.168.50.0/24")


def test_direct_firewall_policy_implementation_and_installer_binding():
    firewall = (INSTALLER / "firewall-direct-test.ps1").read_text(encoding="utf-8")
    policy = (INSTALLER / "direct-test-firewall-policy.ps1").read_text(encoding="utf-8")
    runtime = (INSTALLER / "install-runtime.ps1").read_text(encoding="utf-8")
    iss = (INSTALLER / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
    assert "ConvertTo-DirectTestIPv4Number" in policy
    assert "Get-DirectTestIPv4ComplementCidrs" in policy
    assert "DIRECT_TEST_FIREWALL_ALLOWLIST_OVERLAP" in policy
    assert "TestCameraNetwork" in runtime and "-CameraNetwork $TestCameraNetwork" in runtime
    assert "-TestCameraNetwork \"{#TestCameraNetwork}\"" in iss
    assert "-CameraNetwork \"\"{#TestCameraNetwork}\"\"" in iss
    assert "direct-test-firewall-policy.ps1" in iss


def test_direct_test_fixture_verifier_uses_synthetic_registry_and_cryptographic_verifiers():
    verifier = (INSTALLER / "verify-direct-test-licensing.py").read_text(encoding="utf-8")
    runtime = (INSTALLER / "install-runtime.ps1").read_text(encoding="utf-8")
    assert 'registry_doc.get("environment") != "synthetic"' in verifier
    assert 'purpose="features", for_new_grant=True' in verifier
    assert "appliance_entitlements.verify" in verifier
    assert "_validate_state(accepted, context, verifier=capacity_accept_windows._verify_ed25519)" in verifier
    assert "DIRECT_TEST_LICENSE_CRYPTOGRAPHIC_VERIFICATION_FAILED" in runtime


def test_backup_fixture_is_explicit_about_privilege_and_metadata_limits():
    backup = (INSTALLER / "acceptance" / "Test-DirectTestBackupTooling.ps1").read_text(encoding="utf-8")
    plan = (INSTALLER / "acceptance" / "DIRECT-DELL-BACKUP-AND-RESTORE-PLAN.md").read_text(encoding="utf-8")
    assert "[IO.Path]::GetPathRoot($fixture)" in backup
    assert "A required privilege is not held by the client" in backup
    assert "You do not have the Manage Auditing user right" in backup
    assert "creation_utc_ticks" in backup and "last_access_utc_ticks" in backup and "last_write_utc_ticks" in backup
    assert "else { 'NOT_VERIFIED' }" in backup
    assert "FullyEncrypted" in plan and "ProtectionStatus=On" in plan
    assert "Never restore over production data" in plan


def test_test_profile_covers_service_config_trust_and_recording_roots_only():
    paths = (APP / "platform_paths.py").read_text(encoding="utf-8")
    profile = (APP / "direct_test_profile.py").read_text(encoding="utf-8")
    launcher = (INSTALLER / "service-launcher-direct-test.ps1").read_text(encoding="utf-8")
    trust_installer = (INSTALLER.parents[1] / "appliance-agent" / "system" / "trust_install_windows.py").read_text(encoding="utf-8")
    assert "ANYAICAM_INTERNAL_TEST_BUILD" in paths and "resolve_from_app_module" in paths
    assert "selected is None" in paths and "AnyAiCam-Test" in profile
    for env_name in ("ANYAICAM_CONFIG_DIR", "ANYAICAM_STATE_DIR", "ANYAICAM_LOG_DIR", "ANYAICAM_RECORDING_PATH",
                     "ANYAICAM_VMS_RECORDINGS_PATH", "ANYAICAM_MEDIAMTX_CONFIG", "ANYAICAM_FEATURE_ENTITLEMENTS_FILE"):
        assert env_name in launcher
    assert "platform_paths" in trust_installer and "capacity_accept_windows" in trust_installer
