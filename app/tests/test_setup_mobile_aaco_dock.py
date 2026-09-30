"""The floating AACO button must never cover the setup wizard's controls on a
phone (2026-09-30, staging at 390x844: it sat on the right edge of "Save and
continue"). Pages marked data-aaco-dock="inline" get the button docked below
the content on narrow screens; AACO stays available.

The server-rendered checks always run. The real-browser geometry check is
opt-in: ANYAICAM_RUN_BROWSER_TESTS=1."""
import os
import sqlite3

import pytest

from test_customer_password_reset_browser import BROWSERS, EMAIL, _launch, playwright_instance, server  # noqa: F401
from test_customer_ui_empty_states_browser import PASSWORD, signed_in_customer  # noqa: F401

browser_only = pytest.mark.skipif(
    os.environ.get("ANYAICAM_RUN_BROWSER_TESTS") != "1",
    reason="browser validation is opt-in (ANYAICAM_RUN_BROWSER_TESTS=1)",
)
PHONES = {"phone": {"width": 390, "height": 844}, "small-phone": {"width": 360, "height": 640}}


def test_widget_css_docks_the_button_inline_on_narrow_marked_pages():
    from aaco_web import render_aaco_floating_widget
    html = render_aaco_floating_widget()
    assert '@media (max-width:760px){body:has([data-aaco-dock="inline"]) .aaco-float-root{position:static' in html
    assert 'id="aaco-float-toggle"' in html  # still rendered: AACO stays available


def test_setup_wizard_is_marked_for_the_inline_dock():
    import inspect
    import partner_workspace
    assert '<section class="panel" data-aaco-dock="inline"><nav class="workspace-tabs" id="customer-setup-tabs"' in inspect.getsource(partner_workspace)


def _overlaps(a, b) -> bool:
    return not (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
                or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])


@browser_only
@pytest.mark.parametrize("viewport", list(PHONES), ids=list(PHONES))
@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_aaco_button_never_covers_setup_controls_on_a_phone(signed_in_customer, playwright_instance, engine, channel, viewport):  # noqa: F811
    origin = signed_in_customer["origin"]
    browser = _launch(playwright_instance, engine, channel)
    try:
        context = browser.new_context(viewport=PHONES[viewport])
        page = context.new_page()
        page.goto(f"{origin}/customer-login.html")
        page.fill("#email", EMAIL)
        page.fill("#password", PASSWORD)
        page.press("#password", "Enter")
        page.wait_for_url(lambda url: "customer-login" not in url, timeout=15000)
        page.goto(f"{origin}/customer/setup")
        page.wait_for_selector("#aaco-float-toggle", state="attached")
        for step in range(1, 8):
            page.evaluate(f"document.querySelector('#customer-setup-tabs [data-step=\"{step}\"]').click()")
            page.wait_for_timeout(250)
            # Check every scroll position a customer can reach.
            height = page.evaluate("document.documentElement.scrollHeight")
            for top in range(0, height, 200):
                page.evaluate(f"window.scrollTo(0,{top})")
                toggle = page.locator("#aaco-float-toggle").bounding_box()
                controls = page.evaluate("""() => [...document.querySelectorAll(
                    '.dialog-actions button, .customer-setup-step:not([hidden]) button, .customer-setup-step:not([hidden]) select, #customer-setup-tabs button')]
                    .filter(e => e.offsetParent).map(e => {const r = e.getBoundingClientRect();
                    return {id: e.id || e.textContent.trim(), x: r.x, y: r.y, width: r.width, height: r.height}})""")
                for control in controls:
                    if toggle and control["width"] and control["height"]:
                        assert not _overlaps(toggle, control), (viewport, step, top, control["id"])
        # AACO is still reachable: scroll to it and open it.
        page.locator("#aaco-float-toggle").scroll_into_view_if_needed()
        page.click("#aaco-float-toggle")
        assert page.is_visible("#aaco-float-panel")
    finally:
        browser.close()


@browser_only
@pytest.mark.parametrize("engine,channel", BROWSERS[:1], ids=["chromium"])
def test_desktop_keeps_the_floating_button(signed_in_customer, playwright_instance, engine, channel):  # noqa: F811
    origin = signed_in_customer["origin"]
    browser = _launch(playwright_instance, engine, channel)
    try:
        page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
        page.goto(f"{origin}/customer-login.html")
        page.fill("#email", EMAIL)
        page.fill("#password", PASSWORD)
        page.press("#password", "Enter")
        page.wait_for_url(lambda url: "customer-login" not in url, timeout=15000)
        page.goto(f"{origin}/customer/setup")
        assert page.evaluate("getComputedStyle(document.querySelector('.aaco-float-root')).position") == "fixed"
    finally:
        browser.close()
