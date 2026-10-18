"""The sign-in pages link only to pages that exist (Green 2026-10-17).

partner.html linked "/privacy-policy.html", "/terms.html" and "/contact" as portal paths; app.anyaicam.com has none of
them (404). They now point at the website, as customer-login.html already did. The partner sales page linked
"/onboarding", which has no route; it now opens the partner onboarding wizard (/partner/onboarding).
"""
import re
from pathlib import Path

import pytest
from starlette.routing import Match

import main

APP_DIR = Path(main.__file__).resolve().parent


def _routes_match(path: str) -> bool:
    scope = {"type": "http", "path": path, "method": "GET", "root_path": "", "query_string": b"", "headers": []}
    return any(route.matches(scope)[0] == Match.FULL for route in main.app.routes)


@pytest.mark.parametrize("page", ["partner.html", "customer-login.html"])
def test_every_portal_link_on_the_sign_in_pages_has_a_route(page):
    html = (APP_DIR / page).read_text(encoding="utf-8")
    paths = sorted({href.split("?")[0].split("#")[0] for href in re.findall(r'href="(/[^"]*)"', html)})
    assert paths, "no portal links found"
    missing = [path for path in paths if not _routes_match(path)]
    assert missing == [], f"{page} links to portal paths with no route: {missing}"


def test_the_legal_and_contact_links_go_to_the_website():
    html = (APP_DIR / "partner.html").read_text(encoding="utf-8")
    for url in ("https://anyaicam.com/privacy-policy.html", "https://anyaicam.com/terms.html",
                "https://anyaicam.com/vms-contact.html"):
        assert f'href="{url}"' in html


def test_partner_sales_onboarding_links_open_the_partner_onboarding_wizard():
    source = (APP_DIR / "main.py").read_text(encoding="utf-8")
    assert 'href="/onboarding"' not in source
    assert source.count('href="/partner/onboarding"') >= 2
    assert _routes_match("/partner/onboarding")
    assert not _routes_match("/onboarding")
