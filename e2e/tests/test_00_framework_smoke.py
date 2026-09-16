"""Proof-of-life for the e2e framework itself (2026-09-15): launches the
real staging VMS and reaches the real customer login screen. Deliberately
needs NO credentials -- this is the one test in this directory meant to
pass today, before e2e_credentials (see conftest.py) has anything to
supply. Every other file in tests/ is a scaffolded, skip-marked target
for once a dedicated staging test-tenant login exists.

Targets real, already-verified markup (app/customer-login.html), not
guessed selectors -- read directly from source before writing this.
"""
import json


def test_staging_login_screen_loads(page, base_url, artifacts_dir, console_network_dir, console_and_network_capture):
    response = page.goto("/customer-login.html")

    assert response is not None
    assert response.status == 200, f"expected 200 from {base_url}/customer-login.html, got {response.status}"
    assert page.title() == "Customer Login | ANY AI CAM"

    assert page.get_by_role("heading", name="Customer sign in").is_visible()
    assert page.locator("#login").is_visible()
    assert page.locator("#email").is_visible()
    assert page.locator("#password").is_visible()
    assert page.locator("form#login button.submit").is_visible()

    page.screenshot(path=str(artifacts_dir / "smoke_staging_login_screen.png"), full_page=True)

    # Always written for this specific proof test (not just on failure,
    # unlike the autouse fixture's own default) -- this run's console/
    # network evidence is exactly what proves the framework is real.
    (console_network_dir / "smoke_staging_login_screen.json").write_text(
        json.dumps(console_and_network_capture, indent=2), encoding="utf-8",
    )
