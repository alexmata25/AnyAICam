"""Premium analytics 168 h offline grace (owner 2026-10-19; RED-ANALYTICS-OFFLINE-168H-DESIGN-20261019.md with Green
F1/F2). Real Ed25519 signatures, the real verifier, real MAC'd state files; the clock is passed explicitly.

Covers: boundaries at 24 h, 167 h 59 m, 168 h and beyond; Talk Down / Voice Call still 24 h; reboot (state reload);
reconnect/renewal; revocation by a successful sync (cancellation, downgrade, camera removal, absent document, invalid
document, older document -- Green F1); replay; forged / attacker-key / wrong-type / other-appliance / over-long-window /
future-issued documents; clock rollback and recovery; corrupt / missing / MAC-tampered state and a missing app secret;
per-camera and per-feature isolation; the cloud cap at the earliest recorded authorization end (Green F2).
"""
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import appliance_entitlements as ae
import appliance_identity

IDENTITY = {"appliance_id": "appl-1", "cloud_id": "AIC-1", "customer_id": "cust-1"}
T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _key():
    private = Ed25519PrivateKey.generate()
    return (base64.b64encode(private.private_bytes_raw()).decode(),
            base64.b64encode(private.public_key().public_bytes_raw()).decode())


CLOUD_PRIV, CLOUD_PUB = _key()
ATTACKER_PRIV, _ATTACKER_PUB = _key()


@pytest.fixture(autouse=True)
def appliance(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYAICAM_APP_SECRETS", "appliance-secret-for-tests")
    monkeypatch.setattr(ae, "ANALYTICS_STATE_FILE", tmp_path / "analytics_entitlements.json")
    monkeypatch.setattr(ae, "STATE_FILE", tmp_path / "feature_entitlements.json")
    monkeypatch.setattr(ae, "_trusted_keys", lambda: {"cloud-1": CLOUD_PUB})
    monkeypatch.setattr(ae, "_local_identity", lambda: dict(IDENTITY))
    ae._grant_cache.update({"at": None, "mtime": None, "grants": None, "expires": None})
    yield tmp_path


def doc(grants=None, *, issued=T0, window=timedelta(hours=168), key=CLOUD_PRIV, key_id="cloud-1", **override):
    body = {"type": "anyaicam.analytics_entitlements", "version": 1, **IDENTITY,
            "camera_analytics": {"cam-1": ["lpr", "smart_motion"]} if grants is None else grants,
            "issued_at": issued.isoformat(), "expires_at": (issued + window).isoformat()}
    body.update(override)
    return {**body, "signature": appliance_identity.sign_body(body, key_id=key_id, private_key_b64=key)}


def grants_at(when):
    return ae.camera_analytic_grants(now=when)


def stored_at(document, when=T0):
    return ae.store_analytics_grant(document, now=when)


# ---------------------------------------------------------------- boundaries
@pytest.mark.parametrize("offset,allowed", [
    (timedelta(0), True), (timedelta(hours=24), True), (timedelta(hours=24, minutes=1), True),
    (timedelta(hours=167, minutes=59), True), (timedelta(hours=168), False), (timedelta(hours=168, seconds=1), False),
    (timedelta(hours=200), False), (timedelta(days=30), False)])
def test_offline_window_boundaries(offset, allowed):
    assert stored_at(doc()) == "stored"
    # the high-water advances as time is observed (an appliance that stays up)
    for step in range(0, int(offset.total_seconds() // 3600) + 1, 12):
        grants_at(T0 + timedelta(hours=step))
    result = grants_at(T0 + offset)
    assert (result == {"cam-1": frozenset({"lpr", "smart_motion"})}) is allowed


def test_talk_down_and_voice_call_keep_their_24_hour_expiry():
    import feature_entitlements
    assert feature_entitlements.DEFAULT_SNAPSHOT_TTL_HOURS == 24
    body = {"type": "anyaicam.feature_entitlements", "version": 1, **IDENTITY,
            "features": {"talk_down": True, "voice_call": True}, "camera_capacity": {},
            "issued_at": T0.isoformat(), "expires_at": (T0 + timedelta(hours=24)).isoformat()}
    snap = {**body, "signature": appliance_identity.sign_body(body, key_id="cloud-1", private_key_b64=CLOUD_PRIV)}
    assert ae.verify(snap, {"cloud-1": CLOUD_PUB}, identity=IDENTITY, now=T0 + timedelta(hours=23, minutes=59)) is None
    assert ae.verify(snap, {"cloud-1": CLOUD_PUB}, identity=IDENTITY, now=T0 + timedelta(hours=24, minutes=1)) == "expired"
    # ... while premium analytics continue at 25 h
    assert stored_at(doc()) == "stored"
    for h in (12, 25):
        assert grants_at(T0 + timedelta(hours=h)) == {"cam-1": frozenset({"lpr", "smart_motion"})}


def test_reboot_reloads_the_grant_from_disk():
    assert stored_at(doc()) == "stored"
    ae._grant_cache.update({"at": None, "mtime": None, "grants": None, "expires": None})   # a new process
    assert grants_at(T0 + timedelta(hours=100)) == {"cam-1": frozenset({"lpr", "smart_motion"})}


# ---------------------------------------------------------------- renewal and revocation by a successful sync
def test_reconnect_renews_the_window():
    assert stored_at(doc()) == "stored"
    assert stored_at(doc(issued=T0 + timedelta(hours=160)), T0 + timedelta(hours=160)) == "stored"
    assert grants_at(T0 + timedelta(hours=300)) == {"cam-1": frozenset({"lpr", "smart_motion"})}   # 140 h after renewal


@pytest.mark.parametrize("revoked", [{}, {"cam-1": ["smart_motion"]}, {"cam-2": ["lpr"]}],
                         ids=["cancellation", "downgrade_drops_lpr", "camera_removed"])
def test_connected_revocation_takes_effect_at_the_next_sync(revoked):
    assert stored_at(doc()) == "stored"
    assert stored_at(doc(revoked, issued=T0 + timedelta(minutes=1)), T0 + timedelta(minutes=1)) == "stored"
    assert "lpr" not in grants_at(T0 + timedelta(minutes=2)).get("cam-1", frozenset())


def test_a_successful_sync_without_a_document_clears_the_grant():
    assert stored_at(doc()) == "stored"
    assert stored_at(None, T0 + timedelta(minutes=1)) == "cleared:absent"
    assert grants_at(T0 + timedelta(minutes=2)) == {}


def test_an_invalid_document_from_sync_clears_the_previous_grant():
    assert stored_at(doc()) == "stored"
    assert stored_at(doc(key=ATTACKER_PRIV), T0 + timedelta(minutes=1)).startswith("cleared:rejected:bad_signature")
    assert grants_at(T0 + timedelta(minutes=2)) == {}


def test_green_f1_an_older_document_from_sync_clears_and_never_keeps_the_broader_grant():
    assert stored_at(doc(issued=T0 + timedelta(hours=1)), T0 + timedelta(hours=1)) == "stored"
    revoking_but_older = doc({}, issued=T0)      # e.g. the cloud clock stepped back
    assert stored_at(revoking_but_older, T0 + timedelta(hours=1, minutes=1)) == "cleared:older_than_stored"
    assert grants_at(T0 + timedelta(hours=1, minutes=2)) == {}
    assert stored_at(doc(issued=T0 + timedelta(hours=2)), T0 + timedelta(hours=2)) == "stored"   # a newer one restores


def test_replayed_older_state_file_is_bounded_by_the_next_sync_and_its_own_signed_expiry():
    """DOCUMENTED LIMITATION (design §3): the MAC stops forged or edited state, but someone who can WRITE the state file
    can restore an older AUTHENTIC one. It is honoured only until the next successful sync (cleared, ~60 s online) and
    never beyond its own signed expires_at."""
    assert stored_at(doc(), T0) == "stored"
    old_state = ae.ANALYTICS_STATE_FILE.read_bytes()
    assert stored_at(doc({}, issued=T0 + timedelta(hours=1)), T0 + timedelta(hours=1)) == "stored"   # revoked
    ae.ANALYTICS_STATE_FILE.write_bytes(old_state)                     # restore the broader old (authentic) file
    ae._grant_cache.update({"at": None, "mtime": None, "grants": None, "expires": None})
    assert grants_at(T0 + timedelta(hours=2)) == {"cam-1": frozenset({"lpr", "smart_motion"})}   # honoured offline ...
    assert grants_at(T0 + timedelta(hours=168)) == {}                                            # ... never past expiry
    ae.ANALYTICS_STATE_FILE.write_bytes(old_state)
    assert stored_at(doc({}, issued=T0 + timedelta(hours=3)), T0 + timedelta(hours=3)) == "stored"   # next sync
    assert grants_at(T0 + timedelta(hours=3, minutes=1)) == {}


# ---------------------------------------------------------------- forgery and malformed documents
@pytest.mark.parametrize("variant,reason", [
    ("attacker_key", "bad_signature"), ("unknown_key_id", "bad_signature"), ("other_appliance", "other_appliance"),
    ("other_customer", "other_customer"), ("wrong_type", "unsupported"), ("window_169h", "window_too_long"),
    ("future_issued", "bad_time"), ("unknown_analytic", "malformed_grants"), ("tampered_after_signing", "bad_signature")])
def test_forged_or_malformed_documents_are_rejected(variant, reason):
    d = {"attacker_key": lambda: doc(key=ATTACKER_PRIV),
         "unknown_key_id": lambda: doc(key_id="nope"),
         "other_appliance": lambda: doc(appliance_id="appl-2"),
         "other_customer": lambda: doc(customer_id="cust-2"),
         "wrong_type": lambda: doc(type="anyaicam.feature_entitlements"),
         "window_169h": lambda: doc(window=timedelta(hours=169)),
         "future_issued": lambda: doc(issued=T0 + timedelta(hours=1)),
         "unknown_analytic": lambda: doc({"cam-1": ["guardian"]}),
         "tampered_after_signing": lambda: {**doc(), "camera_analytics": {"cam-1": ["lpr", "ppe"]}}}[variant]()
    assert stored_at(d, T0) == f"cleared:rejected:{reason}"
    assert grants_at(T0) == {}


def test_a_signed_document_cannot_be_used_beyond_168_hours_even_if_the_cloud_signed_more():
    over = doc(window=timedelta(hours=400))
    assert ae.verify_analytics(over, {"cloud-1": CLOUD_PUB}, identity=IDENTITY, now=T0) == "window_too_long"


# ---------------------------------------------------------------- clock and state integrity
def test_clock_rollback_denies_until_a_fresh_sync():
    assert stored_at(doc()) == "stored"
    grants_at(T0 + timedelta(hours=100))                       # high-water advances to +100 h
    assert grants_at(T0 + timedelta(hours=10)) == {}           # clock set back 90 h -> denied
    assert grants_at(T0 + timedelta(hours=100, minutes=1)) != {}   # clock corrected -> allowed again
    grants_at(T0 + timedelta(hours=150))
    assert grants_at(T0 + timedelta(hours=20)) == {}           # rolled back again
    # a real cloud document carries the TRUE time, which a rolled-back appliance clock sees as "in the future"
    assert stored_at(doc(issued=T0 + timedelta(hours=151)), T0 + timedelta(hours=20)) == "cleared:rejected:bad_time"
    assert grants_at(T0 + timedelta(hours=20, minutes=1)) == {}
    assert stored_at(doc(issued=T0 + timedelta(hours=152)), T0 + timedelta(hours=152)) == "stored"   # clock fixed
    assert grants_at(T0 + timedelta(hours=152, minutes=1)) != {}


def test_clock_skew_within_five_minutes_is_tolerated():
    assert stored_at(doc()) == "stored"
    grants_at(T0 + timedelta(hours=10))
    assert grants_at(T0 + timedelta(hours=10) - timedelta(minutes=4)) != {}


@pytest.mark.parametrize("damage", ["corrupt", "missing", "mac_tampered", "field_edited_mac_kept", "no_app_secret"])
def test_damaged_state_denies(damage, monkeypatch):
    assert stored_at(doc()) == "stored"
    f = ae.ANALYTICS_STATE_FILE
    if damage == "corrupt":
        f.write_text("{not json", encoding="utf-8")
    elif damage == "missing":
        f.unlink()
    elif damage == "mac_tampered":
        data = json.loads(f.read_text()); data["mac"] = "0" * 64; f.write_text(json.dumps(data))
    elif damage == "field_edited_mac_kept":
        data = json.loads(f.read_text()); data["high_water"] = T0.replace(year=2020).isoformat(); f.write_text(json.dumps(data))
    else:
        monkeypatch.delenv("ANYAICAM_APP_SECRETS")
    ae._grant_cache.update({"at": None, "mtime": None, "grants": None, "expires": None})
    assert grants_at(T0 + timedelta(hours=1)) == {}


def test_without_an_app_secret_nothing_can_be_stored(monkeypatch):
    monkeypatch.delenv("ANYAICAM_APP_SECRETS")
    assert stored_at(doc()) == "cleared:not_persisted"
    assert grants_at(T0) == {}


# ---------------------------------------------------------------- isolation
def test_per_camera_and_per_feature_isolation():
    assert stored_at(doc({"cam-1": ["lpr"], "cam-2": ["smart_motion"]})) == "stored"
    g = grants_at(T0 + timedelta(hours=1))
    assert g == {"cam-1": frozenset({"lpr"}), "cam-2": frozenset({"smart_motion"})}
    assert "smart_motion" not in g["cam-1"] and "lpr" not in g["cam-2"] and "cam-3" not in g


def test_pipeline_gate_preserves_basic_fields_and_the_customer_choice(monkeypatch):
    import recording_uploader
    assert stored_at(doc({"cam-1": ["lpr"]})) == "stored"
    monkeypatch.setattr(recording_uploader, "RUNTIME_ROLE", "edge")
    granted = grants_at(T0 + timedelta(hours=1))
    monkeypatch.setattr(ae, "camera_analytic_grants", lambda now=None: granted)
    monkeypatch.setitem(recording_uploader._camera_map, 7, {"camera_id": "cam-1", "site_id": "s", "cloud_recording_mode": "motion",
                                                            "lpr_enabled": False, "ppe_enabled": True, "analytics_lab_leases": {}})
    gated = recording_uploader._camera_identity(7)
    assert gated["lpr_enabled"] is False            # licensed but the customer's choice is OFF -> stays OFF
    assert gated["ppe_enabled"] is False            # ON but not licensed -> OFF
    assert gated["cloud_recording_mode"] == "motion" and gated["camera_id"] == "cam-1"


# ---------------------------------------------------------------- cloud signer (Green F2) and wire contract
def test_cloud_signer_caps_expiry_at_the_earliest_recorded_authorization_end(tmp_path, monkeypatch):
    import feature_entitlements
    from database_backend import override_target
    from partner_db import connection, initialize_database
    path = tmp_path / "cloud.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as db:
            end = int((datetime.now(timezone.utc) + timedelta(hours=30)).timestamp())
            db.execute("INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,"
                       "stripe_subscription_id,stripe_subscription_item_id,stripe_price_id,status,current_period_end,cancel_at_period_end,"
                       "local_retention_days,created_at,updated_at) VALUES('v2','cust-1','hybrid',4,'cus','sub','si','price','active',?,1,7,'x','x')",
                       (end,))
        appliance = {"id": "appl-1", "cloud_id": "AIC-1", "customer_id": "cust-1"}
        with_sm = feature_entitlements.analytics_entitlement_snapshot(appliance, {"cam-1": ["smart_motion", "lpr"]})
        lpr_only = feature_entitlements.analytics_entitlement_snapshot(appliance, {"cam-1": ["lpr"]})
        lab = feature_entitlements.analytics_entitlement_snapshot(appliance, {"cam-1": ["ppe"]},
                                                                  lab_expiries=[(datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()])
    window = lambda d: datetime.fromisoformat(d["expires_at"]) - datetime.fromisoformat(d["issued_at"])
    assert timedelta(hours=29) < window(with_sm) <= timedelta(hours=30)      # plan cancels at period end in 30 h
    assert window(lpr_only) == timedelta(hours=168)                          # add-ons record no end -> 168 h
    assert timedelta(hours=4) < window(lab) <= timedelta(hours=5)            # Test Lab lease end
    for d in (with_sm, lpr_only, lab):
        assert d["type"] == "anyaicam.analytics_entitlements" and d["version"] == 1 and "signature" in d


def test_cloud_signer_never_goes_below_ten_minutes_and_never_above_168_hours(tmp_path):
    import feature_entitlements
    from database_backend import override_target
    from partner_db import initialize_database
    with override_target(sqlite_path=str(tmp_path / "c.db")):
        initialize_database()
        d = feature_entitlements.analytics_entitlement_snapshot(
            {"id": "a", "cloud_id": "c", "customer_id": "x"}, {"cam": ["ppe"]},
            lab_expiries=[(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()])
    w = datetime.fromisoformat(d["expires_at"]) - datetime.fromisoformat(d["issued_at"])
    assert timedelta(minutes=10) <= w <= timedelta(minutes=10, seconds=1)
    assert feature_entitlements.ANALYTICS_OFFLINE_GRACE == timedelta(hours=168)
