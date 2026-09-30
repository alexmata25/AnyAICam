"""Customer mobile bottom bar includes Investigate (owner request, 2026-09-30)."""
import re

import partner_portal
from test_live_view_black_tile_fix import client, db_path, _owner_cookie  # noqa: F401  (shared fixtures)


def _mobile_bar(html):
    match = re.search(r'<nav class="mobile-nav" aria-label="Mobile">(.*?)</nav>', html, re.S)
    assert match, "mobile bar missing"
    return match.group(1)


def test_customer_mobile_bar_has_investigate(client):
    bar = _mobile_bar(client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text)
    labels = re.findall(r'href="([^"]+)">([^<]+)</a>', bar)
    assert ("/investigate", "Investigate") in labels
    order = [label for _, label in labels]
    assert order.index("Playback") < order.index("Investigate") < order.index("Account")


def test_investigate_page_marks_its_tab_active(client):
    response = client.get("/investigate", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert '<a class="active" href="/investigate">Investigate</a>' in _mobile_bar(response.text)
