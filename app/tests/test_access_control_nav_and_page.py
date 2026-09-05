"""Access Control navigation item + /access-control placeholder page
(2026-09-05). Both are UI-only: the nav item is always listed for a
customer-portal identity with a "Coming soon" badge (never hidden), and
the page itself shows the same placeholder copy with no operational
controls. ACCESS_CONTROL_FEATURE/REMOTE_RELAY_FEATURE are reserved
names only -- _customer_feature_entitled() always returns False today,
and neither name is added to any LICENSE_PLAN_FEATURES tier, so no
existing customer becomes entitled just because these constants exist.
"""
from unittest.mock import MagicMock

import pytest

import partner_portal
import main


@pytest.fixture()
def access_control_page():
    route = next(r for r in main.app.routes if getattr(r, "path", None) == "/access-control")
    return route.endpoint


@pytest.fixture()
def rendered_html(access_control_page, monkeypatch):
    fake_identity = {"role": "customer_owner", "customer_id": "test-customer-render-only", "email": "x@example.test"}
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: fake_identity)
    fake_request = MagicMock()
    main.REQUEST_CONTEXT.set(fake_request)
    return access_control_page(fake_request)


def test_page_shows_placeholder_copy_no_operational_controls(rendered_html):
    assert "Access Control" in rendered_html
    assert "Manage doors, gates, relay outputs, and access-controlled entry points from AnyAiCam." in rendered_html
    assert rendered_html.count('<span class="coming">Coming soon</span>') == 1


def test_nav_item_present_between_playback_and_media_with_coming_soon_badge(rendered_html):
    assert 'href="/access-control"' in rendered_html
    nav_snippet = rendered_html[rendered_html.index('href="/access-control"'):][:250]
    assert "Access Control" in nav_snippet
    assert '<span class="nav-badge">Coming soon</span>' in nav_snippet


def test_reserved_feature_constants_and_always_false_entitlement():
    assert main.ACCESS_CONTROL_FEATURE == "access_control"
    assert main.REMOTE_RELAY_FEATURE == "remote_relay"
    assert main._customer_feature_entitled("any-customer-id", main.ACCESS_CONTROL_FEATURE) is False
    assert main._customer_feature_entitled(None, main.ACCESS_CONTROL_FEATURE) is False


def test_feature_names_not_added_to_any_existing_license_plan():
    for plan, features in main.LICENSE_PLAN_FEATURES.items():
        assert "access_control" not in features, f"access_control leaked into plan {plan}"
        assert "remote_relay" not in features, f"remote_relay leaked into plan {plan}"


def test_unauthenticated_request_redirects_to_login(access_control_page, monkeypatch):
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)
    from fastapi.responses import RedirectResponse
    response = access_control_page(MagicMock())
    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
