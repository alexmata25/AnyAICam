"""Armed security end to end (2026-09-28): arm state sync to the edge,
the rule worker's armed gating and talk-down, the emergency notification
path (quiet hours / cooldown / event-type selection bypass, SMS setting),
the alarm email's live link and Call 911 dialer link, the portal's
owner-only mode control, and the Live page alarm banner. Normal events
are never elevated to an alarm."""
import contextlib
import json
import sqlite3

import pytest

from database_backend import override_target
from partner_db import initialize_database

with override_target(sqlite_path="/tmp/test_security_alarm_wiring_import.db"):
    import notification_engine
    from partner_db import connection

import customer_analytics_rule_worker as worker
import notification_email
import security_modes
import security_rules

LINE = [{"x": 0.5, "y": 0.0}, {"x": 0.5, "y": 1.0}]  # "inbound" protects the left side
SEC_RULE = {"id": "sec-1", "analytic_type": "security_line", "name": "Back fence", "direction": "inbound", "geometry": LINE}


# ---------------------------------------------------------------- edge: sync + worker

@pytest.fixture()
def edge_db(tmp_path, monkeypatch):
    con = sqlite3.connect(tmp_path / "edge.db")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE cameras(id TEXT PRIMARY KEY, customer_id TEXT, site_id TEXT, appliance_id TEXT)")
    con.execute("INSERT INTO cameras VALUES('yard','cust-1','site-1','appl-1')")
    con.execute("INSERT INTO cameras VALUES('living','cust-1','site-1','appl-1')")
    con.execute("INSERT INTO cameras VALUES('elsewhere','cust-2','site-9','appl-2')")

    @contextlib.contextmanager
    def _conn():
        yield con
    monkeypatch.setattr(worker, "connection", _conn)
    security_rules.reset_state()
    yield con
    security_rules.reset_state()
    con.close()


def _cloud_security(mode, **settings):
    return {"sites": [{"customer_id": "cust-1", "site_id": "site-1", "mode": mode, "changed_at": "2026-09-28T12:00:00", "settings": settings}]}


def test_edge_mirrors_only_its_own_sites_and_ignores_bad_modes(edge_db):
    from edge_camera_sync import _reconcile_security
    payload = _cloud_security("away")
    payload["sites"] += [{"customer_id": "cust-2", "site_id": "site-9", "mode": "away"},
                         {"customer_id": "cust-1", "site_id": "site-1", "mode": "panic"}, "junk"]
    assert _reconcile_security(edge_db, "appl-1", payload) == 1
    assert security_modes.get_state(edge_db, "cust-1", "site-1")["mode"] == "away"
    assert security_modes.get_state(edge_db, "cust-2", "site-9")["mode"] == "disarmed"


def person(x_left, track="t1", cls="person"):
    return {"track_id": track, "class_name": cls, "confidence": 0.9, "x": x_left, "y": 600, "width": 60, "height": 200}


def _walk_in(camera_id, cls="person"):
    fired = []
    for i, x in enumerate([700, 650, 520, 400, 380, 360], 1):
        fired += worker.evaluate_security_lines(1, camera_id, [SEC_RULE], [person(x, cls=cls)], 1000, 1000, now=float(i * 2))
    return fired


def test_disarmed_site_never_alarms(edge_db):
    assert _walk_in("yard") == []


def test_armed_away_alarms_once_with_mode_and_talkdown_text(edge_db):
    security_modes.store_synced_state(edge_db, "cust-1", "site-1", "away",
                                      {"talkdown_on_alarm": True, "talkdown_message": "Leave now."}, None)
    fired = _walk_in("yard")
    assert len(fired) == 1
    alarm = fired[0]
    assert alarm["analytic_type"] == "intrusion_alarm" and alarm["security_mode"] == "away"
    assert alarm["customer_id"] == "cust-1" and alarm["talkdown_text"] == "Leave now."


def test_armed_stay_only_arms_the_chosen_cameras(edge_db):
    security_modes.store_synced_state(edge_db, "cust-1", "site-1", "stay", {"stay_camera_ids": ["yard"]}, None)
    assert _walk_in("living") == []
    assert len(_walk_in("yard")) == 1


def test_cars_and_animals_never_raise_an_intrusion_alarm(edge_db):
    security_modes.store_synced_state(edge_db, "cust-1", "site-1", "away", {}, None)
    assert _walk_in("yard", cls="car") == []
    assert _walk_in("yard", cls="dog") == []


def test_security_lines_are_not_evaluated_as_ordinary_line_crossings():
    assert worker.event_type_for("intrusion_alarm") == "intrusion_alarm"
    assert worker.event_type_for("line_crossing") == "line_crossing"


class _FakeProvider:
    def __init__(self):
        self.requests = []

    def speak(self, request):
        self.requests.append(request)
        from aac_voice_call_greeting import GreetingResult
        return GreetingResult(camera_id=request.camera_id, delivered=True)


def test_talkdown_speaks_the_customers_warning_once_per_cycle():
    provider = _FakeProvider()
    fired = [{"analytic_type": "intrusion_alarm", "customer_id": "cust-1", "talkdown_text": "Leave now."},
             {"analytic_type": "intrusion_alarm", "customer_id": "cust-1", "talkdown_text": "Leave now."}]
    assert worker.speak_alarm_talkdowns("yard", fired, ["e1", "e2"], provider=provider) == 1
    (request,) = provider.requests
    assert request.text == "Leave now." and request.reason == "intrusion_alarm_talkdown" and request.event_id == "e1"


def test_no_talkdown_unless_enabled_or_for_ordinary_events():
    provider = _FakeProvider()
    assert worker.speak_alarm_talkdowns("yard", [{"analytic_type": "intrusion_alarm"}], ["e1"], provider=provider) == 0
    assert worker.speak_alarm_talkdowns("yard", [{"analytic_type": "line_crossing", "talkdown_text": "x"}], ["e1"], provider=provider) == 0
    assert provider.requests == []


def test_edge_does_not_forward_a_second_notification_for_an_alarm(monkeypatch):
    import analytics_sync
    posts = []
    monkeypatch.setattr(analytics_sync, "_control_plane_post", lambda path, payload: posts.append(path) or {"status": "accepted"})
    analytics_sync._forward_notification({"id": "e1", "event_type": "intrusion_alarm", "timestamp": "2026-09-28T12:00:00"}, "yard")
    assert posts == []
    analytics_sync._forward_notification({"id": "e2", "event_type": "person", "timestamp": "2026-09-28T12:00:00"}, "yard")
    assert posts == ["/api/appliance/events"]


# ---------------------------------------------------------------- cloud: notifications

@pytest.fixture()
def cloud_db(tmp_path):
    with override_target(sqlite_path=tmp_path / "cloud.db"):
        initialize_database()
        yield


class _FakeChannel:
    def __init__(self, status="sent"):
        self.status = status
        self.calls = []

    def send(self, notification, recipient):
        self.calls.append((notification, recipient))
        return {"channel": "fake", "status": self.status, "provider": "fake", "error": None}


@pytest.fixture()
def channels(monkeypatch):
    fakes = {"in_app": _FakeChannel("stored"), "email": _FakeChannel(), "sms": _FakeChannel()}
    monkeypatch.setattr(notification_engine, "CHANNELS", fakes)
    return fakes


def _seed_customer(quiet_hours=True, event_types=("person",)):
    now = "2026-09-28T00:00:00"
    with connection() as db:
        db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (now,))
        db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C','c@example.test','active','real',?)", (now,))
        db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Home',?)", (now,))
        db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1',?)", (now,))
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES('yard','cust-1','site-1','appl-1','Yard',?)", (now,))
        db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                   "VALUES('owner-1','p1','owner@example.test','Owner','customer_owner','x',1,'cust-1',?)", (now,))
        db.execute(
            "INSERT INTO customer_notification_channels(user_id,customer_id,email_address,email_enabled,phone_number,sms_enabled,event_types_json,camera_scope,quiet_hours_enabled,quiet_start,quiet_end,delivery_mode,updated_at) "
            "VALUES('owner-1','cust-1','owner@example.test',1,'+15555550100',1,?,'all',?,'00:00','23:59','immediate',?)",
            (json.dumps(list(event_types)), int(quiet_hours), now),
        )


def _fan(event_type, event_id):
    return notification_engine.fanout_appliance_event(
        {"customer_id": "cust-1", "site_id": "site-1"},
        {"id": event_id, "camera_id": "yard", "event_type": event_type, "timestamp": "2026-09-28T03:00:00"},
    )


def test_an_alarm_reaches_email_and_sms_through_quiet_hours_and_back_to_back(cloud_db, channels):
    _seed_customer()
    assert _fan("intrusion_alarm", "a1") == 1
    assert _fan("intrusion_alarm", "a2") == 1  # no 5-minute spacing for emergencies
    assert len(channels["email"].calls) == 2 and len(channels["sms"].calls) == 2
    with connection() as db:
        severities = {r["severity"] for r in db.execute("SELECT severity FROM notifications WHERE event_type='intrusion_alarm'")}
    assert severities == {"critical"}


def test_a_normal_person_event_is_not_elevated(cloud_db, channels):
    _seed_customer()  # quiet hours cover the whole day
    _fan("person", "p1")
    assert channels["email"].calls == [] and channels["sms"].calls == []
    with connection() as db:
        assert db.execute("SELECT severity FROM notifications WHERE event_id='p1'").fetchone()["severity"] == "info"


def test_the_customer_can_turn_off_alarm_sms(cloud_db, channels):
    _seed_customer(quiet_hours=False)
    with connection() as db:
        security_modes.save_settings(db, "cust-1", "site-1", {"notify_sms": False}, actor="owner", valid_camera_ids={"yard"})
    _fan("intrusion_alarm", "a1")
    assert len(channels["email"].calls) == 1 and channels["sms"].calls == []


def test_alarm_fanout_message_links_to_live_camera(monkeypatch):
    import appliance_cloud
    monkeypatch.setattr(notification_email, "public_base_url", lambda: "https://portal.example.test")
    payload = appliance_cloud._fanout_payload({"name": "Yard"}, "evt-9", "yard", "intrusion_alarm", "2026-09-28T03:00:00", None)
    assert payload["severity"] == "critical"
    assert payload["message"] == "INTRUSION ALARM at Yard. Open the live camera: https://portal.example.test/customer/cameras/yard/live?alarm=evt-9"
    plain = appliance_cloud._fanout_payload({"name": "Yard"}, "evt-1", "yard", "person", "t", None)
    assert "severity" not in plain and plain["message"] is None


def test_alarm_email_opens_live_camera_and_offers_a_911_dialer_link():
    context = {"event_type": "intrusion_alarm", "event_id": "evt-9", "camera_id": "yard", "title": "INTRUSION ALARM",
               "camera_name": "Yard", "timestamp": "2026-09-28T03:00:00", "message": "A person crossed"}
    assert notification_email.event_path(context) == "/customer/cameras/yard/live?alarm=evt-9"
    email = notification_email.build_alert_email(context, base_url="https://portal.example.test")
    assert "Open live camera: https://portal.example.test/customer/cameras/yard/live?alarm=evt-9" in email["text"]
    assert "tel:911" in email["text"] and 'href="tel:911"' in email["html"]
    normal = notification_email.build_alert_email(dict(context, event_type="person"), base_url="https://portal.example.test")
    assert "911" not in normal["text"] and "911" not in normal["html"]


# ---------------------------------------------------------------- cloud: portal + live banner

def test_only_the_owner_can_change_the_mode(cloud_db):
    import security_portal
    from fastapi import HTTPException
    _seed_customer()
    owner = {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.test"}
    viewer = dict(owner, role="customer_viewer")
    with pytest.raises(HTTPException):
        security_portal._require_owner(viewer)
    with connection() as db:
        seen_by_viewer = security_portal.security_overview(db, viewer)
        assert seen_by_viewer["mode"] == "disarmed" and not seen_by_viewer["can_change"] and "settings" not in seen_by_viewer
        security_modes.set_mode(db, "cust-1", "site-1", "away", actor="owner@example.test")
        seen_by_owner = security_portal.security_overview(db, owner)
    assert seen_by_owner["mode_label"] == "Armed Away" and seen_by_owner["cameras"][0]["armed_in_away"] is True
    control = security_portal.security_mode_control("away", can_change=False)
    assert control.count("disabled") == 3 and "Arm Stay" in control and "Arm Away" in control and "Disarm" in control


def test_a_customer_cannot_see_another_customers_site(cloud_db):
    import security_portal
    from fastapi import HTTPException
    _seed_customer()
    with connection() as db, pytest.raises(HTTPException):
        security_portal.security_overview(db, {"role": "customer_owner", "customer_id": "cust-1"}, "someone-elses-site")


class _Req:
    def __init__(self, alarm):
        self.query_params = {"alarm": alarm} if alarm else {}


def test_live_page_banner_only_for_this_customers_alarm(cloud_db):
    import live_view_page
    _seed_customer()
    with connection() as db:
        for event_id, event_type in (("alarm-1", "intrusion_alarm"), ("person-1", "person")):
            db.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", (event_id, "cust-1", "site-1", "appl-1", "yard", event_id, event_type, "2026-09-28T03:00:00", "2026-09-28T03:00:01"))
    owner = {"customer_id": "cust-1"}
    banner = live_view_page._intrusion_alarm_banner(_Req("alarm-1"), "yard", owner)
    assert "INTRUSION ALARM" in banner and 'href="tel:911"' in banner and "talk-mic" in banner
    assert live_view_page._intrusion_alarm_banner(_Req("person-1"), "yard", owner) == ""
    assert live_view_page._intrusion_alarm_banner(_Req("alarm-1"), "yard", {"customer_id": "cust-2"}) == ""
    assert live_view_page._intrusion_alarm_banner(_Req(None), "yard", owner) == ""


def test_a_security_line_must_protect_one_side():
    from fastapi import HTTPException
    import customer_analytics_rules as rules
    points, direction = rules._validate_geometry("security_line", LINE, "outbound")
    assert direction == "outbound" and len(points) == 2
    with pytest.raises(HTTPException):
        rules._validate_geometry("security_line", LINE, "both")
    assert rules._validate_geometry("line_crossing", LINE, "both")[1] == "both"  # ordinary lines unchanged


def test_dashboard_panel_is_empty_for_non_customer_sessions(monkeypatch):
    import security_portal
    monkeypatch.setattr(security_portal, "partner_identity", lambda request: {"role": "partner_admin"})
    assert security_portal.dashboard_security_panel(object()) == ""
