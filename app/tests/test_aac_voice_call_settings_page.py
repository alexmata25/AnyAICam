"""Settings -> Visitor Voice Call page (2026-09-29): the first customer UI
for choosing entrance cameras, their greeting and greeting volume (the
owner-only APIs existed, but nothing used them)."""
from database_backend import override_target
from partner_db import connection

import aac_voice_call_events as store
import partner_portal
from test_aac_voice_call_door_unlock import (  # noqa: F401  (fixtures)
    _isolated_relay, _owner_cookie, _seed_tenant_with_door, _viewer_cookie, client, db_path,
)

URL = "/customer/voice-call-settings"


def _seed(db_path, customer_id="cust-a", viewer=False):
    _seed_tenant_with_door(db_path, customer_id=customer_id, camera_id=f"door-{customer_id}", partner_id=f"p-{customer_id}",
                           owner_email=f"owner-{customer_id}@example.test", fleet_size=3,
                           viewer_email=f"viewer-{customer_id}@example.test" if viewer else None)
    with override_target(sqlite_path=str(db_path)), connection() as db:
        db.execute("UPDATE cameras SET talk_down_supported=1 WHERE id=?", (f"door-{customer_id}",))
        db.execute("UPDATE cameras SET talk_down_supported=0, name='Driveway' WHERE id=?", (f"cam-{customer_id}-2",))
        db.execute("UPDATE cameras SET status='pending_installation', name='Camera 3' WHERE id=?", (f"cam-{customer_id}-3",))


def _get(client, cookie):
    return client.get(URL, cookies={partner_portal.SESSION_COOKIE: cookie})


def test_owner_sees_every_real_camera_with_its_speaker_capability(client, db_path):
    _seed(db_path)
    html = _get(client, _owner_cookie("cust-a", "owner-cust-a@example.test")).text
    assert "Visitor Voice Call" in html and "Front Door" in html and "Driveway" in html
    assert "Speaker detected" in html and "No speaker detected" in html
    assert "Camera 3" not in html  # an uninstalled placeholder slot is not a camera yet
    assert html.count('class="action-button vc-save"') == 2
    assert "Only the account owner" not in html


def test_enabled_camera_shows_its_greeting_and_volume(client, db_path):
    _seed(db_path)
    with override_target(sqlite_path=str(db_path)):
        store.set_entrance_camera(customer_id="cust-a", camera_id="door-cust-a", enabled=True)
        store.set_camera_greeting_text(customer_id="cust-a", camera_id="door-cust-a", greeting_text="Hi <there>")
        store.set_camera_greeting_volume(customer_id="cust-a", camera_id="door-cust-a", volume="low")
    html = _get(client, _owner_cookie("cust-a", "owner-cust-a@example.test")).text
    card = html[html.index('data-camera-id="door-cust-a"'):]
    card = card[:card.index("</article>")]
    assert 'class="vc-enabled" checked' in card and "hidden" not in card.split("vc-details")[1][:12]
    assert "Hi &lt;there&gt;" in card  # escaped, never raw HTML
    assert '<option value="low" selected>' in card


def test_viewer_sees_settings_read_only(client, db_path):
    _seed(db_path, viewer=True)
    html = _get(client, _viewer_cookie("cust-a", "viewer-cust-a@example.test")).text
    assert "Only the account owner can change these settings." in html
    assert "vc-save" not in html.split("<script>")[0]
    assert 'class="vc-enabled" disabled' in html


def test_other_tenants_cameras_never_appear(client, db_path):
    _seed(db_path, "cust-a")
    _seed(db_path, "cust-b")
    html = _get(client, _owner_cookie("cust-a", "owner-cust-a@example.test")).text
    assert "door-cust-b" not in html and "cam-cust-b" not in html


def test_non_customer_is_refused(client, db_path):
    assert client.get(URL).status_code == 403


def test_greeting_text_length_is_bounded_server_side(client, db_path):
    _seed(db_path)
    cookie = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-cust-a@example.test")}
    client.post("/api/customer/aac/voice-call/entrance-cameras/door-cust-a?enabled=true", cookies=cookie)
    too_long = client.post("/api/customer/aac/voice-call/entrance-cameras/door-cust-a/greeting", json={"greeting_text": "x" * 501}, cookies=cookie)
    assert too_long.status_code == 422
    ok = client.post("/api/customer/aac/voice-call/entrance-cameras/door-cust-a/greeting", json={"greeting_text": "Hello"}, cookies=cookie)
    assert ok.status_code == 200


def test_volume_api_is_owner_only_and_validated(client, db_path):
    _seed(db_path, viewer=True)
    owner = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-cust-a@example.test")}
    viewer = {partner_portal.SESSION_COOKIE: _viewer_cookie("cust-a", "viewer-cust-a@example.test")}
    client.post("/api/customer/aac/voice-call/entrance-cameras/door-cust-a?enabled=true", cookies=owner)
    url = "/api/customer/aac/voice-call/entrance-cameras/door-cust-a/greeting-volume"
    assert client.post(url, json={"volume": "high"}, cookies=viewer).status_code == 403
    assert client.post(url, json={"volume": "max"}, cookies=owner).status_code == 422
    response = client.post(url, json={"volume": "High"}, cookies=owner)
    assert response.status_code == 200 and response.json()["greeting_volume"] == "high"
    other = client.post("/api/customer/aac/voice-call/entrance-cameras/door-cust-b/greeting-volume", json={"volume": "low"}, cookies=owner)
    assert other.status_code == 404
