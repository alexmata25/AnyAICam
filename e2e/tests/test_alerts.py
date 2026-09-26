"""Target: Smart Alerts (the last remaining, previously-unexplored
customer-facing page from the requested sweep). Real route confirmed
from source (app/main.py: @app.get("/alerts") -> _render_customer_alerts()).
Real notification rows come from _customer_notifications(), written by
notification_engine.fanout_appliance_event() -- the same real data
source Dashboard's own Smart Alerts widget uses (see
PROJECT_CHECKPOINT.md's 2026-09-14 note on that widget).

Selectors confirmed from source (note: both the card <article> AND its
own "Mark read" <button> carry data-notification-id -- an unscoped
`[data-notification-id]` selector double-counts every unread card;
always scope to `article[data-notification-id]` for a real card count):
  <article class="feature-card[ alert-unread]" data-alert-camera
      data-notification-id data-read="0|1">
    ...<div class="dashboard-event-actions">
      <a class="download" href="/customer/cameras/{id}/live">Live view</a>
      <a class="download" href="{playback_href}">Playback</a>  (or a
      pending <span class="event-action-pending"> if no clip yet, same
      contract as Events)
      <button class="mark-alert-read" data-notification-id>Mark read</button>
    </div>
  </article>
  camera filters: #alerts-camera-filters (checkbox per camera, data-camera=<camera_number>)
  unread pill:    #alerts-unread-pill ("N unread · M alert(s)")
  bulk action:    #mark-all-alerts-read

IMPORTANT, deliberately not exercised here: "Mark read" and "Mark all
read" both perform a REAL mutating POST (/api/customer/notifications/
{id}/read, /api/customer/notifications/read-all) against this real
pilot customer's own account data -- this whole e2e suite has stayed
strictly read/navigate/assert-only against that account throughout the
session (see PROJECT_CHECKPOINT.md), so those two controls are only
ever checked for presence/correct disabled-state, never clicked.
"""
import pytest


@pytest.fixture
def alerts_page(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/alerts")
    page.wait_for_selector("#alerts-grid")
    return page


@pytest.mark.e2e
def test_alerts_page_loads_after_login(alerts_page):
    page = alerts_page
    assert page.url.endswith("/alerts")
    assert page.title() == "Alerts · AnyAiCam"


@pytest.mark.e2e
def test_alerts_shows_real_notifications_or_an_explicit_empty_state(alerts_page):
    """Never a silently blank grid: source always renders either real
    cards or the explicit "No alerts yet." empty-stage message."""
    page = alerts_page
    cards = page.locator("#alerts-grid article[data-notification-id]")
    empty_state = page.locator("#alerts-grid .empty-stage")
    assert cards.count() > 0 or empty_state.count() == 1, "expected either real alert cards or the explicit empty state, never a blank grid"


@pytest.mark.e2e
def test_alerts_unread_pill_shows_a_real_count_matching_the_rendered_cards(alerts_page):
    """Scoped to `article[data-notification-id]` specifically, not the
    bare `[data-notification-id]` attribute -- confirmed live that an
    unread card's own "Mark read" <button> also carries
    data-notification-id (source: _render_customer_alerts()), so the
    bare attribute selector double-counts every unread card. A first
    version of this test used the bare selector and failed (pill said
    100, selector counted 200) -- a test-selector bug, not an app bug.

    The grid is also capped to the first 100 notifications
    (notifications_list[:100] in source) while the pill's own total
    reflects the *full* notification count -- so the real, correct
    invariant is "cards == min(pill's total, 100)", not flat equality."""
    page = alerts_page
    cards = page.locator("#alerts-grid article[data-notification-id]")
    if cards.count() == 0:
        pytest.skip("no real alert cards rendered -- nothing to count")
    pill_text = page.locator("#alerts-unread-pill").text_content() or ""
    import re
    match = re.search(r"(\d+) unread .+? (\d+) alert", pill_text)
    assert match, f"expected the unread pill to show real 'N unread · M alert(s)' text, got {pill_text!r}"
    total_in_pill = int(match.group(2))
    assert cards.count() == min(total_in_pill, 100), (
        f"expected {min(total_in_pill, 100)} rendered cards (min of the pill's real total {total_in_pill} "
        f"and the page's own 100-card cap), got {cards.count()}"
    )


@pytest.mark.e2e
def test_unchecking_a_camera_filter_hides_that_cameras_alerts(alerts_page):
    page = alerts_page
    checkboxes = page.locator("#alerts-camera-filters input[type=checkbox]")
    if checkboxes.count() == 0:
        pytest.skip("no camera filter checkboxes rendered")
    first_checkbox = checkboxes.first
    camera_num = first_checkbox.get_attribute("data-camera")
    matching_cards_before = page.locator(f'[data-alert-camera="{camera_num}"]:visible').count()
    if matching_cards_before == 0:
        pytest.skip(f"no visible alerts for camera {camera_num} -- nothing to prove got hidden")
    first_checkbox.click()
    page.wait_for_timeout(300)
    matching_cards_after = page.locator(f'[data-alert-camera="{camera_num}"]:visible').count()
    assert matching_cards_after == 0, f"unchecking camera {camera_num}'s filter must hide all of its alert cards"


@pytest.mark.e2e
def test_mark_read_controls_exist_but_are_never_clicked_here(alerts_page):
    """Presence/shape only, deliberately not exercised -- see this
    file's own module docstring for why (a real mutating POST against
    the real pilot customer's own notification data)."""
    page = alerts_page
    unread_card = page.locator('#alerts-grid [data-read="0"]').first
    if unread_card.count() == 0:
        pytest.skip("no unread alert cards currently rendered -- nothing to check the control on")
    mark_read_button = unread_card.locator(".mark-alert-read")
    assert mark_read_button.count() == 1
    assert mark_read_button.is_enabled()
