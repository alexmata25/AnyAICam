"""Runtime enforcement of Talk Down and AAC Voice Call (2026-10-05, Codex
review of 41d2af4): reaching an endpoint or trigger is not enough -- the
account must hold the entitlement, checked on the cloud where billing lives.

Ordinary Talk Down: an active AI Local / Hybrid plan, or a held Talk Down
add-on (Basic Local, legacy). AAC Voice Call: never from a plan; only a held
package or grant for voice_call. Ended/suspended plans and cancelled add-ons
stop counting. The appliance is sent Voice Call entrance cameras and the
alarm talk-down switch only for an entitled account.
"""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from database_backend import override_target
from test_aac_voice_call_cloud_edge_flow import (  # noqa: F401 -- fixtures and helpers
    _auth_headers, _cloud_rows, _seed_cloud, aac_voice_call, appliance_cloud, cloud_client, cloud_db, connection,
    greeting_provider, relay_provider, store)

PRICES = {"basic_local": "price_e_basic", "ai_local": "price_e_ai", "hybrid": "price_e_hybrid"}


@pytest.fixture(autouse=True)
def cloud_role(monkeypatch):
    import per_camera_billing as billing
    import stripe_state
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    for plan_key, price in PRICES.items():
        monkeypatch.setenv(billing.PRICE_ENV[plan_key], price)
    monkeypatch.setattr(stripe_state, "subscription_payment_reversal", lambda _s: None)


def _plan(cloud_db, plan_key, status="active"):
    import per_camera_billing as billing
    with override_target(sqlite_path=str(cloud_db)):
        billing._upsert_current({"id": "sub_e", "customer": "cus_e", "status": "active", "metadata": {"anyaicam_customer_id": "cust-1"},
                                 "items": {"data": [{"id": "si", "quantity": 2, "price": {"id": PRICES[plan_key]}}]}})
        if status != "active":
            with connection() as db:
                db.execute("UPDATE camera_plan_entitlements_v2 SET status=? WHERE customer_id='cust-1'", (status,))


def _addon(cloud_db, addon_key="talk_down", status="active"):
    import analytics_entitlements as analytics
    with override_target(sqlite_path=str(cloud_db)):
        analytics.upsert_addon_subscription(customer_id="cust-1", addon_key=addon_key, status=status, stripe_subscription_id=f"sub_{addon_key}")


def _grant(cloud_db, analytic_key):
    import analytics_entitlements as analytics
    with override_target(sqlite_path=str(cloud_db)):
        analytics.upsert_analytics_subscription(customer_id="cust-1", analytic_key=analytic_key, status="active")


def _legacy(cloud_db):
    with override_target(sqlite_path=str(cloud_db)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)


def _entitled(cloud_db, feature):
    import feature_entitlements
    with override_target(sqlite_path=str(cloud_db)):
        return feature_entitlements.customer_entitled("cust-1", feature)


# ------------------------------------------------------------ who is entitled

@pytest.mark.parametrize("setup,talk_down,voice_call", [
    ([], False, False),
    ([("plan", "basic_local")], False, False),
    ([("plan", "basic_local"), ("addon", "talk_down")], True, True),
    ([("plan", "ai_local")], True, False),
    ([("plan", "hybrid")], True, False),
    ([("plan", "ai_local", "cancelled")], False, False),
    ([("plan", "hybrid", "suspended")], False, False),
    ([("legacy",)], False, False),
    ([("legacy",), ("addon", "talk_down")], True, True),
    ([("plan", "ai_local"), ("grant", "voice_call")], True, True),
    ([("plan", "basic_local"), ("addon", "talk_down", "cancelled")], False, False),
])
def test_entitlement_matrix(cloud_db, setup, talk_down, voice_call):
    _seed_cloud(cloud_db)
    for step in setup:
        if step[0] == "plan":
            _plan(cloud_db, *step[1:])
        elif step[0] == "addon":
            _addon(cloud_db, *step[1:])
        elif step[0] == "grant":
            _grant(cloud_db, step[1])
        else:
            _legacy(cloud_db)
    assert _entitled(cloud_db, "talk_down") is talk_down
    assert _entitled(cloud_db, "voice_call") is voice_call


def test_an_entitlement_read_failure_denies(cloud_db, monkeypatch):
    import analytics_entitlements
    _seed_cloud(cloud_db)
    _plan(cloud_db, "ai_local")

    def broken(*_a, **_k):
        raise RuntimeError("store unavailable")
    monkeypatch.setattr(analytics_entitlements, "account_wide_feature_active", broken)
    assert _entitled(cloud_db, "talk_down") is False


def test_an_appliance_runtime_does_not_enforce_billing_it_does_not_hold(monkeypatch):
    import feature_entitlements
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    assert feature_entitlements.allowed("cust-unknown", "talk_down") is True


# ------------------------------------------------------------ customer routes

@pytest.fixture()
def customer_client(cloud_db):
    import aac_voice_call as vc
    import partner_portal
    import security_portal
    import talk_audio_relay
    import talk_sessions
    _seed_cloud(cloud_db)
    with override_target(sqlite_path=str(cloud_db)):
        app = FastAPI()
        talk_sessions.register_talk_session_routes(app)
        talk_audio_relay.register_talk_audio_relay_routes(app)
        security_portal.register_security_routes(app, lambda *a, **k: "")
        vc.register_aac_voice_call_routes(app, lambda *a, **k: "")
        with TestClient(app) as client:
            client.cookies.set(partner_portal.SESSION_COOKIE,
                               partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None))
            yield client


def test_talk_start_requires_talk_down(customer_client, cloud_db):
    denied = customer_client.post("/api/customer/cameras/cam-1/talk/start")
    assert denied.status_code == 403 and "Talk Down" in denied.json()["detail"]
    _plan(cloud_db, "ai_local")
    allowed = customer_client.post("/api/customer/cameras/cam-1/talk/start")
    assert allowed.status_code != 403  # past the entitlement gate (camera/channel checks follow)
    _plan(cloud_db, "ai_local", status="cancelled")  # the plan ended
    assert customer_client.post("/api/customer/cameras/cam-1/talk/start").status_code == 403


def test_basic_local_talk_needs_the_purchased_add_on(customer_client, cloud_db):
    _plan(cloud_db, "basic_local")
    assert customer_client.post("/api/customer/cameras/cam-1/talk/start").status_code == 403
    _addon(cloud_db, "talk_down")
    assert customer_client.post("/api/customer/cameras/cam-1/talk/start").status_code != 403


def test_the_talk_audio_socket_closes_once_the_entitlement_is_gone(customer_client, cloud_db):
    from datetime import datetime, timedelta
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            db.execute("INSERT INTO customer_talk_sessions(id,customer_id,site_id,camera_id,user_id,requested_by,role,state,"
                       "requested_at,ended_at,expires_at) VALUES('talk-1','cust-1','site-1','cam-1',NULL,'owner@example.test',"
                       "'customer_owner','requested',?,NULL,?)",
                       (datetime.now().isoformat(), (datetime.now() + timedelta(minutes=5)).isoformat()))
    with pytest.raises(WebSocketDisconnect) as closed:
        with customer_client.websocket_connect("/api/customer/talk/sessions/talk-1/audio"):
            pass
    assert closed.value.code == 4403


def test_alarm_talk_down_cannot_be_switched_on_without_talk_down(customer_client, cloud_db):
    body = {"site_id": "site-1", "settings": {"talkdown_on_alarm": True, "talkdown_message": "Leave now"}}
    assert customer_client.put("/api/customer/security/settings", json=body).status_code == 403
    _plan(cloud_db, "hybrid")
    assert customer_client.put("/api/customer/security/settings", json=body).status_code == 200


def test_voice_call_configuration_and_answering_require_voice_call(customer_client, cloud_db):
    _plan(cloud_db, "ai_local")  # ordinary Talk Down only
    assert customer_client.post("/api/customer/aac/voice-call/entrance-cameras/cam-1?enabled=true").status_code == 403
    assert customer_client.post("/api/customer/aac/voice-call/entrance-cameras/cam-1?enabled=false").status_code == 200
    assert customer_client.post("/api/customer/aac/voice-call/events/evt-x/answer").status_code == 403
    _addon(cloud_db, "talk_down")  # the add-on that carries AAC Voice Call
    assert customer_client.post("/api/customer/aac/voice-call/entrance-cameras/cam-1?enabled=true").status_code == 200
    assert customer_client.post("/api/customer/aac/voice-call/events/evt-x/answer").status_code == 404  # past the gate


# ------------------------------------------------------------ triggers and the appliance

def _enable_entrance(cloud_db):
    with override_target(sqlite_path=str(cloud_db)):
        store.set_entrance_camera(customer_id="cust-1", camera_id="cam-1", enabled=True, configured_by="owner@example.test")


def test_an_appliance_reported_call_is_only_a_detection_without_voice_call(cloud_db):
    _seed_cloud(cloud_db)
    _enable_entrance(cloud_db)
    _plan(cloud_db, "hybrid")
    from datetime import datetime
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            for detection_id in ("det-1", "det-2"):  # the appliance's detections, as the ingest route stores them
                db.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,"
                           "event_timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (detection_id, "cust-1", "site-1", "appl-1", "cam-1", f"local-{detection_id}", "aac_voice_call",
                            datetime.now().isoformat(), datetime.now().isoformat()))
    with override_target(sqlite_path=str(cloud_db)):
        result = aac_voice_call.ingest_edge_visitor_event(customer_id="cust-1", camera_id="cam-1", detection_event_id="det-1",
                                                          event_timestamp=datetime.now().isoformat())
    assert result == {"status": "skipped", "skipped_reason": "not_entitled"}
    assert _cloud_rows(cloud_db, "SELECT id FROM aac_voice_call_events") == []
    _addon(cloud_db, "talk_down")
    with override_target(sqlite_path=str(cloud_db)):
        created = aac_voice_call.ingest_edge_visitor_event(customer_id="cust-1", camera_id="cam-1", detection_event_id="det-2",
                                                           event_timestamp=datetime.now().isoformat())
    assert created.get("event_id") and len(_cloud_rows(cloud_db, "SELECT id FROM aac_voice_call_events")) == 1


def test_cloud_person_detection_and_simulated_triggers_respect_voice_call(cloud_db):
    _seed_cloud(cloud_db)
    _enable_entrance(cloud_db)
    _plan(cloud_db, "ai_local")
    with override_target(sqlite_path=str(cloud_db)):
        assert aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")["skipped_reason"] == "not_entitled"
        with pytest.raises(HTTPException) as refused:
            aac_voice_call.trigger_visitor_event(customer_id="cust-1", camera_id="cam-1")
        assert refused.value.status_code == 403


def test_the_appliance_gets_entrance_cameras_and_alarm_talk_down_only_when_entitled(cloud_client, cloud_db):
    _seed_cloud(cloud_db)
    _enable_entrance(cloud_db)
    import security_modes
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            security_modes.save_settings(db, "cust-1", "site-1", {"talkdown_on_alarm": True, "talkdown_message": "Leave now"},
                                         actor="owner@example.test", valid_camera_ids={"cam-1"})

    def config():
        with override_target(sqlite_path=str(cloud_db)):
            body = cloud_client.get("/api/appliance/configuration", headers=_auth_headers()).json()
        return body["aac_voice_call"]["entrance_cameras"], body["security"]["sites"][0]["settings"]["talkdown_on_alarm"]

    assert config() == ([], False)  # no plan: nothing to greet with, no spoken alarm
    _plan(cloud_db, "hybrid")
    cameras, talkdown = config()
    assert cameras == [] and talkdown is True  # Talk Down included; Voice Call is not
    _addon(cloud_db, "talk_down")
    cameras, talkdown = config()
    assert [c["camera_id"] for c in cameras] == ["cam-1"] and talkdown is True
    _addon(cloud_db, "talk_down", status="cancelled")  # the Voice Call add-on ended
    assert config()[0] == []
