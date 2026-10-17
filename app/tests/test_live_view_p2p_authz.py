"""Focused authorization tests for reused live-view signaling session IDs."""
from datetime import datetime

import pytest
from fastapi import HTTPException

import live_view_p2p
import live_view_wireguard


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Db:
    def __init__(self, *, can_live):
        self.can_live = can_live

    def execute(self, query, params):
        if "FROM live_view_sessions" in query:
            return _Result({"id": "session-a", "customer_id": "cust-a", "camera_id": "cam-a", "state": "requested"})
        if "FROM cameras" in query:
            return _Result({"id": "cam-a"})
        if "FROM partner_users" in query:
            return _Result({"id": "viewer-a"})
        if "FROM customer_camera_permissions" in query:
            return _Result({"can_live": int(self.can_live)})
        raise AssertionError(f"unexpected SQL in test: {query}")


def test_viewer_requires_current_camera_live_grant_for_another_users_session():
    viewer = {"customer_id": "cust-a", "email": "viewer@example.test", "role": "customer_viewer"}
    with pytest.raises(HTTPException) as denied:
        live_view_p2p._own_session(_Db(can_live=False), "session-a", viewer)
    assert denied.value.status_code == 404

    allowed = live_view_p2p._own_session(_Db(can_live=True), "session-a", viewer)
    assert allowed["camera_id"] == "cam-a"


def test_customer_owner_keeps_existing_access_without_camera_grant_lookup():
    owner = {"customer_id": "cust-a", "email": "owner@example.test", "role": "customer_owner"}
    assert live_view_p2p._own_session(_Db(can_live=False), "session-a", owner)["id"] == "session-a"


def test_wireguard_session_access_rechecks_current_camera_live_grant():
    viewer = {"customer_id": "cust-a", "email": "viewer@example.test", "role": "customer_viewer"}
    with pytest.raises(HTTPException) as denied:
        live_view_wireguard._own_session(_Db(can_live=False), "session-a", viewer)
    assert denied.value.status_code == 404
    assert live_view_wireguard._own_session(_Db(can_live=True), "session-a", viewer)["camera_id"] == "cam-a"


def test_signaling_storage_rejects_oversized_payload_instead_of_truncating_json():
    class CaptureDb:
        def __init__(self):
            self.params = None

        def execute(self, query, params):
            self.params = params

    db = CaptureDb()
    with pytest.raises(HTTPException) as rejected:
        live_view_p2p._insert_signal(db, session_id="session-a", kind="offer",
                                     payload={"sdp": "x" * live_view_p2p.MAX_SIGNAL_PAYLOAD_BYTES},
                                     now=datetime.now())
    assert rejected.value.status_code == 413
    assert db.params is None


def test_disabled_p2p_flag_fails_closed_at_signaling_layer(monkeypatch):
    monkeypatch.setattr(live_view_p2p, "LIVE_P2P_ENABLED", False)
    with pytest.raises(HTTPException) as rejected:
        live_view_p2p._require_p2p_enabled()
    assert rejected.value.status_code == 404
