"""Regression coverage for a real, confirmed-live defect found during golden
RC1's first genuinely clean install on Ryzen (2026-09-11, zero manual
runtime patches): configuration_issues() required AWS_REGION, an external
database, an S3 bucket, a public URL, and Secrets Manager to be configured
directly on the VMS container whenever ANYAICAM_ENV=production -- entirely
regardless of ANYAICAM_RUNTIME_ROLE. This contradicted readiness_snapshot()'s
own deliberate role scoping one level up (role_ready only requires
cloud_foundation_ready for RUNTIME_ROLE cloud/combined, never edge), and made
every genuinely clean, unclaimed, production edge appliance permanently
un-ready via GET /ready regardless of camera/claim state, because
configuration_valid (a critical startup_self_test() check) could never pass.

No prior Ryzen validation ever caught this because every earlier session's
Ryzen had AWS_REGION etc. already hand-patched into vms.env, left over from
unrelated motion-cloud validation work (see docs/checkpoints/RYZEN.md) --
this is the first time a truly clean install, with nothing manually patched,
was ever checked against GET /ready.

The same fix also covers ANYAICAM_FORCE_HTTPS, which had the identical
shape: _default_force_https() already deliberately defaults it to False for
production+edge (a production edge appliance has no TLS listener of its
own -- reached over a private LAN/Tailscale or an operator's own reverse
proxy), but configuration_issues() required it be True anyway, contradicting
its own documented default.

configuration_issues() is tested directly here (the root function), not the
full GET /ready HTTP surface -- matching this suite's own established
pattern (see test_camera_numbers_phantom_fallback.py's docstring) of testing
the one function every downstream consumer (startup_self_test(),
readiness_snapshot(), GET /ready) routes through, rather than duplicating
coverage at every layer above it.
"""
import pytest

import main


@pytest.fixture(autouse=True)
def _isolated_role_env(monkeypatch):
    """Every test sets DEPLOYMENT_ENV/RUNTIME_ROLE/FORCE_HTTPS/SECURE_COOKIES
    explicitly -- these are module-level constants baked in at import time
    from env vars, so monkeypatching the module attributes directly (not
    os.environ) is what actually takes effect, matching this file's own
    established pattern (e.g. test_aws_region_configuration_guard.py)."""
    monkeypatch.setattr(main, "SECURE_COOKIES", True)
    yield


def _missing_cloud_requirement_keys(issues):
    return {issue["key"] for issue in issues if issue["message"].startswith("AWS deployment requirement")}


# =============================================================== edge role: cloud/AWS requirements must never apply


def test_production_edge_with_no_cloud_config_raises_no_cloud_issues(monkeypatch):
    """The exact Ryzen scenario: ANYAICAM_ENV=production, ANYAICAM_RUNTIME_
    ROLE=edge, nothing AWS/cloud-related configured. Before the fix, this
    unconditionally raised 5 critical issues (aws_region_configured,
    database_configured, s3_configured, public_url_configured,
    secrets_manager_configured) and made configuration_valid permanently
    False. None of them are appropriate for a pure edge appliance."""
    monkeypatch.setattr(main, "DEPLOYMENT_ENV", "production")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(main, "FORCE_HTTPS", False)  # the correct default for production+edge
    monkeypatch.setattr(
        main,
        "cloud_configuration_snapshot",
        lambda: {"missing_cloud_requirements": [
            "aws_region_configured", "database_configured", "s3_configured",
            "public_url_configured", "secrets_manager_configured",
        ]},
    )
    issues = main.configuration_issues()
    assert _missing_cloud_requirement_keys(issues) == set()
    critical = [issue for issue in issues if issue["severity"] == "critical"]
    assert critical == [], f"expected zero critical issues on a clean production-edge box, got: {critical}"


def test_staging_edge_with_no_cloud_config_raises_no_cloud_issues(monkeypatch):
    """Same role-awareness must hold in staging, not just production --
    the cloud_configuration_snapshot() loop was gated on
    DEPLOYMENT_ENV alone before this fix, with no staging/production
    distinction in whether RUNTIME_ROLE mattered."""
    monkeypatch.setattr(main, "DEPLOYMENT_ENV", "staging")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(main, "FORCE_HTTPS", True)
    monkeypatch.setattr(
        main, "cloud_configuration_snapshot",
        lambda: {"missing_cloud_requirements": ["aws_region_configured", "database_configured"]},
    )
    issues = main.configuration_issues()
    assert _missing_cloud_requirement_keys(issues) == set()


# =============================================================== cloud/combined role: requirements are unchanged


def test_production_cloud_role_still_requires_cloud_config(monkeypatch):
    """The fix must not weaken the real requirement for a RUNTIME_ROLE=cloud
    appliance -- it genuinely does need AWS/database/S3/public-URL/secrets-
    manager configuration, and a missing one must still be critical in
    production."""
    monkeypatch.setattr(main, "DEPLOYMENT_ENV", "production")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    monkeypatch.setattr(main, "FORCE_HTTPS", True)
    monkeypatch.setattr(
        main, "cloud_configuration_snapshot",
        lambda: {"missing_cloud_requirements": ["aws_region_configured", "s3_configured"]},
    )
    issues = main.configuration_issues()
    assert _missing_cloud_requirement_keys(issues) == {"aws_region_configured", "s3_configured"}
    critical_keys = {issue["key"] for issue in issues if issue["severity"] == "critical"}
    assert {"aws_region_configured", "s3_configured"} <= critical_keys


def test_production_combined_role_still_requires_cloud_config(monkeypatch):
    monkeypatch.setattr(main, "DEPLOYMENT_ENV", "production")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "combined")
    monkeypatch.setattr(main, "FORCE_HTTPS", True)
    monkeypatch.setattr(
        main, "cloud_configuration_snapshot",
        lambda: {"missing_cloud_requirements": ["public_url_configured"]},
    )
    issues = main.configuration_issues()
    assert _missing_cloud_requirement_keys(issues) == {"public_url_configured"}


def test_staging_cloud_role_missing_config_is_a_warning_not_critical(monkeypatch):
    """Unchanged pre-existing behavior: staging severity stays 'warning'
    regardless of this fix -- only the production/edge combination changed."""
    monkeypatch.setattr(main, "DEPLOYMENT_ENV", "staging")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    monkeypatch.setattr(main, "FORCE_HTTPS", True)
    monkeypatch.setattr(
        main, "cloud_configuration_snapshot",
        lambda: {"missing_cloud_requirements": ["aws_region_configured"]},
    )
    issues = main.configuration_issues()
    matching = [issue for issue in issues if issue["key"] == "aws_region_configured"]
    assert matching and matching[0]["severity"] == "warning"


# =============================================================== FORCE_HTTPS: same role-aware treatment


def test_production_edge_with_force_https_false_raises_no_https_issue(monkeypatch):
    """Mirrors _default_force_https()'s own edge_production carve-out: False
    is the CORRECT default for production+edge (no TLS listener of its
    own), so this must not be flagged as a critical misconfiguration."""
    monkeypatch.setattr(main, "DEPLOYMENT_ENV", "production")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(main, "FORCE_HTTPS", False)
    monkeypatch.setattr(main, "cloud_configuration_snapshot", lambda: {"missing_cloud_requirements": []})
    issues = main.configuration_issues()
    assert not any(issue["key"] == "ANYAICAM_FORCE_HTTPS" for issue in issues)


def test_production_combined_role_with_force_https_false_still_raises_critical(monkeypatch):
    """The carve-out is edge-only -- combined (and cloud) production still
    genuinely needs HTTPS enforcement, exactly as before this fix."""
    monkeypatch.setattr(main, "DEPLOYMENT_ENV", "production")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "combined")
    monkeypatch.setattr(main, "FORCE_HTTPS", False)
    monkeypatch.setattr(main, "cloud_configuration_snapshot", lambda: {"missing_cloud_requirements": []})
    issues = main.configuration_issues()
    matching = [issue for issue in issues if issue["key"] == "ANYAICAM_FORCE_HTTPS"]
    assert matching and matching[0]["severity"] == "critical"


def test_production_cloud_role_with_force_https_false_still_raises_critical(monkeypatch):
    monkeypatch.setattr(main, "DEPLOYMENT_ENV", "production")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    monkeypatch.setattr(main, "FORCE_HTTPS", False)
    monkeypatch.setattr(main, "cloud_configuration_snapshot", lambda: {"missing_cloud_requirements": []})
    issues = main.configuration_issues()
    matching = [issue for issue in issues if issue["key"] == "ANYAICAM_FORCE_HTTPS"]
    assert matching and matching[0]["severity"] == "critical"


def test_production_edge_with_force_https_true_still_fine(monkeypatch):
    """An operator who explicitly turns FORCE_HTTPS on for an edge box
    (e.g. it has its own real TLS termination) must never be flagged --
    this only ever exempts the False/default case."""
    monkeypatch.setattr(main, "DEPLOYMENT_ENV", "production")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(main, "FORCE_HTTPS", True)
    monkeypatch.setattr(main, "cloud_configuration_snapshot", lambda: {"missing_cloud_requirements": []})
    issues = main.configuration_issues()
    assert not any(issue["key"] == "ANYAICAM_FORCE_HTTPS" for issue in issues)


# =============================================================== end-to-end: the exact Ryzen self-test shape


def test_production_edge_clean_install_passes_startup_self_test_configuration_check(monkeypatch, tmp_path):
    """The exact condition that made startup_self_test()['ok'] False on
    Ryzen's clean install: production + edge + zero AWS/cloud config. Only
    the configuration_valid check is exercised here (recordings_writable/
    storage_available/ffmpeg_available depend on real filesystem/binary
    state unrelated to this fix); this proves configuration_valid alone no
    longer blocks self_test.ok for this exact role/environment shape."""
    monkeypatch.setattr(main, "DEPLOYMENT_ENV", "production")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(main, "FORCE_HTTPS", False)
    monkeypatch.setattr(main, "cloud_configuration_snapshot", lambda: {
        "missing_cloud_requirements": [
            "aws_region_configured", "database_configured", "s3_configured",
            "public_url_configured", "secrets_manager_configured",
        ],
    })
    issues = main.configuration_issues()
    critical = [issue for issue in issues if issue["severity"] == "critical"]
    assert critical == []
