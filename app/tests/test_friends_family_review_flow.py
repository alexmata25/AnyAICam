"""Friends & Family review, end to end (2026-10-01).

Reported: clicking Review Request in the administrator email answered
'detail: Partner authorization required'. Three causes:
- signed out, the cloud sent /admin/friends-family/... to the appliance's
  emergency /login, whose local session the review page cannot accept;
- Partner Portal sign-in ignored ?next=, so even the right sign-in page
  never came back to the request;
- signed in as anyone else (often the customer account in the same browser)
  the review page answered with raw JSON.
The customer was also never told the decision.
"""
from pathlib import Path

import pytest

from test_pricing_ff_commission import (  # noqa: F401 -- fixtures and helpers
    OWNER,
    _cookie,
    _make_global_admin,
    _seed,
    db_path,
    portal,
)


def _request(client, db_path):
    _seed(db_path)
    return client.post("/api/customer/friends-family/request", json={}, cookies=_cookie(*OWNER)).json()["request_id"]


def test_the_emailed_link_is_the_review_page(portal, db_path):
    client, _, sent = portal
    request_id = _request(client, db_path)
    assert f"/admin/friends-family/{request_id}" in sent[0]["text"]


def test_signed_out_on_the_cloud_goes_to_partner_sign_in_and_back(portal, db_path, monkeypatch):
    import main
    client, _, _ = portal
    request_id = _request(client, db_path)
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    response = client.get(f"/admin/friends-family/{request_id}")
    assert response.status_code == 303
    assert response.headers["location"] == f"/partner.html?next=/admin/friends-family/{request_id}"


def test_the_review_route_itself_never_answers_a_signed_out_browser_with_json(portal, db_path):
    import friends_family
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    client, _, _ = portal
    request_id = _request(client, db_path)
    bare = FastAPI()
    friends_family.register_routes(bare, lambda title, active, content, scripts="": content)
    response = TestClient(bare, follow_redirects=False).get(f"/admin/friends-family/{request_id}")
    assert response.status_code == 303 and response.headers["location"] == f"/partner.html?next=/admin/friends-family/{request_id}"


def test_signed_in_as_the_customer_explains_and_offers_to_switch(portal, db_path):
    client, _, _ = portal
    request_id = _request(client, db_path)
    response = client.get(f"/admin/friends-family/{request_id}", cookies=_cookie(*OWNER))
    assert response.status_code == 403 and "text/html" in response.headers["content-type"]
    assert "Platform administrator sign-in needed" in response.text and "owner@example.test" in response.text
    assert "Sign in as administrator" in response.text and f"/partner.html?next=/admin/friends-family/{request_id}" in response.text
    assert "Partner authorization required" not in response.text
    # A partner-scoped administrator gets the same explanation, never the request.
    company = client.get(f"/admin/friends-family/{request_id}", cookies=_cookie("company-admin@example.test", "administrator"))
    assert company.status_code == 403 and "Approve" not in company.text


def test_partner_sign_in_returns_to_the_review_page_after_password_and_mfa():
    html = (Path(__file__).resolve().parents[1] / "partner.html").read_text(encoding="utf-8")
    assert "function nextDestination()" in html
    assert html.count("location.href=nextDestination()||response.url") == 2  # password step and MFA step


def test_the_review_page_shows_details_and_no_internal_ids(portal, db_path):
    client, _, _ = portal
    request_id = _request(client, db_path)
    page = client.get(f"/admin/friends-family/{request_id}", cookies=_make_global_admin(db_path)).text
    assert "Real Customer" in page and "owner@example.test" in page and "Approve" in page and "Decline" in page
    assert "cust-1" not in page  # the internal account id is not shown


@pytest.mark.parametrize("decision,status,subject_part,body_part", [
    ("approve", "approved", "approved", "off your camera plan"),
    ("decline", "declined", "About your Friends & Family request", "regular price"),
])
def test_the_decision_is_saved_shown_and_emailed_to_the_customer(portal, db_path, decision, status, subject_part, body_part):
    client, _, sent = portal
    request_id = _request(client, db_path)
    admin = _make_global_admin(db_path)
    decided = client.post(f"/api/admin/friends-family/{request_id}/decision", json={"decision": decision, "note": "ok"}, cookies=admin)
    assert decided.status_code == 200 and decided.json()["status"] == status
    assert client.get("/api/customer/friends-family", cookies=_cookie(*OWNER)).json()["status"] == status
    to_customer = [m for m in sent if m["to"] == "owner@example.test"]
    assert len(to_customer) == 1 and subject_part in to_customer[0]["subject"] and body_part in to_customer[0]["text"]
    page = client.get(f"/admin/friends-family/{request_id}", cookies=admin).text
    assert "Decided by" in page and "admin@example.test" in page and 'id="ff-approve"' not in page
    # Deciding again is refused and sends nothing more.
    assert client.post(f"/api/admin/friends-family/{request_id}/decision", json={"decision": "approve"}, cookies=admin).status_code == 409
    assert len([m for m in sent if m["to"] == "owner@example.test"]) == 1


def test_the_admin_list_follows_the_same_sign_in_rules(portal, db_path, monkeypatch):
    import main
    client, _, _ = portal
    _request(client, db_path)
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    assert client.get("/admin/friends-family").headers["location"] == "/partner.html?next=/admin/friends-family"
    assert client.get("/admin/friends-family", cookies=_cookie(*OWNER)).status_code == 403
    assert "Real Customer" in client.get("/admin/friends-family", cookies=_make_global_admin(db_path)).text
