import os
import sys
from pathlib import Path

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


def test_direct_test_installs_only_run_scoped_outbound_block_rules():
    runtime = (INSTALLER / "install-runtime.ps1").read_text(encoding="utf-8")
    iss = (INSTALLER / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
    firewall = (INSTALLER / "firewall-direct-test.ps1").read_text(encoding="utf-8")
    files_section = iss.split("[Files]", 1)[1].split("[Icons]", 1)[0]
    direct_section = files_section.split("#ifdef DirectDellTestCandidate", 1)[1].split("#endif", 1)[0]
    assert "firewall-direct-test.ps1" in direct_section
    assert "-Apply -InstallRoot $InstallRoot -RunId $TestRunId" in runtime
    assert "-Remove -InstallRoot" in iss and "{#TestRunId}" in iss
    assert "-Direction Outbound -Action Block -Program $path -Protocol Any -RemoteAddress Any -Profile Any -Enabled True" in firewall
    assert "New-NetFirewallRule" in firewall and "Remove-NetFirewallRule" in firewall
    assert "-PolicyStore ActiveStore" in firewall and "DIRECT_TEST_FIREWALL_RULE_NOT_EFFECTIVE" in firewall
    assert "DIRECT_TEST_FIREWALL_PARTIAL_CLEANUP_UNCONFIRMED" in firewall
    assert "DIRECT_TEST_FIREWALL_RULE_NAME_COLLISION" in firewall
    assert "Test-BlockedRule $persistent[0] $record.Path" in firewall
    assert "Without a successful return, ownership cannot be" in firewall
    assert "established; preserve the entry" in firewall
    assert "-Action Allow" not in firewall and "-RemoteAddress LocalSubnet" not in firewall
    assert "DIRECT_TEST_FIREWALL_RULE_VERIFICATION_FAILED" in firewall


def test_direct_test_fixture_verifier_uses_synthetic_registry_and_cryptographic_verifiers():
    verifier = (INSTALLER / "verify-direct-test-licensing.py").read_text(encoding="utf-8")
    runtime = (INSTALLER / "install-runtime.ps1").read_text(encoding="utf-8")
    assert 'registry_doc.get("environment") != "synthetic"' in verifier
    assert 'purpose="features", for_new_grant=True' in verifier
    assert "appliance_entitlements.verify" in verifier
    assert "_validate_state(accepted, context, verifier=capacity_accept_windows._verify_ed25519)" in verifier
    assert "DIRECT_TEST_LICENSE_CRYPTOGRAPHIC_VERIFICATION_FAILED" in runtime


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
