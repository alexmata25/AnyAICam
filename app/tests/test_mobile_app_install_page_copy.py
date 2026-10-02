"""Regression coverage for GET /mobile-app (2026-09-23):

The page's header "eyebrow" (the small kicker line above the h1, used
consistently across every other customer-facing page for real copy like
"Secure mobile access" or "Mobile security") was the literal internal
development phase codename "Phase 6D" -- a leftover from this feature's
own build-out that was never replaced with real customer-facing text.
Confirmed live on portal-staging.anyaicam.com. Fixed to "Get the app",
matching the plain, professional style of every other page's eyebrow.
"""

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_mobile_app_install_page_copy.db"


@pytest.fixture()
def http_client(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, base_url="https://portal-staging.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def test_internal_phase_codename_is_gone_from_the_customer_facing_page(http_client):
    response = http_client.get("/mobile-app", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "Phase 6D" not in response.text
    assert '<p class="eyebrow">Get the app</p>' in response.text
