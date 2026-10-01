"""The floating AACO button must never cover the setup wizard's controls
(2026-09-30: on a 390px phone, and then on desktop at 1280x800, it sat on
the right edge of "Save and continue"). The setup page gives the button a
place in its own action row (data-aaco-dock-slot); the widget moves it there
at every width. AACO stays available; other pages keep the floating button.

The server-rendered checks always run. The real-browser geometry checks are
opt-in: ANYAICAM_RUN_BROWSER_TESTS=1."""
import os

import pytest

from test_customer_password_reset_browser import BROWSERS, EMAIL, _launch, playwright_instance, server  # noqa: F401
from test_customer_ui_empty_states_browser import PASSWORD, signed_in_customer  # noqa: F401

browser_only = pytest.mark.skipif(
    os.environ.get("ANYAICAM_RUN_BROWSER_TESTS") != "1",
    reason="browser validation is opt-in (ANYAICAM_RUN_BROWSER_TESTS=1)",
)
VIEWPORTS = {"phone": {"width": 390, "height": 844}, "small-phone": {"width": 360, "height": 640},
             "desktop": {"width": 1280, "height": 800}}


def test_widget_moves_the_button_into_a_page_dock_slot():
    from aaco_web import render_aaco_floating_widget
    html = render_aaco_floating_widget()
    assert "document.querySelector('[data-aaco-dock-slot]')" in html
    assert "slot.appendChild(root);root.classList.add('aaco-docked');" in html
    assert ".aaco-float-root.aaco-docked{position:static" in html
    assert 'id="aaco-float-toggle"' in html  # still rendered: AACO stays available


def test_setup_action_row_has_the_dock_slot():
    import inspect
    import partner_workspace
    assert ('<div class="dialog-actions"><span data-aaco-dock-slot style="margin-right:auto;display:inline-flex"></span>'
            '<button class="ghost-button" id="customer-setup-back" hidden>Back</button>') in inspect.getsource(partner_workspace)


def _overlaps(a, b) -> bool:
    return not (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
                or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])


def _sign_in(browser, origin, viewport):
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    page.goto(f"{origin}/customer-login.html")
    page.fill("#email", EMAIL)
    page.fill("#password", PASSWORD)
    page.press("#password", "Enter")
    page.wait_for_url(lambda url: "customer-login" not in url, timeout=15000)
    return page


@browser_only
@pytest.mark.parametrize("viewport", list(VIEWPORTS), ids=list(VIEWPORTS))
@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_aaco_button_never_covers_setup_controls(signed_in_customer, playwright_instance, engine, channel, viewport):  # noqa: F811
    origin = signed_in_customer["origin"]
    browser = _launch(playwright_instance, engine, channel)
    try:
        page = _sign_in(browser, origin, VIEWPORTS[viewport])
        page.goto(f"{origin}/customer/setup")
        page.wait_for_selector(".dialog-actions .aaco-float-root.aaco-docked #aaco-float-toggle")
        for step in range(1, 8):
            page.evaluate(f"document.querySelector('#customer-setup-tabs [data-step=\"{step}\"]').click()")
            page.wait_for_timeout(250)
            height = page.evaluate("document.documentElement.scrollHeight")
            for top in range(0, height, 200):  # every scroll position a customer can reach
                page.evaluate(f"window.scrollTo(0,{top})")
                toggle = page.locator("#aaco-float-toggle").bounding_box()
                controls = page.evaluate("""() => [...document.querySelectorAll(
                    '.dialog-actions button:not(#aaco-float-toggle), .customer-setup-step:not([hidden]) button, .customer-setup-step:not([hidden]) select, #customer-setup-tabs button')]
                    .filter(e => e.offsetParent).map(e => {const r = e.getBoundingClientRect();
                    return {id: e.id || e.textContent.trim(), x: r.x, y: r.y, width: r.width, height: r.height}})""")
                for control in controls:
                    if toggle and control["width"] and control["height"]:
                        assert not _overlaps(toggle, control), (viewport, step, top, control["id"])
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        page.click("#aaco-float-toggle")  # AACO is still one tap away
        assert page.is_visible("#aaco-float-panel")
    finally:
        browser.close()


@browser_only
@pytest.mark.parametrize("engine,channel", BROWSERS[:1], ids=["chromium"])
def test_pages_without_a_slot_keep_the_floating_button(signed_in_customer, playwright_instance, engine, channel):  # noqa: F811
    origin = signed_in_customer["origin"]
    browser = _launch(playwright_instance, engine, channel)
    try:
        page = _sign_in(browser, origin, {"width": 1280, "height": 800})
        page.goto(f"{origin}/events")
        page.wait_for_selector("#aaco-float-toggle", state="attached")
        assert page.evaluate("getComputedStyle(document.querySelector('.aaco-float-root')).position") == "fixed"
    finally:
        browser.close()
