"""Target: Investigate. Real route confirmed from source (app/main.py:
@app.get("/investigate")) -- client-side search/filter over a bounded,
server-embedded event snapshot (see _customer_investigate_events() and
PROJECT_CHECKPOINT.md's 2026-09-14 "Investigate reliability" entry).
Exact filter-control selectors are TODO.
"""
import pytest


@pytest.mark.e2e
def test_investigate_loads_after_login(page, base_url, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/investigate")
    assert page.url.endswith("/investigate")


@pytest.mark.e2e
def test_investigate_search_filters_results(page, base_url, e2e_credentials):
    pytest.skip("search box / result-row selectors not yet confirmed against a real authenticated session")


@pytest.mark.e2e
def test_investigate_event_deep_links_to_playback(page, base_url, e2e_credentials):
    pytest.skip("event-row deep-link selector not yet confirmed against a real authenticated session")
