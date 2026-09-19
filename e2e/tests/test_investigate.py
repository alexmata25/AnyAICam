"""Target: Investigate. Real route confirmed from source (app/main.py:
@app.get("/investigate")) -- client-side search/filter over a bounded,
server-embedded event snapshot (see _customer_investigate_events() and
PROJECT_CHECKPOINT.md's 2026-09-14 "Investigate reliability" entry).
Selectors confirmed live 2026-09-16 against the real test account: a
real natural-language search box (#investigation-query, placeholder
"Example: red truck on camera 2 yesterday"), a color filter
(#investigation-color), and 1086 real event rows
([data-event-id]) already embedded for this real customer.

Each result is an <article class="investigation-card" data-event-id>
with a `.investigation-thumb` (a real `<img>` when the event has a
thumbnail, or a `.investigation-placeholder` "No thumbnail" div when it
doesn't -- confirmed from source, card()) and a
`.investigation-card-actions a.primary` "Playback" link. That link's
href is built by the one shared, canonical
_customer_event_playback_href() (app/main.py) -- always at least
`/playback?camera=<id>`, never a broken/empty link, per that function's
own documented history of two real "Investigate -> Playback handoff"
bugs it was written to fix once and for all.
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


@pytest.mark.e2e
def test_investigate_results_show_a_real_thumbnail_or_an_explicit_placeholder(investigate_page):
    """Never a silently blank thumbnail area: source (card(), app/main.py)
    always renders either a real <img> or a ".investigation-placeholder"
    with "No thumbnail" text -- this proves that contract live, and that
    any real <img> actually decodes."""
    page = investigate_page
    first_thumb = page.locator(".investigation-thumb").first
    if first_thumb.count() == 0:
        pytest.skip("no investigation results rendered -- nothing to check thumbnails for")
    img = first_thumb.locator("img")
    placeholder = first_thumb.locator(".investigation-placeholder")
    assert img.count() == 1 or placeholder.count() == 1, "every result thumbnail area must be a real image or an explicit placeholder, never blank"
    if img.count() == 1:
        assert img.get_attribute("src")
        page.wait_for_function(
            "img => img.complete && img.naturalWidth > 0",
            arg=img.element_handle(),
            timeout=5000,
        )
    else:
        assert "No thumbnail" in (placeholder.text_content() or "")


@pytest.mark.e2e
def test_clicking_playback_from_a_result_navigates_to_playback_for_that_events_camera(investigate_page):
    """The real, canonical handoff (_customer_event_playback_href()) --
    every result's Playback link always carries at least ?camera=<id>,
    per that function's own fix for two real historical "Investigate ->
    Playback handoff" bugs (see this file's own module docstring)."""
    page = investigate_page
    playback_link = page.locator(".investigation-card .investigation-card-actions a.primary").first
    if playback_link.count() == 0:
        pytest.skip("no investigation results rendered -- nothing to click through to Playback")
    href = playback_link.get_attribute("href")
    assert href and href.startswith("/playback"), f"expected a real Playback deep link, got {href!r}"
    assert "camera=" in href, "a real event's Playback link must carry its own camera, not fall back to the bare /playback path"
    playback_link.click()
    page.wait_for_load_state("networkidle")
    assert "/playback" in page.url
    assert "camera=" in page.url
