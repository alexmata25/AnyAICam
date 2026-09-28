"""Notification email deep links (2026-09-27, found from real emails).

Bug 1: clicking "View event video" in a detection email landed on the
Dashboard. The click comes from another site (the mail client), so the
SameSite=Strict session cookie is not sent; the protected page bounced
to /customer-login.html?next=<unencoded path> -- losing &event=...&
autoplay=... -- and the login page, finding the session valid on that
now same-site hop, always went to the Dashboard and ignored next=.

Bug 2: clicking "Open the visitor call" in a Voice Call email landed on
the local emergency sign-in page: /aac/voice-call/ was not a known
customer path on the cloud.

Real HTTP through the real app, same fixtures as
test_cloud_customer_auth_routing.py."""
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

import main
import notification_email
from test_cloud_customer_auth_routing import _csrf_headers, db_path, http_client  # noqa: F401

NOW = "2026-09-27T20:00:00"
PASSWORD_A = "customer a long passphrase"
PASSWORD_B = "customer b long passphrase"


def _seed_tenant(conn, customer_id, email, password):
    from partner_db import password_hash

    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (NOW,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                 (customer_id, "partner-1", f"Customer {customer_id}", f"billing-{customer_id}@example.test", "active", NOW))
    site_id, appliance_id, camera_id = f"site-{customer_id}", f"app-{customer_id}", f"cam-{customer_id}"
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Home", NOW))
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
                 (appliance_id, customer_id, site_id, f"cloud-{customer_id}", NOW))
    conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?,?)",
                 (camera_id, customer_id, site_id, appliance_id, "Front Door", "configured", 5, NOW))
    conn.execute("INSERT OR IGNORE INTO partner_users(id,partner_id,email,role,customer_id,password_hash,camera_access_mode,approved,account_status,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (f"user-{customer_id}", "partner-1", email, "customer_owner", customer_id, password_hash(password), "all", 1, "active", NOW))
    return site_id, camera_id


@pytest.fixture()
def tenants(http_client, db_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    conn = sqlite3.connect(db_path)
    site_a, cam_a = _seed_tenant(conn, "cust-a", "owner-a@example.test", PASSWORD_A)
    site_b, cam_b = _seed_tenant(conn, "cust-b", "owner-b@example.test", PASSWORD_B)
    conn.commit()
    conn.close()
    import aac_voice_call_events as store

    call_a = store.create_voice_call_event(customer_id="cust-a", site_id=site_a, camera_id=cam_a, trigger_source="detection")
    return {"cam_a": cam_a, "cam_b": cam_b, "call_a": call_a}


def _email_path(context):
    return notification_email.event_path(context)


def _event_link(camera_id, event_id="evt-a-1"):
    return _email_path({"event_type": "person", "camera_id": camera_id, "event_id": event_id,
                        "timestamp": NOW, "has_clip": True})


def _login(client, email, password, next_path=None):
    body = {"email": email, "password": password, "customer_only": True}
    if next_path is not None:
        body["next"] = next_path
    return client.post("/api/partner-login", json=body, headers=_csrf_headers(client))


def _destination(response):
    if response.status_code == 303:
        return response.headers["location"]
    body = response.json()
    return body.get("destination") or body.get("redirect") or body.get("url")


def _login_redirect_next(response):
    """What the login page's own URLSearchParams(...).get('next') reads."""
    assert response.status_code == 303, response.status_code
    location = response.headers["location"]
    assert location.startswith("/customer-login.html?"), location
    return parse_qs(urlsplit(location).query)["next"][0]


# ---------------------------------------------------------------- the links themselves

def test_event_email_links_to_the_exact_camera_and_event_clip(tenants):
    link = _event_link(tenants["cam_a"], "evt-a-1")
    query = parse_qs(urlsplit(link).query)
    assert urlsplit(link).path == "/playback"
    assert query["camera"] == [tenants["cam_a"]] and query["event"] == ["evt-a-1"] and query["autoplay"] == ["event"]


def test_voice_call_email_links_to_that_call_screen(tenants):
    assert _email_path({"event_type": "aac_voice_call", "event_id": tenants["call_a"], "camera_id": tenants["cam_a"]}) == f"/aac/voice-call/{tenants['call_a']}"


# ---------------------------------------------------------------- event email

def test_authenticated_event_email_click_opens_the_exact_event(http_client, tenants):  # noqa: F811
    assert _login(http_client, "owner-a@example.test", PASSWORD_A).status_code in (200, 303)
    page = http_client.get(_event_link(tenants["cam_a"], "evt-a-1"))
    assert page.status_code == 200
    assert 'const initialEventId="evt-a-1"' in page.text  # Playback opens and autoplays that exact event clip
    assert 'const autoplayFromEvent=true' in page.text
    assert f'playback-camera-tile active" data-camera-id="{tenants["cam_a"]}"' in page.text  # on that camera


def test_unauthenticated_event_email_click_returns_to_the_exact_event_after_login(http_client, tenants):  # noqa: F811
    link = _event_link(tenants["cam_a"], "evt-a-1")
    next_path = _login_redirect_next(http_client.get(link))
    assert next_path == link  # the WHOLE destination survives, event= and autoplay= included
    assert _destination(_login(http_client, "owner-a@example.test", PASSWORD_A, next_path)) == link
    page = http_client.get(link)
    assert page.status_code == 200 and 'const initialEventId="evt-a-1"' in page.text


# ---------------------------------------------------------------- voice call email

def test_authenticated_voice_call_click_opens_that_cameras_live_view(http_client, tenants):  # noqa: F811
    assert _login(http_client, "owner-a@example.test", PASSWORD_A).status_code in (200, 303)
    page = http_client.get(f"/aac/voice-call/{tenants['call_a']}")
    assert page.status_code == 200
    assert f'/customer/cameras/{tenants["cam_a"]}/live' in page.text  # the live feed of the calling camera
    assert "voice-call-answer" in page.text  # with the visitor-call controls


def test_unauthenticated_voice_call_click_goes_to_customer_login_then_back(http_client, tenants):  # noqa: F811
    link = f"/aac/voice-call/{tenants['call_a']}"
    response = http_client.get(link)
    assert not response.headers["location"].startswith("/login"), "must never land on the local emergency sign-in"
    next_path = _login_redirect_next(response)
    assert next_path == link
    assert _destination(_login(http_client, "owner-a@example.test", PASSWORD_A, next_path)) == link
    page = http_client.get(link)
    assert page.status_code == 200 and f'/customer/cameras/{tenants["cam_a"]}/live' in page.text


def test_voice_call_path_keeps_the_local_login_on_an_edge_appliance(http_client, tenants, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
    assert http_client.get(f"/aac/voice-call/{tenants['call_a']}").headers["location"].startswith("/login?next=")


# ---------------------------------------------------------------- already signed in, arriving from the mail client

def test_login_page_sends_an_already_signed_in_customer_to_next_not_the_dashboard():
    """customer-login.html: the Strict session cookie is not sent on the
    click from the mail client, so the customer is bounced to the login
    page while signed in. The page must continue to next=."""
    import re
    from pathlib import Path

    html = (Path(main.__file__).parent / "customer-login.html").read_text(encoding="utf-8")
    branch = html[html.index("if(session.experience==='customer')"):]
    branch = branch[: branch.index("return}") + 7]
    assert "get('next')" in branch and "session.portal_url" in branch
    guard = re.search(r"/\^(.+?)\$/\.test\(wanted\)", branch).group(1)
    pattern = re.compile("^" + guard.replace("\\/", "/") + "$")
    for good in ("/playback?camera=c&event=e&autoplay=event", "/aac/voice-call/abc"):
        assert pattern.match(good), good
    for bad in ("https://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)", "", "/a b"):
        assert not pattern.match(bad), bad


@pytest.mark.parametrize("bad", ["https://evil.example/x", "//evil.example", "/\\evil.example", "/\\/evil.example", "/ok\nSet-Cookie:x"])
def test_login_never_redirects_off_origin(http_client, tenants, bad):  # noqa: F811
    destination = _destination(_login(http_client, "owner-a@example.test", PASSWORD_A, bad))
    assert destination != bad and destination.startswith("/") and not destination.startswith("//") and "\\" not in destination


# ---------------------------------------------------------------- tenant isolation

def test_customer_b_cannot_open_customer_as_voice_call_by_editing_the_link(http_client, tenants):  # noqa: F811
    assert _login(http_client, "owner-b@example.test", PASSWORD_B).status_code in (200, 303)
    response = http_client.get(f"/aac/voice-call/{tenants['call_a']}")
    assert response.status_code == 404
    assert tenants["cam_a"] not in response.text


def test_customer_b_cannot_open_customer_as_camera_or_event_by_editing_the_link(http_client, tenants):  # noqa: F811
    assert _login(http_client, "owner-b@example.test", PASSWORD_B).status_code in (200, 303)
    assert http_client.get(f"/customer/cameras/{tenants['cam_a']}/live").status_code in (403, 404)
    media = http_client.get(f"/api/customer/events/{tenants['cam_a']}/evt-a-1/media/url")
    assert media.status_code in (403, 404)
    page = http_client.get(_event_link(tenants["cam_a"], "evt-a-1"))
    assert tenants["cam_a"] not in page.text  # B's Playback never lists or opens A's camera


def test_login_next_to_another_tenants_page_still_cannot_reach_it(http_client, tenants):  # noqa: F811
    link = f"/aac/voice-call/{tenants['call_a']}"
    assert _destination(_login(http_client, "owner-b@example.test", PASSWORD_B, link)) == link  # next is only a path...
    assert http_client.get(link).status_code == 404  # ...the page itself still enforces the tenant


# ---------------------------------------------------------------- call screen audio line follows the camera's real capability

def test_call_screen_audio_line_reflects_the_cameras_talk_capability(http_client, tenants, db_path):  # noqa: F811
    assert _login(http_client, "owner-a@example.test", PASSWORD_A).status_code in (200, 303)
    page = http_client.get(f"/aac/voice-call/{tenants['call_a']}").text
    assert "does not support two-way audio" in page and "not enabled on this deployment" not in page
    con = sqlite3.connect(db_path)
    con.execute("UPDATE cameras SET talk_down_supported=1 WHERE id=?", (tenants["cam_a"],))
    con.commit()
    con.close()
    page = http_client.get(f"/aac/voice-call/{tenants['call_a']}").text
    assert "Press and hold Talk" in page and "not confirmed to reach" not in page
