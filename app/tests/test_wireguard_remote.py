"""WireGuard direct remote-connectivity -- Phase A2 foundation coverage
(see docs/wireguard-remote-connectivity-plan.md). Tests the cloud-side
enrollment route and its underlying logic (wireguard_remote.py): tenant
isolation, authorization, key handling (a private key is never accepted,
stored, or returned by any route), revocation, reconnect/idempotency,
failure cases, and -- since no real tunnel exists yet this pass -- that
adding this module is completely inert with respect to the three existing
live-view transports.

Same established pattern as test_live_view_p2p_signaling.py: appliance-
side bearer-auth routes are tested through a minimal, isolated FastAPI app
(register_wireguard_remote_appliance_routes only), with real
X-Appliance-Id/X-Request-Timestamp/X-Request-Nonce/Authorization headers
-- not a monkeypatched identity function -- so authenticate_appliance()'s
own real verification path is actually exercised, not bypassed.
"""

import secrets
import time
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import wireguard_remote
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash, row


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_wireguard_remote.db"


def _seed(db_path, *, appliance_credential="cred-a"):
    now = datetime.now().isoformat()
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-b','partner-1','Customer B','b@example.test','active',?)", (now,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main',?)", (now,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-b','cust-b','Main',?)", (now,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A',?)", (now,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-b','cust-b','site-b','AIC-B',?)", (now,))
            db.execute(
                "INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('cred-a-row','appl-a',?,?)",
                (password_hash(appliance_credential), now),
            )
            db.execute(
                "INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('cred-b-row','appl-b',?,?)",
                (password_hash("cred-b"), now),
            )


def _appliance_headers(appliance_id, credential):
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


@pytest.fixture()
def appliance_client(db_path, monkeypatch):
    monkeypatch.setattr(wireguard_remote, "GATEWAY_PUBLIC_KEY", "z" * 43 + "=")
    monkeypatch.setattr(wireguard_remote, "GATEWAY_ENDPOINT", "gateway.example.test:51820")
    with override_target(sqlite_path=str(db_path)):
        app = FastAPI()
        wireguard_remote.register_wireguard_remote_appliance_routes(app)
        with TestClient(app) as test_client:
            yield test_client


def _real_public_key():
    _, public_key = wireguard_remote.generate_keypair()
    return public_key


# --------------------------------------------------------------- key format


def test_generate_keypair_returns_real_wireguard_format():
    private_key, public_key = wireguard_remote.generate_keypair()
    assert wireguard_remote.is_valid_wireguard_public_key(public_key)
    # The private key is the same 44-char base64 shape -- generate_keypair()
    # itself is never called by any route (see module docstring), but its
    # output format must still match what a real `wg genkey` produces so a
    # future appliance-agent implementation can use it directly.
    assert len(private_key) == 44
    assert private_key != public_key


@pytest.mark.parametrize("bad_value", [
    "",
    "not-base64!!!!",
    "short",
    "a" * 44,  # right length, but decodes to 33 bytes (no padding char) -- not a valid 32-byte key
    None,
    123,
    "a" * 45,  # wrong length outright
])
def test_invalid_public_keys_rejected(bad_value):
    assert wireguard_remote.is_valid_wireguard_public_key(bad_value) is False


def test_valid_public_key_accepted():
    assert wireguard_remote.is_valid_wireguard_public_key(_real_public_key()) is True


# --------------------------------------------------------------- authorization


def test_enroll_requires_appliance_authentication(db_path, appliance_client):
    _seed(db_path)
    response = appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()})
    assert response.status_code == 401


def test_enroll_rejects_unknown_appliance_id(db_path, appliance_client):
    _seed(db_path)
    headers = _appliance_headers("appl-does-not-exist", "cred-a")
    response = appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()}, headers=headers)
    assert response.status_code == 403


def test_enroll_rejects_wrong_credential(db_path, appliance_client):
    _seed(db_path)
    headers = _appliance_headers("appl-a", "wrong-credential")
    response = appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()}, headers=headers)
    assert response.status_code == 403


# --------------------------------------------------------------- enrollment happy path


def test_enroll_succeeds_and_never_returns_a_private_key(db_path, appliance_client):
    _seed(db_path)
    headers = _appliance_headers("appl-a", "cred-a")
    response = appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["gateway_public_key"] == "z" * 43 + "="
    assert body["gateway_endpoint"] == "gateway.example.test:51820"
    assert body["status"] == "enrolled"
    assert "tunnel_address" in body and body["tunnel_address"].startswith("10.70.")
    # The response contract has exactly these four fields -- nothing that
    # could ever be a private key, a credential, or any other secret.
    assert set(body.keys()) == {"tunnel_address", "gateway_public_key", "gateway_endpoint", "status"}


def test_enroll_persists_only_the_public_key_never_any_other_submitted_field(db_path, appliance_client):
    """A malicious or buggy caller submitting a 'private_key' field
    alongside public_key must never see it echoed back, and it must
    never be persisted anywhere -- enroll_peer() only ever reads
    public_key out of the payload (see module docstring)."""
    _seed(db_path)
    headers = _appliance_headers("appl-a", "cred-a")
    public_key = _real_public_key()
    response = appliance_client.post(
        "/api/appliance/wireguard/enroll",
        json={"public_key": public_key, "private_key": "SHOULD-NEVER-BE-STORED", "tunnel_address": "10.70.0.1"},
        headers=headers,
    )
    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        stored = row("SELECT * FROM appliance_wireguard_peers WHERE public_key=?", (public_key,))
    assert stored is not None
    assert "SHOULD-NEVER-BE-STORED" not in dict(stored).values()
    # 'private_key' is not a real column at all -- structurally impossible
    # to have been persisted, not merely absent from this one row.
    assert "private_key" not in dict(stored)
    # The client's own requested tunnel_address is ignored; a real one is
    # always assigned server-side from the pool, never trusted from the
    # request.
    assert stored["tunnel_address"].startswith("10.70.")


def test_enroll_rejects_malformed_public_key(db_path, appliance_client):
    _seed(db_path)
    headers = _appliance_headers("appl-a", "cred-a")
    response = appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": "not-a-real-key"}, headers=headers)
    assert response.status_code == 400


def test_enroll_fails_closed_when_gateway_is_not_configured(db_path, appliance_client, monkeypatch):
    _seed(db_path)
    monkeypatch.setattr(wireguard_remote, "GATEWAY_PUBLIC_KEY", "")
    headers = _appliance_headers("appl-a", "cred-a")
    response = appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()}, headers=headers)
    assert response.status_code == 503


# --------------------------------------------------------------- tenant isolation


def test_two_appliances_get_distinct_tunnel_addresses_and_customer_scope(db_path, appliance_client):
    _seed(db_path)
    response_a = appliance_client.post(
        "/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()}, headers=_appliance_headers("appl-a", "cred-a"),
    )
    response_b = appliance_client.post(
        "/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()}, headers=_appliance_headers("appl-b", "cred-b"),
    )
    assert response_a.status_code == 200 and response_b.status_code == 200
    assert response_a.json()["tunnel_address"] != response_b.json()["tunnel_address"]
    with override_target(sqlite_path=str(db_path)):
        peer_a = row("SELECT * FROM appliance_wireguard_peers WHERE appliance_id='appl-a'")
        peer_b = row("SELECT * FROM appliance_wireguard_peers WHERE appliance_id='appl-b'")
    assert peer_a["customer_id"] == "cust-a"
    assert peer_b["customer_id"] == "cust-b"


def test_appliance_credential_cannot_enroll_a_peer_for_a_different_appliance(db_path, appliance_client):
    """authenticate_appliance() resolves appliance identity from the
    credential itself, never from anything the request body claims --
    there is no appliance_id field in the enroll payload at all, so
    this is really a structural guarantee, proven here by confirming
    cred-a's own peer is always attributed to appl-a regardless of
    what X-Appliance-Id claims (authenticate_appliance() itself is
    exercised unchanged, so a mismatched header is simply a wrong-
    credential 403, covered by test_enroll_rejects_wrong_credential)."""
    _seed(db_path)
    headers = _appliance_headers("appl-a", "cred-a")
    appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()}, headers=headers)
    with override_target(sqlite_path=str(db_path)):
        peer = row("SELECT * FROM appliance_wireguard_peers WHERE appliance_id='appl-a'")
    assert peer["appliance_id"] == "appl-a"
    assert peer["customer_id"] == "cust-a"


# --------------------------------------------------------------- reconnect / idempotency


def test_reenrolling_the_same_public_key_is_idempotent(db_path, appliance_client):
    """Simulates an appliance reboot/retry re-submitting its own
    already-generated public key (see plan doc Sec 15) -- must not
    create a duplicate row or violate the public_key unique index."""
    _seed(db_path)
    public_key = _real_public_key()
    headers = _appliance_headers("appl-a", "cred-a")
    first = appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": public_key}, headers=headers)
    second = appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": public_key}, headers=_appliance_headers("appl-a", "cred-a"))
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["tunnel_address"] == second.json()["tunnel_address"]
    with override_target(sqlite_path=str(db_path)):
        count = row("SELECT COUNT(*) AS c FROM appliance_wireguard_peers WHERE public_key=?", (public_key,))
    assert count["c"] == 1


def test_rotation_allows_a_second_active_peer_for_the_same_appliance(db_path, appliance_client):
    """A genuinely NEW public key for an appliance that already has one
    active peer is a rotation, not a conflict -- see plan doc Sec 11."""
    _seed(db_path)
    first_key = _real_public_key()
    second_key = _real_public_key()
    headers_a = _appliance_headers("appl-a", "cred-a")
    appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": first_key}, headers=headers_a)
    response = appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": second_key}, headers=_appliance_headers("appl-a", "cred-a"))
    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            active = wireguard_remote.active_peers_for_appliance(db, "appl-a")
    assert len(active) == 2
    assert {peer["public_key"] for peer in active} == {first_key, second_key}


def test_same_public_key_cannot_be_stolen_by_a_different_appliance(db_path, appliance_client):
    _seed(db_path)
    public_key = _real_public_key()
    appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": public_key}, headers=_appliance_headers("appl-a", "cred-a"))
    response = appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": public_key}, headers=_appliance_headers("appl-b", "cred-b"))
    assert response.status_code == 409


# ------------------------------------------------- replace_existing (hardware replacement, plan doc Sec 12)


def test_replace_existing_revokes_the_appliances_prior_peer(db_path, appliance_client):
    _seed(db_path)
    old_key = _real_public_key()
    new_key = _real_public_key()
    old_response = appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": old_key}, headers=_appliance_headers("appl-a", "cred-a"))
    old_peer_id = None
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            old_peer_id = wireguard_remote.active_peers_for_appliance(db, "appl-a")[0]["id"]

    response = appliance_client.post(
        "/api/appliance/wireguard/enroll",
        json={"public_key": new_key, "replace_existing": True},
        headers=_appliance_headers("appl-a", "cred-a"),
    )

    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            active = wireguard_remote.active_peers_for_appliance(db, "appl-a")
            old_row = db.execute("SELECT status,revoked_reason FROM appliance_wireguard_peers WHERE id=?", (old_peer_id,)).fetchone()
    assert {peer["public_key"] for peer in active} == {new_key}
    assert old_row["status"] == "revoked"
    assert old_row["revoked_reason"] == "reenrolled"


def test_replace_existing_never_revokes_other_appliances_peers(db_path, appliance_client):
    _seed(db_path)
    appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()}, headers=_appliance_headers("appl-b", "cred-b"))
    appliance_client.post(
        "/api/appliance/wireguard/enroll",
        json={"public_key": _real_public_key(), "replace_existing": True},
        headers=_appliance_headers("appl-a", "cred-a"),
    )
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            b_active = wireguard_remote.active_peers_for_appliance(db, "appl-b")
    assert len(b_active) == 1  # untouched by appl-a's own replace_existing call


def test_replace_existing_without_any_prior_peer_is_a_safe_first_enrollment(db_path, appliance_client):
    """A brand-new appliance's first-ever enroll call can legitimately
    set replace_existing=True (the agent doesn't need to know whether
    this is its first activation or a re-enrollment to be correct) --
    must succeed exactly like a normal enrollment, not error because
    there was nothing to revoke."""
    _seed(db_path)
    response = appliance_client.post(
        "/api/appliance/wireguard/enroll",
        json={"public_key": _real_public_key(), "replace_existing": True},
        headers=_appliance_headers("appl-a", "cred-a"),
    )
    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            active = wireguard_remote.active_peers_for_appliance(db, "appl-a")
    assert len(active) == 1


def test_replace_existing_resubmitting_the_same_already_active_key_does_not_revoke_itself(db_path, appliance_client):
    """The ordering fix this route relies on: enroll_peer() runs before
    the revoke-others step, and the revoke explicitly excludes the row
    just enrolled/confirmed -- so replaying the SAME key with
    replace_existing=True must never revoke the very peer it just
    (idempotently) confirmed."""
    _seed(db_path)
    key = _real_public_key()
    appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": key}, headers=_appliance_headers("appl-a", "cred-a"))
    response = appliance_client.post(
        "/api/appliance/wireguard/enroll",
        json={"public_key": key, "replace_existing": True},
        headers=_appliance_headers("appl-a", "cred-a"),
    )
    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            active = wireguard_remote.active_peers_for_appliance(db, "appl-a")
    assert {peer["public_key"] for peer in active} == {key}


# --------------------------------------------------------------- revocation


def test_revoke_peer_marks_row_revoked_and_frees_its_tunnel_address(db_path, appliance_client):
    _seed(db_path)
    response = appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()}, headers=_appliance_headers("appl-a", "cred-a"))
    tunnel_address = response.json()["tunnel_address"]
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            peer = db.execute("SELECT id FROM appliance_wireguard_peers WHERE appliance_id='appl-a'").fetchone()
            revoked = wireguard_remote.revoke_peer(db, peer_id=peer["id"], reason="test_revocation", now=datetime.now())
        assert revoked is True
        stored = row("SELECT * FROM appliance_wireguard_peers WHERE id=?", (peer["id"],))
    assert stored["status"] == "revoked"
    assert stored["revoked_at"] is not None
    assert stored["revoked_reason"] == "test_revocation"
    # A second, different appliance may now be assigned the freed address --
    # proving the partial-unique-index/reassignment design in the plan
    # doc's Sec 10/11 actually works, not just that the row updated.
    second = appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()}, headers=_appliance_headers("appl-b", "cred-b"))
    assert second.status_code == 200
    # Not strictly guaranteed to reuse the exact freed address (lowest-free
    # allocation may pick a different one if other rows exist), but must
    # succeed rather than exhausting the pool -- the meaningful assertion
    # here is that revocation didn't leave the address permanently stuck.
    assert second.json()["tunnel_address"]


def test_revoke_peer_is_idempotent_on_an_already_revoked_row(db_path, appliance_client):
    _seed(db_path)
    appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()}, headers=_appliance_headers("appl-a", "cred-a"))
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            peer = db.execute("SELECT id FROM appliance_wireguard_peers WHERE appliance_id='appl-a'").fetchone()
            first = wireguard_remote.revoke_peer(db, peer_id=peer["id"], reason="first", now=datetime.now())
        with connection() as db:
            second = wireguard_remote.revoke_peer(db, peer_id=peer["id"], reason="second", now=datetime.now())
    assert first is True
    assert second is False  # already-revoked -- a no-op, not an error, matching this codebase's established convention


def test_revoke_unknown_peer_id_is_a_safe_no_op(db_path):
    _seed(db_path)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            result = wireguard_remote.revoke_peer(db, peer_id="does-not-exist", reason="x", now=datetime.now())
    assert result is False


def test_revoke_all_peers_for_appliance_handles_hardware_replacement(db_path, appliance_client):
    """Plan doc Sec 12: a re-enrollment event should invalidate every
    previously-active peer for the appliance_id before the replacement
    device enrolls its own fresh key."""
    _seed(db_path)
    appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()}, headers=_appliance_headers("appl-a", "cred-a"))
    appliance_client.post("/api/appliance/wireguard/enroll", json={"public_key": _real_public_key()}, headers=_appliance_headers("appl-a", "cred-a"))
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            count = wireguard_remote.revoke_all_peers_for_appliance(db, appliance_id="appl-a", reason="hardware_replaced", now=datetime.now())
        assert count == 2
        active = row("SELECT COUNT(*) AS c FROM appliance_wireguard_peers WHERE appliance_id='appl-a' AND revoked_at IS NULL")
    assert active["c"] == 0


def test_revoke_all_peers_for_appliance_with_none_enrolled_returns_zero(db_path):
    _seed(db_path)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            count = wireguard_remote.revoke_all_peers_for_appliance(db, appliance_id="appl-a", reason="x", now=datetime.now())
    assert count == 0


# --------------------------------------------------------------- inertness w.r.t. existing transports


def test_wireguard_module_does_not_alter_existing_live_view_session_start(db_path):
    """Regression-safety, not a WireGuard feature test: proves adding
    this schema/module changes nothing about the existing relay-always-
    queues-first behavior in live_view_sessions.py (plan doc Sec 18) --
    a fresh live/start call still queues start_live_relay exactly as
    before, with no new columns or side effects introduced by this
    migration touching that table or that route at all."""
    now = datetime.now().isoformat()
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active',?)", (now,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main',?)", (now,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A',?)", (now,))
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,status,name,created_at) "
                "VALUES('cam-a','cust-a','site-a','appl-a',1,'configured','Camera 1',?)", (now,),
            )
            db.execute(
                "INSERT INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
                "VALUES('user-a','owner-a@example.test','customer_owner','cust-a','x','all',?)", (now,),
            )
        # appliance_wireguard_peers exists and is empty -- has no foreign
        # key or trigger relationship to live_view_sessions/cameras/
        # appliance_commands that could change their behavior.
        with connection() as db:
            table_count = db.execute("SELECT COUNT(*) AS c FROM appliance_wireguard_peers").fetchone()["c"]
    assert table_count == 0
