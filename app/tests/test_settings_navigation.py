"""Settings navigation audit (see the category-by-category report handed
back alongside this file): regression coverage for every visible Settings
item across the two real Settings surfaces this app has --

  - /settings + /settings/{slug} (main.py): the admin/installer settings
    hub, gated by has_permission(user, "manage_settings") on the legacy
    VMS identity (current_user()). Ten categories: Cameras, Events &
    alerts, Recording, Analytics, Notifications, Users, Network, Storage,
    System, Integrations.
  - /customer-app-settings (customer_platform.py): the customer portal's
    own Settings entry (partner_identity()-gated), a single page ("Camera
    analytics and alerts"), not a ten-category grid -- a customer_owner/
    customer_viewer never reaches the admin hub above (its nav key,
    "settings", is never advertised to CUSTOMER_PORTAL_ROLES; theirs is
    the separate "customer-app-settings" nav key/route).

Confirms every hub tile is either a real, working link (Events & alerts)
or a genuinely non-clickable "Coming soon" tile (the other nine) -- never
a clickable item that goes nowhere -- and that navigating directly to any
of the nine by URL still renders a real, honest placeholder page (never
blank, never a 404, never an unrelated redirect).
"""

from html import escape as html_escape

import pytest

import customer_platform
import main
import partner_portal


class _StubRequest:
    """Minimal stand-in for a FastAPI Request -- just enough for the
    handful of attributes settings()/settings_detail() actually touch
    once current_user() is monkeypatched (record_audit() reads
    request.headers)."""

    headers: dict = {}


def _stub_request():
    return _StubRequest()


# =============================================================== pure slug/href integrity


def test_every_category_slug_is_unique():
    slugs = [main.slugify(name) for name, _description in main.SETTINGS_CATEGORIES]
    assert len(slugs) == len(set(slugs))


def test_settings_categories_match_the_audited_list():
    names = [name for name, _description in main.SETTINGS_CATEGORIES]
    assert names == [
        "Cameras", "Events & alerts", "Recording", "Analytics", "Notifications",
        "Users", "Network", "Storage", "System", "Integrations",
    ]


def test_events_alerts_is_the_only_implemented_category_today():
    # Documents current backend reality -- this test is meant to start
    # failing the moment a second category gets a real implementation,
    # as a reminder to update IMPLEMENTED_SETTINGS_CATEGORIES and this
    # audit rather than leaving a tile silently out of sync.
    assert main.IMPLEMENTED_SETTINGS_CATEGORIES == {"Events & alerts"}


# =============================================================== /settings hub


def _admin_user():
    return {"id": "admin-1", "role": "administrator", "enabled": True, "camera_ids": []}


def _unprivileged_user():
    return {"id": "viewer-1", "role": "viewer", "enabled": True, "camera_ids": []}


def test_hub_renders_every_category_name_and_description(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_user())
    result = main.settings(_stub_request())
    for name, description in main.SETTINGS_CATEGORIES:
        assert html_escape(name) in result
        assert html_escape(description) in result


def test_hub_gives_events_alerts_a_real_clickable_link(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_user())
    result = main.settings(_stub_request())
    assert 'href="/settings/events-alerts"' in result
    # It's a real <a>, not a disabled tile.
    assert 'aria-disabled="true"' not in result.split('href="/settings/events-alerts"')[0].rsplit("<a", 1)[-1]


@pytest.mark.parametrize("name", [
    "Cameras", "Recording", "Analytics", "Notifications", "Users", "Network", "Storage", "System", "Integrations",
])
def test_hub_marks_every_unimplemented_category_disabled_not_a_dead_link(monkeypatch, name):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_user())
    result = main.settings(_stub_request())
    slug = main.slugify(name)
    # No clickable item should do nothing: an unimplemented category must
    # never appear as a real <a href="/settings/{slug}"> the way Events &
    # alerts does -- it must be a non-clickable, explicitly-labeled tile.
    assert f'href="/settings/{slug}"' not in result
    assert f'aria-disabled="true" title="Coming soon"><div><strong>{name}</strong>' in result


def test_hub_denies_role_without_manage_settings(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _unprivileged_user())
    result = main.settings(_stub_request())
    # Never leaks the category grid to an unauthorized role -- check the
    # categories' own descriptions (unlike "Cameras", these never collide
    # with an unrelated sidebar nav label every page already renders).
    for name, description in main.SETTINGS_CATEGORIES:
        assert html_escape(description) not in result
        assert f'href="/settings/{main.slugify(name)}"' not in result


# =============================================================== /settings/{slug} detail route


def test_events_alerts_detail_route_renders_the_real_working_page(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_user())
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [1, 2, 3])
    result = main.settings_detail("events-alerts", _stub_request())
    assert 'id="motion-settings-form"' in result
    assert 'id="alert-rule-form"' in result
    assert "Read-only preview" not in result  # this one is real, not a placeholder


@pytest.mark.parametrize("name", [
    "Cameras", "Recording", "Analytics", "Notifications", "Users", "Network", "Storage", "System", "Integrations",
])
def test_unimplemented_category_detail_renders_an_honest_placeholder_never_blank(monkeypatch, name):
    """The exact behavior the audit needed to confirm: clicking through to
    an unimplemented category (whether via a future direct link, a
    bookmark, or someone typing the URL) must never land on a blank page,
    a 404, or an unrelated redirect -- it must render a real, honestly-
    labeled placeholder naming the category itself."""
    monkeypatch.setattr(main, "current_user", lambda request: _admin_user())
    slug = main.slugify(name)
    result = main.settings_detail(slug, _stub_request())
    assert result.strip() != ""
    assert name in result
    assert "Read-only preview" in result
    assert "Settings category not found" not in result


def test_unknown_slug_shows_a_real_not_found_message_not_blank(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_user())
    result = main.settings_detail("not-a-real-category", _stub_request())
    assert "Settings category not found" in result


def test_detail_route_denies_role_without_manage_settings(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _unprivileged_user())
    result = main.settings_detail("events-alerts", _stub_request())
    assert "motion-settings-form" not in result


# =============================================================== navigation: nav-key -> route -> role wiring


def test_settings_nav_key_reaches_admin_portal_roles():
    for role in ("administrator", "admin", "support_admin"):
        assert "settings" in main.navigation_keys_for_role(role)


def test_settings_nav_key_is_never_advertised_to_customer_portal_roles():
    # The admin hub's own href ("/settings") would 403 a customer-portal
    # identity (current_user() never recognizes a partner_identity()
    # session) -- it must not appear as a clickable nav item for them.
    for role in ("customer_owner", "customer_viewer"):
        assert "settings" not in main.navigation_keys_for_role(role)


def test_customer_app_settings_nav_key_reaches_customer_portal_roles():
    for role in ("customer_owner", "customer_viewer"):
        assert "customer-app-settings" in main.navigation_keys_for_role(role)


def test_nav_items_hrefs_match_the_routes_this_file_exercises():
    nav_by_key = {key: href for key, href, *_rest in main.NAV_ITEMS}
    assert nav_by_key["settings"] == "/settings"
    assert nav_by_key["customer-app-settings"] == "/customer-app-settings"


# =============================================================== /customer-app-settings (the customer portal's own Settings entry)


def _owner_cookie(customer_id="cust-set-1"):
    return partner_portal._token(f"owner-{customer_id}@example.test", "customer_owner", None, customer_id, None)


@pytest.fixture()
def customer_settings_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import sqlite3
    from database_backend import override_target
    from partner_db import initialize_database

    monkeypatch.setattr(customer_platform, "FEATURES_FILE", tmp_path / "customer_camera_features.json")
    monkeypatch.setattr(customer_platform, "ALERTS_FILE", tmp_path / "customer_camera_alerts.json")
    db_path = tmp_path / "test_settings_nav.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
            conn.execute(
                "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) "
                "VALUES('cust-set-1','partner-1','Test Co','test@example.com','active','2026-01-01')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                "VALUES('owner-set-1','partner-1','owner-cust-set-1@example.test','Owner','customer_owner','x',1,'cust-set-1','2026-01-01')"
            )
            conn.commit()
        with TestClient(main.app) as test_client:
            yield test_client


def test_customer_app_settings_route_renders_for_a_real_customer_owner(customer_settings_client):
    response = customer_settings_client.get(
        "/customer-app-settings", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}
    )
    assert response.status_code == 200
    assert "Camera analytics and alerts" in response.text


def test_customer_app_settings_is_not_the_ten_category_admin_grid(customer_settings_client):
    # Guards against the two Settings surfaces silently merging in a way
    # that would put unimplemented admin categories (Users, Network,
    # Storage, System, Integrations...) in front of a customer identity.
    response = customer_settings_client.get(
        "/customer-app-settings", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}
    )
    for name in ("Network", "Storage", "System", "Integrations"):
        assert name not in response.text
