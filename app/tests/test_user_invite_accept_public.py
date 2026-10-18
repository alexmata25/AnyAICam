"""Accepting a Users-page invitation works for someone who is not signed in (Green 2026-10-17).

/accept-invite (the link in the invitation email) is a public page, but the API it posts to, /api/user-invites/accept,
was not a public path, so the authentication middleware answered every invitee with 401 "Authentication required"
and nobody could accept. The handler itself checks the signed, 7-day invitation token and that the invitation is
still pending, so the exact path is now public; the other /api/user-invites routes stay protected.
"""
import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def store(monkeypatch):
    data = {"users": [], "invites": [{"id": "inv-1", "email": "invitee@example.test", "role": "viewer",
                                      "status": "pending", "all_cameras": True}]}
    monkeypatch.setattr(main, "load_users", lambda: [dict(u) for u in data["users"]])
    monkeypatch.setattr(main, "save_users", lambda users: data.__setitem__("users", users))
    monkeypatch.setattr(main, "load_user_invites", lambda: [dict(i) for i in data["invites"]])
    monkeypatch.setattr(main, "save_user_invites", lambda invites: data.__setitem__("invites", invites))
    return data


def _token(invite_id="inv-1"):
    return main.session_serializer.dumps({"invite_id": invite_id}, salt="anyaicam-user-invite")


def test_an_invitee_without_a_session_can_accept(store):
    with TestClient(main.app) as client:
        response = client.post("/api/user-invites/accept", json={
            "token": _token(), "display_name": "New Viewer", "password": "correct-horse-battery-staple"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "complete"
    assert [u["email"] for u in store["users"]] == ["invitee@example.test"]
    assert store["invites"][0]["status"] == "accepted"


def test_a_forged_token_is_still_refused_by_the_handler(store):
    with TestClient(main.app) as client:
        response = client.post("/api/user-invites/accept", json={
            "token": "not-a-real-token", "display_name": "Someone", "password": "correct-horse-battery-staple"})
    assert response.status_code == 200
    assert response.json() == {"status": "error", "message": "Invitation expired or invalid."}
    assert store["users"] == []


def test_an_accepted_invitation_cannot_be_used_twice(store):
    store["invites"][0]["status"] = "accepted"
    with TestClient(main.app) as client:
        response = client.post("/api/user-invites/accept", json={
            "token": _token(), "display_name": "Again", "password": "correct-horse-battery-staple"})
    assert response.json() == {"status": "error", "message": "Invitation is no longer available."}


@pytest.mark.parametrize("method, path", [("get", "/api/user-invites"), ("post", "/api/user-invites"),
                                          ("post", "/api/user-invites/inv-1/resend"),
                                          ("delete", "/api/user-invites/inv-1")])
def test_the_other_invitation_routes_still_need_a_session(store, method, path):
    with TestClient(main.app) as client:
        response = getattr(client, method)(path)
    assert response.status_code in (401, 303, 307), (path, response.status_code)
