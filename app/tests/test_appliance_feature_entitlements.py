"""Talk Down and AAC Voice Call on the appliance (2026-10-05, Codex review of
51d1346): an edge runtime may run a paid feature only with a valid,
cloud-signed entitlement snapshot for this appliance (appliance_entitlements
.py); an unknown runtime role never grants one; the ordinary VMS never
depends on it.

The snapshot comes from the real cloud configuration endpoint
(appliance_cloud.py, billing in the cloud database) and is stored by the real
edge configuration sync (edge_camera_sync.py). It is trusted only when signed
by a key in the installer-provisioned keyset (the trust anchor), which these
tests provision from the cloud's own public keys as a release would.
"""
import base64
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import appliance_entitlements
import feature_entitlements
from database_backend import override_target
from test_aac_voice_call_cloud_edge_flow import (  # noqa: F401 -- fixtures and helpers
    APPLIANCE_ID, IDENTITY, _auth_headers, _seed_cloud, appliance_cloud, cloud_client, cloud_db, connection,
    edge_camera_sync, edge_db, edge_env, greeting_provider, relay_provider, store)
from test_runtime_feature_enforcement import PRICES, _addon, _grant, _plan

pytestmark = pytest.mark.real_entitlements

# This appliance's persisted activation identity, as the cloud knows it
# (_seed_cloud names the appliance's cloud_id "AIC-<appliance id>").
EDGE_IDENTITY = {**IDENTITY, "cloud_id": f"AIC-{APPLIANCE_ID}"}

import appliance_activation  # noqa: E402
_REAL_LOAD_IDENTITY = appliance_activation.load_persisted_identity  # captured before any test patches it


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    import per_camera_billing as billing
    import stripe_state
    for plan_key, price in PRICES.items():
        monkeypatch.setenv(billing.PRICE_ENV[plan_key], price)
    monkeypatch.setattr(stripe_state, "subscription_payment_reversal", lambda _s: None)
    monkeypatch.delenv("ANYAICAM_CLOUD_SIGNING_PUBLIC_KEYS", raising=False)


def _cloud_public_keys(cloud_db) -> dict:
    """The cloud's public signing keys (what GET /api/appliance/signing-keys
    serves, and what a release packages as the trust anchor)."""
    import appliance_identity
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            appliance_identity.ensure_signing_key(db)
            return appliance_identity.active_public_keys(db)


def _provision_anchor(content):
    """Writes the trust-anchor keyset the installer would install (a dict is
    written as {"keys": ...}; a string is written as-is)."""
    path = appliance_entitlements.TRUST_ANCHOR_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps({"keys": content}), encoding="utf-8")


@pytest.fixture()
def appliance(cloud_client, cloud_db, edge_db, edge_env, monkeypatch):
    """A cloud with this appliance's customer, the appliance's own sync wired
    to the cloud's real configuration endpoint, and the trust anchor
    provisioned with the cloud's public keys. (Root ownership of the anchor
    is checked in production; temp files here are the test user's, so only
    that file-ownership check is bypassed -- ownership_problem() has its own
    tests below.)"""
    _seed_cloud(cloud_db)
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: dict(EDGE_IDENTITY))
    monkeypatch.setattr(appliance_entitlements, "_anchor_file_problem", lambda path: "" if path.exists() else f"{path} is missing")
    _provision_anchor(_cloud_public_keys(cloud_db))
    state = {"online": True, "rewrite": None}

    def cloud_get(path, appliance_id, credential):
        if not state["online"]:
            return None  # what _control_plane_get returns when the cloud is unreachable
        monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
        try:
            with override_target(sqlite_path=str(cloud_db)):
                response = cloud_client.get(path, headers=_auth_headers()).json()
        finally:
            monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
        # An attacker on the sync path (tests set state["rewrite"]).
        return state["rewrite"](response) if state["rewrite"] else response

    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", cloud_get)
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    return state


def _sync(edge_db):
    with override_target(sqlite_path=str(edge_db)):
        return edge_camera_sync.sync_provisioned_cameras()


def _edge_allows(feature, customer_id="cust-1"):
    return feature_entitlements.allowed(customer_id, feature)


def _cached():
    return json.loads(appliance_entitlements.STATE_FILE.read_text(encoding="utf-8"))


def _resign(snapshot, cloud_db, **changes):
    """A snapshot the real cloud key signed, with fields changed (to build an
    expired one, another appliance's, ...)."""
    import appliance_identity
    body = {key: value for key, value in snapshot.items() if key != "signature"}
    body.update(changes)
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            key = appliance_identity.ensure_signing_key(db)
    return {**body, "signature": appliance_identity.sign_body(body, key_id=key["key_id"], private_key_b64=key["private_key_b64"])}


# ------------------------------------------------------------ entitled / not

def test_an_entitled_appliance_runs_talk_down_and_voice_call(appliance, cloud_db, edge_db):
    _plan(cloud_db, "basic_local")
    _addon(cloud_db, "talk_down")  # grants talk_down and voice_call
    _sync(edge_db)
    snapshot = _cached()["snapshot"]
    assert snapshot["appliance_id"] == APPLIANCE_ID and snapshot["features"] == {"talk_down": True, "voice_call": True}
    assert snapshot["signature"]["alg"] == "Ed25519"
    assert _edge_allows("talk_down") and _edge_allows("voice_call")


def test_a_non_entitled_appliance_is_denied(appliance, cloud_db, edge_db):
    _plan(cloud_db, "basic_local")
    _sync(edge_db)
    assert _cached()["snapshot"]["features"] == {"talk_down": False, "voice_call": False}
    assert not _edge_allows("talk_down") and not _edge_allows("voice_call")


def test_an_appliance_that_never_synced_is_denied(appliance):
    assert not appliance_entitlements.STATE_FILE.exists()
    assert not _edge_allows("talk_down") and not _edge_allows("voice_call")


@pytest.mark.parametrize("ended", ["cancelled", "suspended"])
def test_a_cancelled_or_suspended_plan_is_denied_after_the_next_sync(appliance, cloud_db, edge_db, ended):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    assert _edge_allows("talk_down")
    _plan(cloud_db, "ai_local", status=ended)
    _sync(edge_db)
    assert not _edge_allows("talk_down")


def test_a_cancelled_add_on_is_denied_after_the_next_sync(appliance, cloud_db, edge_db):
    _plan(cloud_db, "basic_local")
    _addon(cloud_db, "talk_down")
    _sync(edge_db)
    assert _edge_allows("talk_down") and _edge_allows("voice_call")
    _addon(cloud_db, "talk_down", status="canceled")
    _sync(edge_db)
    assert not _edge_allows("talk_down") and not _edge_allows("voice_call")


# ------------------------------------------------------------ independent features

def test_talk_down_without_voice_call(appliance, cloud_db, edge_db):
    _plan(cloud_db, "hybrid")  # ordinary Talk Down, no Voice Call
    _sync(edge_db)
    assert _edge_allows("talk_down") and not _edge_allows("voice_call")


def test_voice_call_without_talk_down(appliance, cloud_db, edge_db):
    _plan(cloud_db, "basic_local")
    _grant(cloud_db, "voice_call")  # a direct grant: Voice Call only
    _sync(edge_db)
    assert _edge_allows("voice_call") and not _edge_allows("talk_down")


# ------------------------------------------------------------ stale, expired, invalid

def test_an_expired_snapshot_fails_closed(appliance, cloud_db, edge_db, monkeypatch):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    snapshot = _cached()["snapshot"]
    past = datetime.now(timezone.utc) - timedelta(hours=30)
    state = _cached()
    state["snapshot"] = _resign(snapshot, cloud_db, issued_at=past.isoformat(), expires_at=(past + timedelta(hours=24)).isoformat())
    appliance_entitlements.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    assert not _edge_allows("talk_down")


def test_a_snapshot_left_stale_by_an_outage_expires_after_its_ttl(appliance, cloud_db, edge_db):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    appliance["online"] = False
    assert _sync(edge_db) == {"status": "unreachable"}
    assert _edge_allows("talk_down")  # inside the TTL a short outage changes nothing
    expires = datetime.fromisoformat(_cached()["snapshot"]["expires_at"])
    issued = datetime.fromisoformat(_cached()["snapshot"]["issued_at"])
    assert expires - issued == timedelta(hours=feature_entitlements.DEFAULT_SNAPSHOT_TTL_HOURS)
    state = _cached()
    later = expires + timedelta(seconds=1)
    assert appliance_entitlements.verify(state["snapshot"], appliance_entitlements._trusted_keys(), identity=EDGE_IDENTITY,
                                         now=later) == "expired"


@pytest.mark.parametrize("tamper", [
    lambda s: s["features"].update(talk_down=True, voice_call=True),        # edited into a grant
    lambda s: s.update(expires_at="2099-01-01T00:00:00+00:00"),            # lifetime extended
    lambda s: s["signature"].update(value=s["signature"]["value"][::-1]),  # signature garbled
    lambda s: s["signature"].update(key_id="someone-elses-key"),           # unknown key
    lambda s: s.pop("signature"),                                          # unsigned
])
def test_an_edited_snapshot_fails_closed(appliance, cloud_db, edge_db, tamper):
    _plan(cloud_db, "basic_local")
    _sync(edge_db)
    state = _cached()
    tamper(state["snapshot"])
    appliance_entitlements.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    assert not _edge_allows("talk_down") and not _edge_allows("voice_call")


def test_another_appliances_snapshot_or_another_customer_is_denied(appliance, cloud_db, edge_db):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    assert not _edge_allows("talk_down", customer_id="cust-2")
    state = _cached()
    state["snapshot"] = _resign(state["snapshot"], cloud_db, appliance_id="appl-other")
    appliance_entitlements.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    assert not _edge_allows("talk_down")


def test_an_unreadable_cache_fails_closed(appliance, cloud_db, edge_db):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    appliance_entitlements.STATE_FILE.write_text("{not json", encoding="utf-8")
    assert not _edge_allows("talk_down")


def test_an_invalid_snapshot_from_the_cloud_clears_the_previous_grant(appliance, cloud_db, edge_db):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    good = _cached()["snapshot"]
    forged = {**good, "features": {"talk_down": True, "voice_call": True}}
    assert appliance_entitlements.store_snapshot(forged).startswith("rejected:bad_signature")
    assert "snapshot" not in _cached() and not _edge_allows("talk_down")


# ------------------------------------------------------------ trust anchor
# (staging finding on 1f66bcd: with no pinned key, keys delivered by config
# sync became trusted, so a snapshot signed with an attacker's key and that
# key supplied through sync turned Talk Down on.)

def _attacker():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private = Ed25519PrivateKey.generate()
    return (base64.b64encode(private.private_bytes_raw()).decode(),
            base64.b64encode(private.public_key().public_bytes_raw()).decode())


def _signed_by(body, private_b64, key_id):
    import appliance_identity
    body = {k: v for k, v in body.items() if k != "signature"}
    return {**body, "signature": appliance_identity.sign_body(body, key_id=key_id, private_key_b64=private_b64)}


def _forged_body(cloud_db):
    """An otherwise valid, entitled snapshot body for exactly this appliance's
    appliance_id, cloud_id and customer_id."""
    now = datetime.now(timezone.utc)
    return {"type": "anyaicam.feature_entitlements", "version": 1, "appliance_id": APPLIANCE_ID,
            "cloud_id": EDGE_IDENTITY["cloud_id"], "customer_id": "cust-1",
            "features": {"talk_down": True, "voice_call": True},
            "issued_at": now.isoformat(), "expires_at": (now + timedelta(hours=24)).isoformat()}


def _talk_denied_everywhere():
    import talk_audio_relay_client as client
    assert client._talk_entitled() is False
    for feature in ("talk_down", "voice_call"):
        assert appliance_entitlements.feature_allowed(feature) is False
        assert _edge_allows(feature) is False


def test_greens_attack_an_attacker_key_supplied_by_config_sync_is_never_trusted(appliance, cloud_db, edge_db):
    """1. no trust anchor; 2. sync supplies attacker key A; 3. a snapshot signed
    with A; 4. correct appliance/cloud/customer ids; 5. stored and used;
    6. rejected; 7. Talk Down stays denied."""
    appliance_entitlements.TRUST_ANCHOR_FILE.unlink()
    private_a, public_a = _attacker()
    forged = _signed_by(_forged_body(cloud_db), private_a, "attacker-a")
    appliance["rewrite"] = lambda response: {**response, "feature_entitlements": forged,
                                             "signing_public_keys": {"attacker-a": public_a}}
    assert _sync(edge_db)["status"] != "unreachable"  # the sync itself ran
    assert "snapshot" not in _cached() and "public_keys" not in _cached()
    assert appliance_entitlements.store_snapshot(forged) == "rejected:no_trust_anchor"
    _talk_denied_everywhere()


def test_an_attacker_key_from_sync_is_not_trusted_alongside_the_real_anchor(appliance, cloud_db, edge_db):
    private_a, public_a = _attacker()
    forged = _signed_by(_forged_body(cloud_db), private_a, "attacker-a")
    appliance["rewrite"] = lambda response: {**response, "feature_entitlements": forged,
                                             "signing_public_keys": {**response.get("signing_public_keys", {}), "attacker-a": public_a}}
    _sync(edge_db)
    assert "snapshot" not in _cached()
    _talk_denied_everywhere()


def test_an_attacker_key_reusing_the_legitimate_key_id_is_rejected(appliance, cloud_db, edge_db):
    legit_id = next(iter(_cloud_public_keys(cloud_db)))
    private_a, public_a = _attacker()
    forged = _signed_by(_forged_body(cloud_db), private_a, legit_id)  # claims to be the real key
    appliance["rewrite"] = lambda response: {**response, "feature_entitlements": forged, "signing_public_keys": {legit_id: public_a}}
    _sync(edge_db)
    assert "snapshot" not in _cached()
    _talk_denied_everywhere()


def test_sync_cannot_replace_or_override_the_trusted_key(appliance, cloud_db, edge_db):
    _plan(cloud_db, "basic_local")
    _addon(cloud_db, "talk_down")
    anchor_before = appliance_entitlements.TRUST_ANCHOR_FILE.read_bytes()
    legit_id = next(iter(_cloud_public_keys(cloud_db)))
    private_a, public_a = _attacker()
    appliance["rewrite"] = lambda response: {**response, "signing_public_keys": {legit_id: public_a, "attacker-a": public_a}}
    _sync(edge_db)  # the genuine snapshot still verifies against the real key
    assert _edge_allows("talk_down") and _edge_allows("voice_call")
    assert appliance_entitlements.TRUST_ANCHOR_FILE.read_bytes() == anchor_before  # never written by sync
    assert appliance_entitlements.store_snapshot(_signed_by(_forged_body(cloud_db), private_a, legit_id)) == "rejected:bad_signature"
    assert appliance_entitlements.store_snapshot(_signed_by(_forged_body(cloud_db), private_a, "attacker-a")) == "rejected:bad_signature"
    _talk_denied_everywhere()  # the rejected forgery also cleared the cached grant (fail closed)


def test_a_key_carried_only_inside_the_snapshot_is_not_trusted(appliance, cloud_db, edge_db):
    private_a, public_a = _attacker()
    body = {**_forged_body(cloud_db), "public_key": public_a, "signing_public_keys": {"attacker-a": public_a}}
    forged = _signed_by(body, private_a, "attacker-a")
    forged["signature"]["public_key"] = public_a
    assert appliance_entitlements.store_snapshot(forged) == "rejected:bad_signature"
    _talk_denied_everywhere()


def test_the_old_environment_pin_cannot_supply_a_key(appliance, cloud_db, edge_db, monkeypatch):
    """ANYAICAM_CLOUD_SIGNING_PUBLIC_KEYS lived in vms.env, which the
    unprivileged anyaicam user can write; it is no longer read."""
    appliance_entitlements.TRUST_ANCHOR_FILE.unlink()
    private_a, public_a = _attacker()
    monkeypatch.setenv("ANYAICAM_CLOUD_SIGNING_PUBLIC_KEYS", json.dumps({"attacker-a": public_a}))
    assert appliance_entitlements.store_snapshot(_signed_by(_forged_body(cloud_db), private_a, "attacker-a")) == "rejected:no_trust_anchor"
    _talk_denied_everywhere()


@pytest.mark.parametrize("anchor", [
    "{not json",
    "[]",
    '{"keys": "abc"}',
    '{"keys": {"k1": "not base64!"}}',
    '{"keys": {"k1": "AAAA"}}',                       # base64, but not 32 bytes
    '{"keys": {"bad id with spaces": "%s"}}' % base64.b64encode(b"\0" * 32).decode(),
    '{"keys": {"k1": 12345}}',
])
def test_a_malformed_trust_anchor_denies(appliance, cloud_db, edge_db, anchor):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    assert _edge_allows("talk_down")
    _provision_anchor(anchor)
    _talk_denied_everywhere()


def test_one_bad_entry_rejects_the_whole_keyset(appliance, cloud_db, edge_db):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    _provision_anchor({**_cloud_public_keys(cloud_db), "broken": "AAAA"})
    _talk_denied_everywhere()


@pytest.mark.parametrize("anchor", ["", "   ", "{}", '{"keys": {}}'])
def test_an_empty_trust_anchor_denies(appliance, cloud_db, edge_db, anchor):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    _provision_anchor(anchor)
    _talk_denied_everywhere()


def test_the_trusted_key_and_a_valid_snapshot_are_allowed(appliance, cloud_db, edge_db):
    import talk_audio_relay_client as client
    _plan(cloud_db, "basic_local")
    _addon(cloud_db, "talk_down")
    _sync(edge_db)
    assert _cached()["snapshot"]["signature"]["key_id"] in appliance_entitlements._trusted_keys()
    assert client._talk_entitled() is True
    assert _edge_allows("talk_down") and _edge_allows("voice_call")


def test_the_trusted_key_with_a_wrong_signature_is_denied(appliance, cloud_db, edge_db):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    state = _cached()
    other = _signed_by(state["snapshot"], _attacker()[0], state["snapshot"]["signature"]["key_id"])
    state["snapshot"]["signature"]["value"] = other["signature"]["value"]  # the trusted key id, someone else's signature
    appliance_entitlements.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    _talk_denied_everywhere()


@pytest.mark.parametrize("field,value", [("customer_id", "cust-2"), ("cloud_id", "AIC-OTHER"), ("appliance_id", "appl-2")])
def test_the_trusted_key_with_the_wrong_identity_is_denied(appliance, cloud_db, edge_db, field, value):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    state = _cached()
    state["snapshot"] = _resign(state["snapshot"], cloud_db, **{field: value})  # genuinely signed by the trusted key
    appliance_entitlements.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    _talk_denied_everywhere()


def test_a_cached_grant_survives_an_outage_only_while_it_verifies_against_the_anchor(appliance, cloud_db, edge_db):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    appliance["online"] = False
    assert _sync(edge_db) == {"status": "unreachable"}
    assert _edge_allows("talk_down")  # trusted key: the cached grant keeps working offline
    _provision_anchor({"next-key": _attacker()[1]})  # a release that no longer trusts the signing key
    assert not _edge_allows("talk_down")


def test_an_untrusted_cached_grant_is_denied(appliance, cloud_db, edge_db):
    """A cache written by an older build, holding a forged snapshot and the
    attacker key it cached from sync, grants nothing; the next store drops
    those cached keys."""
    private_a, public_a = _attacker()
    appliance_entitlements.STATE_FILE.write_text(json.dumps({
        "public_keys": {"attacker-a": public_a},
        "snapshot": _signed_by(_forged_body(cloud_db), private_a, "attacker-a")}), encoding="utf-8")
    _talk_denied_everywhere()
    appliance_entitlements.store_snapshot(None)
    assert "public_keys" not in _cached()


def test_a_missing_trust_anchor_denies_paid_features_but_not_the_vms(appliance, cloud_db, edge_db, monkeypatch):
    import aac_voice_call
    import aac_voice_call_greeting
    import customer_analytics_rule_worker as worker
    import vms_capacity
    _plan(cloud_db, "ai_local")
    _grant(cloud_db, "voice_call")
    appliance_entitlements.TRUST_ANCHOR_FILE.unlink()
    result = _sync(edge_db)
    assert result["status"] != "unreachable"
    capacity = vms_capacity.load_capacity()
    assert capacity is not None and vms_capacity.camera_licensed(1, [1]) == (int(capacity["camera_slot_quantity"]) >= 1)
    _talk_denied_everywhere()
    fired = [{"analytic_type": "intrusion_alarm", "talkdown_text": "Leave now", "customer_id": "cust-1"}]
    assert worker.speak_alarm_talkdowns("cam-1", fired, ["e1"], provider=aac_voice_call_greeting.MockGreetingAudioProvider()) == 0
    with override_target(sqlite_path=str(edge_db)):
        greeting = aac_voice_call.handle_edge_person_detected(customer_id="cust-1", camera_id="cam-1", camera_number=1,
                                                              forward_event=lambda event: pytest.fail("no call"))
    assert greeting == {"triggered": False, "skipped_reason": "not_entitled"}


def test_the_anchor_must_be_root_owned_and_not_writable_by_others():
    import stat
    from types import SimpleNamespace as Stat
    regular = stat.S_IFREG
    assert appliance_entitlements.ownership_problem(Stat(st_mode=regular | 0o644, st_uid=0)) == ""
    assert appliance_entitlements.ownership_problem(Stat(st_mode=stat.S_IFDIR | 0o755, st_uid=0)) == ""
    assert "owned by root" in appliance_entitlements.ownership_problem(Stat(st_mode=regular | 0o644, st_uid=1000))
    assert "writable" in appliance_entitlements.ownership_problem(Stat(st_mode=regular | 0o664, st_uid=0))
    assert "writable" in appliance_entitlements.ownership_problem(Stat(st_mode=regular | 0o646, st_uid=0))
    assert "symbolic link" in appliance_entitlements.ownership_problem(Stat(st_mode=stat.S_IFLNK | 0o777, st_uid=0))


def test_the_anchor_path_is_fixed_in_code():
    import inspect
    source = inspect.getsource(appliance_entitlements)
    assert 'TRUST_ANCHOR_FILE = Path("/etc/anyaicam-update/entitlement_signing_keys.json")' in source
    assert "ANYAICAM_CLOUD_SIGNING_PUBLIC_KEYS" not in source.split('"""', 2)[2]  # never read


# ------------------------------------------------------------ runtime role

@pytest.mark.parametrize("role", ["", "bogus", "EDGE2", "relay"])
def test_an_unknown_runtime_role_cannot_bypass_the_check(appliance, cloud_db, edge_db, monkeypatch, role):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", role)
    assert not _edge_allows("talk_down") and not _edge_allows("voice_call")


def test_an_unreadable_runtime_role_cannot_bypass_the_check(monkeypatch):
    import sys
    monkeypatch.delenv("ANYAICAM_RUNTIME_ROLE", raising=False)
    monkeypatch.setitem(sys.modules, "cloud_config", None)  # import fails
    assert feature_entitlements.runtime_role() == ""
    assert not feature_entitlements.allowed("cust-1", "talk_down")


# ------------------------------------------------------------ ordinary VMS unaffected

def test_a_cloud_outage_does_not_disable_the_local_vms(appliance, cloud_db, edge_db):
    import vms_capacity
    _plan(cloud_db, "basic_local")  # no paid talk features at all
    assert _sync(edge_db)["status"] != "unreachable"
    capacity = vms_capacity.load_capacity()
    assert capacity is not None
    appliance["online"] = False
    assert _sync(edge_db) == {"status": "unreachable"}
    assert vms_capacity.load_capacity() == capacity  # cameras keep recording/streaming offline
    # Even with no usable snapshot at all, camera licensing is unchanged.
    appliance_entitlements.clear()
    assert vms_capacity.camera_licensed(1, [1]) == (int(capacity["camera_slot_quantity"]) >= 1)
    assert not _edge_allows("talk_down")


# ------------------------------------------------------------ the appliance's own triggers

def test_the_appliance_voice_call_greeting_needs_the_snapshot(appliance, cloud_db, edge_db, greeting_provider):
    import aac_voice_call
    _plan(cloud_db, "hybrid")  # Talk Down only
    _sync(edge_db)
    with override_target(sqlite_path=str(edge_db)):
        result = aac_voice_call.handle_edge_person_detected(customer_id="cust-1", camera_id="cam-1", camera_number=1,
                                                            forward_event=lambda event: pytest.fail("no call may be forwarded"))
    assert result == {"triggered": False, "skipped_reason": "not_entitled"}


def test_alarm_talk_down_on_the_appliance_needs_the_snapshot(appliance, cloud_db, edge_db):
    import aac_voice_call_greeting
    import customer_analytics_rule_worker as worker
    provider = aac_voice_call_greeting.MockGreetingAudioProvider()
    fired = [{"analytic_type": "intrusion_alarm", "talkdown_text": "Leave now", "customer_id": "cust-1"}]
    _plan(cloud_db, "basic_local")
    _sync(edge_db)
    assert worker.speak_alarm_talkdowns("cam-1", fired, ["e1"], provider=provider) == 0
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    assert worker.speak_alarm_talkdowns("cam-1", fired, ["e1"], provider=provider) == 1


def test_the_appliance_refuses_a_talk_start_without_the_snapshot(appliance, cloud_db, edge_db, monkeypatch):
    import talk_audio_relay_client as client
    replies = []

    async def send(text):
        replies.append(json.loads(text))
    monkeypatch.setattr(client, "_start_session", lambda *a, **k: pytest.fail("the camera must not be opened"))
    _plan(cloud_db, "basic_local")
    _sync(edge_db)
    message = json.dumps({"type": "start", "session_id": "sess-1", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000})
    asyncio.run(client._handle_message(message, {1: {"camera_id": "cam-1"}}, send))
    assert replies == [{"type": "error", "session_id": "sess-1", "reason": "not_entitled"}]


# ------------------------------------------------------------ bound to the persisted identity
# (Codex review of 1760f0a: verification itself binds the snapshot to the
# appliance's persisted appliance/cloud/customer identity; no caller can
# skip it by passing no customer.)

def _write_snapshot(snapshot):
    state = _cached()
    state["snapshot"] = snapshot
    appliance_entitlements.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _talk_start_reply(monkeypatch):
    import talk_audio_relay_client as client
    replies = []

    async def send(text):
        replies.append(json.loads(text))
    monkeypatch.setattr(client, "_start_session", lambda *a, **k: False)
    message = json.dumps({"type": "start", "session_id": "sess-b", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000})
    asyncio.run(client._handle_message(message, {1: {"camera_id": "cam-1"}}, send))
    return replies


@pytest.mark.parametrize("field,value", [("customer_id", "cust-2"), ("cloud_id", "AIC-OTHER")])
def test_a_signed_snapshot_for_this_appliance_but_another_customer_or_cloud_id_is_denied(
        appliance, cloud_db, edge_db, monkeypatch, field, value):
    import talk_audio_relay_client as client
    _plan(cloud_db, "basic_local")
    _addon(cloud_db, "talk_down")
    _sync(edge_db)
    assert client._talk_entitled() is True  # correct appliance and customer: allowed
    _write_snapshot(_resign(_cached()["snapshot"], cloud_db, **{field: value}))  # validly signed by the cloud
    assert client._talk_entitled() is False  # the appliance's talk relay passes no customer
    assert _talk_start_reply(monkeypatch) == [{"type": "error", "session_id": "sess-b", "reason": "not_entitled"}]
    for feature in ("talk_down", "voice_call"):
        assert appliance_entitlements.feature_allowed(feature) is False
        assert appliance_entitlements.feature_allowed(feature, None) is False
        assert appliance_entitlements.feature_allowed(feature, value) is False  # naming the snapshot's customer does not help
        assert _edge_allows(feature) is False


def test_the_correct_identity_is_allowed_when_entitled(appliance, cloud_db, edge_db, monkeypatch):
    import talk_audio_relay_client as client
    _plan(cloud_db, "basic_local")
    _addon(cloud_db, "talk_down")
    _sync(edge_db)
    snapshot = _cached()["snapshot"]
    assert (snapshot["appliance_id"], snapshot["cloud_id"], snapshot["customer_id"]) == (APPLIANCE_ID, f"AIC-{APPLIANCE_ID}", "cust-1")
    assert client._talk_entitled() is True
    assert appliance_entitlements.feature_allowed("talk_down", None) is True
    assert appliance_entitlements.feature_allowed("voice_call", "cust-1") is True
    started = []
    monkeypatch.setattr(client, "_start_session", lambda *a, **k: started.append(a) or False)
    message = json.dumps({"type": "start", "session_id": "sess-c", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000})
    asyncio.run(client._handle_message(message, {1: {"camera_id": "cam-1"}}, None))
    assert len(started) == 1  # past the entitlement check, on to the camera


@pytest.mark.parametrize("field,value", [("customer_id", "cust-2"), ("cloud_id", "AIC-OTHER"), ("appliance_id", "appl-2")])
def test_a_persisted_identity_that_does_not_match_the_snapshot_is_denied(appliance, cloud_db, edge_db, monkeypatch, field, value):
    _plan(cloud_db, "ai_local")
    _grant(cloud_db, "voice_call")
    _sync(edge_db)
    assert _edge_allows("talk_down") and _edge_allows("voice_call")
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: {**EDGE_IDENTITY, field: value})
    for feature in ("talk_down", "voice_call"):
        assert appliance_entitlements.feature_allowed(feature) is False
        assert appliance_entitlements.feature_allowed(feature, None) is False


@pytest.mark.parametrize("persisted", [
    None,                                                     # missing or unreadable identity file
    {k: v for k, v in EDGE_IDENTITY.items() if k != "cloud_id"},
    {k: v for k, v in EDGE_IDENTITY.items() if k != "customer_id"},
    {**EDGE_IDENTITY, "customer_id": ""},
    {**EDGE_IDENTITY, "customer_id": 123},
    {**EDGE_IDENTITY, "cloud_id": None},
    "not a dict",
])
def test_a_missing_or_malformed_persisted_identity_is_denied(appliance, cloud_db, edge_db, monkeypatch, persisted):
    _plan(cloud_db, "ai_local")
    _grant(cloud_db, "voice_call")
    _sync(edge_db)
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: persisted)
    for feature in ("talk_down", "voice_call"):
        assert appliance_entitlements.feature_allowed(feature) is False
    assert appliance_entitlements.store_snapshot(_cached().get("snapshot")) == "rejected:not_activated"


def test_an_unreadable_identity_file_is_denied(appliance, cloud_db, edge_db, monkeypatch, tmp_path):
    import appliance_activation
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    assert _edge_allows("talk_down")
    broken = tmp_path / "identity.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(appliance_activation, "ACTIVATION_IDENTITY_FILE", broken)
    monkeypatch.setattr(appliance_activation, "load_persisted_identity", _REAL_LOAD_IDENTITY)  # the real loader
    assert appliance_activation.load_persisted_identity() is None
    assert not _edge_allows("talk_down") and not _edge_allows("voice_call")


@pytest.mark.parametrize("change", [
    {"customer_id": None}, {"customer_id": ""}, {"customer_id": 7},
    {"cloud_id": None}, {"cloud_id": ""}, {"appliance_id": None},
])
def test_a_signed_snapshot_with_malformed_identity_is_denied(appliance, cloud_db, edge_db, change):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    _write_snapshot(_resign(_cached()["snapshot"], cloud_db, **change))
    assert appliance_entitlements.feature_allowed("talk_down") is False
    assert appliance_entitlements.feature_allowed("talk_down", None) is False


@pytest.mark.parametrize("missing", ["customer_id", "cloud_id", "appliance_id"])
def test_a_signed_snapshot_missing_an_identity_field_is_denied(appliance, cloud_db, edge_db, missing):
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    body = {k: v for k, v in _cached()["snapshot"].items() if k not in ("signature", missing)}
    _write_snapshot(_resign(body, cloud_db))
    assert appliance_entitlements.feature_allowed("talk_down") is False
    assert appliance_entitlements.feature_allowed("talk_down", None) is False


@pytest.fixture()
def owner_client(appliance, cloud_db):
    """The talk routes, signed in as the customer owner (rows seeded by appliance)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import partner_portal
    import talk_audio_relay
    import talk_sessions
    with override_target(sqlite_path=str(cloud_db)):
        app = FastAPI()
        talk_sessions.register_talk_session_routes(app)
        talk_audio_relay.register_talk_audio_relay_routes(app)
        with TestClient(app) as client:
            client.cookies.set(partner_portal.SESSION_COOKIE,
                               partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None))
            yield client


def test_an_appliance_talk_session_ends_when_a_sync_revokes_talk_down(appliance, cloud_db, edge_db, owner_client, monkeypatch):
    """The appliance's own audio socket (edge role) re-checks the cached
    snapshot while talking; a sync that removes Talk Down stops the audio."""
    from datetime import datetime as _dt, timedelta as _td
    import talk_audio_relay
    from starlette.websockets import WebSocketDisconnect
    frames = []

    class FakeCameraSpeaker:
        def __init__(self, camera, sample_rate, session_id=None):
            self.error = None

        def start(self):
            return True

        def send_pcm16(self, frame):
            frames.append(frame)

        def stop(self):
            pass
    monkeypatch.setattr(talk_audio_relay, "_LocalIsapiTalkRelay", FakeCameraSpeaker)
    monkeypatch.setattr(talk_audio_relay, "ENTITLEMENT_RECHECK_SECONDS", 0)
    _plan(cloud_db, "ai_local")
    _sync(edge_db)
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    assert _edge_allows("talk_down")
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            db.execute("UPDATE cameras SET talk_down_supported=1 WHERE id='cam-1'")
            db.execute("INSERT INTO customer_talk_sessions(id,customer_id,site_id,camera_id,user_id,requested_by,role,state,"
                       "requested_at,ended_at,expires_at) VALUES('talk-e','cust-1','site-1','cam-1',NULL,'owner@example.test',"
                       "'customer_owner','requested',?,NULL,?)",
                       (_dt.now().isoformat(), (_dt.now() + _td(minutes=5)).isoformat()))
        with owner_client.websocket_connect("/api/customer/talk/sessions/talk-e/audio") as socket:
            assert socket.receive_json() == {"type": "ready"}
            socket.send_bytes(bytes(320))
            _plan(cloud_db, "ai_local", status="cancelled")
            _sync(edge_db)  # the next configuration sync carries the revocation
            socket.send_bytes(bytes(320))
            message = socket.receive_json()
            assert message["type"] == "error" and message["reason"] == "not_entitled"
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
    assert closed.value.code == 4403
    assert len(frames) == 1
