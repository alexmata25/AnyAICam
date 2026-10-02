"""Regression coverage for configuration_issues() / the Operations ->
Health & recovery readiness validator.

Root cause (reported on a fresh Samsung install with zero cameras
provisioned): the legacy camera env-var check iterated
get_camera_numbers(), which falls back to range(1, LEGACY_DEFAULT_
CAMERA_COUNT + 1) (4) whenever the `cameras` DB table is empty --
conflating "candidate legacy slots" with "required slots". A brand-new,
fully-functional dynamic-provisioning install with zero DB camera rows
was therefore checked against 4 *candidate* CAMERA{n}_HOST/USERNAME/
PASSWORD slots as if all 12 of those env vars were required, reporting
"configuration valid: FAIL, 14 issues / 14 critical" for a state that
was never actually broken -- matching what Operations -> Health &
recovery showed.

Two fixes, both in configuration_issues():
  1. The legacy camera-slot loop no longer depends on get_camera_numbers()
     (DB-row-dependent) at all -- it always iterates the fixed
     LEGACY_DEFAULT_CAMERA_COUNT range, and only raises an issue for a
     slot that is *partially* configured (some but not all of its three
     env vars set) -- proof an operator is actively using the legacy
     scheme for that slot and got it wrong. A slot with none of the
     three set is simply not in use (dynamic or unprovisioned) and is
     never flagged.
  2. ANYAICAM_ADMIN_EMAIL/ANYAICAM_ADMIN_PASSWORD/ANYAICAM_PORTAL_SECRET
     were downgraded from "critical" to "warning" -- all three degrade
     gracefully today (partner_db.bootstrap_admin() no-ops without the
     first two; ANYAICAM_PORTAL_SECRET already has a real runtime
     fallback) and were never genuinely readiness-blocking.
"""
import main


_RECOMMENDED_GLOBAL = ("ANYAICAM_ADMIN_EMAIL", "ANYAICAM_ADMIN_PASSWORD", "ANYAICAM_PORTAL_SECRET")
_LEGACY_CAMERA_KEYS = [
    f"CAMERA{camera}_{suffix}"
    for camera in range(1, main.LEGACY_DEFAULT_CAMERA_COUNT + 1)
    for suffix in ("HOST", "USERNAME", "PASSWORD")
]


def _clear_env(monkeypatch):
    """Fresh-install baseline: none of the optional/legacy vars set."""
    for key in _RECOMMENDED_GLOBAL:
        monkeypatch.delenv(key, raising=False)
    for key in _LEGACY_CAMERA_KEYS:
        monkeypatch.delenv(key, raising=False)


def _critical(issues):
    return [issue for issue in issues if issue["severity"] == "critical"]


# =============================================================== fresh install, zero cameras


def test_fresh_install_zero_cameras_has_no_critical_issues(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    issues = main.configuration_issues()
    assert _critical(issues) == []


def test_fresh_install_zero_cameras_still_warns_about_missing_recommended_vars(monkeypatch):
    # Not hidden entirely -- just never readiness-blocking.
    _clear_env(monkeypatch)
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    issues = main.configuration_issues()
    warned_keys = {issue["key"] for issue in issues if issue["severity"] == "warning"}
    assert set(_RECOMMENDED_GLOBAL) <= warned_keys


def test_fresh_install_configuration_valid_and_ready_via_startup_self_test(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    result = main.startup_self_test()
    checks = {check["name"]: check for check in result["checks"]}
    assert checks["configuration_valid"]["ok"] is True


# =============================================================== dynamic cameras configured


def test_dynamic_cameras_configured_has_no_critical_camera_issues(monkeypatch):
    # Real, DB-provisioned dynamic cameras never touch the legacy
    # CAMERA{n}_* env vars at all -- configuration_issues() must not
    # depend on get_camera_numbers()'s DB-backed range any more, so this
    # is unaffected by how many real cameras exist.
    _clear_env(monkeypatch)
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [1, 2, 3, 4, 5])
    issues = main.configuration_issues()
    assert _critical(issues) == []


# =============================================================== legacy camera config partially supplied


def test_legacy_camera_slot_partially_configured_fails_for_the_missing_keys(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    monkeypatch.setenv("CAMERA1_HOST", "192.168.1.50")
    # USERNAME/PASSWORD left unset -- operator is actively using the
    # legacy scheme for slot 1 but got it wrong.
    issues = main.configuration_issues()
    critical_keys = {issue["key"] for issue in _critical(issues)}
    assert critical_keys == {"CAMERA1_USERNAME", "CAMERA1_PASSWORD"}
    assert "CAMERA1_HOST" not in critical_keys  # the one that *is* set is never flagged


def test_legacy_camera_slot_fully_configured_has_no_issues_for_that_slot(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    monkeypatch.setenv("CAMERA1_HOST", "192.168.1.50")
    monkeypatch.setenv("CAMERA1_USERNAME", "admin")
    monkeypatch.setenv("CAMERA1_PASSWORD", "hunter2")
    issues = main.configuration_issues()
    assert not any(issue["key"].startswith("CAMERA1_") for issue in issues)


def test_untouched_legacy_camera_slots_are_never_flagged_even_when_slot_1_is_partial(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    monkeypatch.setenv("CAMERA1_HOST", "192.168.1.50")  # slot 1 partial
    issues = main.configuration_issues()
    assert not any(issue["key"].startswith(("CAMERA2_", "CAMERA3_", "CAMERA4_")) for issue in issues)


# =============================================================== genuinely required config missing


def test_retention_days_below_minimum_is_still_critical(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    monkeypatch.setattr(main, "RETENTION_DAYS", 0)
    issues = main.configuration_issues()
    critical_keys = {issue["key"] for issue in _critical(issues)}
    assert "RETENTION_DAYS" in critical_keys


# =============================================================== optional/stale vars never fail readiness


def test_admin_email_password_portal_secret_missing_never_critical(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    issues = main.configuration_issues()
    critical_keys = {issue["key"] for issue in _critical(issues)}
    assert critical_keys.isdisjoint(set(_RECOMMENDED_GLOBAL))


def test_admin_email_password_portal_secret_present_clears_the_warning(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    for key in _RECOMMENDED_GLOBAL:
        monkeypatch.setenv(key, "configured-value")
    issues = main.configuration_issues()
    assert not any(issue["key"] in _RECOMMENDED_GLOBAL for issue in issues)


def test_leading_or_trailing_whitespace_on_recommended_vars_is_only_a_warning(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    monkeypatch.setenv("ANYAICAM_ADMIN_EMAIL", " admin@example.test ")
    issues = main.configuration_issues()
    matching = [issue for issue in issues if issue["key"] == "ANYAICAM_ADMIN_EMAIL"]
    assert matching and matching[0]["severity"] == "warning"
