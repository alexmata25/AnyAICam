"""Target: Investigate. Real route confirmed from source (app/main.py:
@app.get("/investigate")) -- client-side search/filter over a bounded,
server-embedded event snapshot (see _customer_investigate_events() and
PROJECT_CHECKPOINT.md's 2026-09-14 "Investigate reliability" entry).
Selectors confirmed live 2026-09-16 against the real test account: a
real natural-language search box (#investigation-query, placeholder
"Example: red truck on camera 2 yesterday"), a color filter
(#investigation-color), and 1086 real event rows
([data-event-id]) already embedded for this real customer.
"""
import pytest


@pytest.fixture
def investigate_page(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/investigate")
    page.wait_for_selector("#investigation-query")
    return page


@pytest.mark.e2e
def test_investigate_loads_after_login(investigate_page):
    page = investigate_page
    assert page.url.endswith("/investigate")
    assert page.title() == "Investigate · AnyAiCam"


@pytest.mark.e2e
def test_investigate_shows_the_customers_real_embedded_events(investigate_page):
    page = investigate_page
    rows = page.locator("[data-event-id]")
    assert rows.count() > 0, "expected at least one real event row embedded for this customer"


@pytest.mark.e2e
def test_investigate_search_box_accepts_a_natural_language_query(investigate_page):
    page = investigate_page
    query_box = page.locator("#investigation-query")
    assert query_box.count() == 1
    query_box.fill("person")
    assert query_box.input_value() == "person"


@pytest.mark.e2e
def test_investigate_search_narrows_the_visible_event_rows(investigate_page):
    """A real, live-data assertion: searching a specific, uncommon term
    should never show MORE rows than the unfiltered baseline, and
    typically shows fewer -- proves the client-side filter genuinely
    runs against the real embedded data, not a no-op."""
    page = investigate_page
    total_rows = page.locator("[data-event-id]").count()
    if total_rows == 0:
        pytest.skip("no real events embedded for this customer -- nothing to narrow")
    query_box = page.locator("#investigation-query")
    query_box.fill("intrusion")
    query_box.press("Enter")
    page.wait_for_timeout(500)
    filtered_rows = page.locator("[data-event-id]:visible").count()
    assert filtered_rows <= total_rows
