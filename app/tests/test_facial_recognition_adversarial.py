"""AAC adversarial hardening tests -- Codex review pass.

Written against two concrete findings from the review of
facial_recognition_ui.py:

1. customer_id (client-supplied for staff roles) flowed unsanitized
   into a filesystem path in _save_face_crop(). In practice
   facial_people.enroll_person()'s foreign-key constraint against
   `customers` already made this unreachable with a real payload, but
   that protection was implicit and backend-dependent -- see
   _require_safe_path_segment() in facial_recognition_ui.py for the
   explicit fix and its own docstring.
2. Several innerHTML template literals (People/Watchlists/Facial
   Events/Match detail pages) interpolated server data -- display_name,
   watchlist name, matched_person_name, etc, all free text a
   'facial.manage' user can set -- without HTML-escaping, a stored-XSS
   vector across roles (e.g. a compromised customer_owner session
   injecting markup that executes in an administrator's browser when
   they view the same tenant's People/Events page). Fixed with a
   client-side aacEsc() helper; this file's source-scan tests are the
   regression guard for that fix (a TestClient has no JS engine, so the
   fix itself can only be verified statically here).
"""

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_facial_recognition_adversarial_import.db"):
    import facial_recognition_ui
    import partner_portal
    from partner_db import connection, initialize_database

NOW = "2026-09-08T00:00:00"


def _shell(title, active, content, scripts=""):
    return f"<html><title>{title}</title>{content}{scripts}</html>"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_facial_recognition_adversarial.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C1','c1@example.test','active','real',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-2','p1','C2','c2@example.test','active','real',?)", (NOW,))
        app = FastAPI()
        facial_recognition_ui.register_facial_recognition_routes(app, _shell)
        with TestClient(app) as test_client:
            yield test_client


def _admin_cookie():
    return partner_portal._token("admin@example.test", "administrator")


def _customer_owner_cookie(customer_id="cust-1"):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id)


def _customer_viewer_cookie(customer_id="cust-1"):
    return partner_portal._token("viewer@example.test", "customer_viewer", None, customer_id)


def _cookies(token):
    return {partner_portal.SESSION_COOKIE: token}


# --------------------------------------------------------------- path traversal


@pytest.mark.parametrize(
    "payload",
    [
        "../../etc",
        "..\\..\\windows",
        "cust-1/../cust-2",
        "cust-1/../../secrets",
        "",
        "cust 1",  # space is not in the allowlist either
        "cust-1;rm -rf",
    ],
)
def test_path_traversal_customer_id_is_rejected_on_a_representative_route(client, payload):
    response = client.get("/api/aac/people", params={"customer_id": payload}, cookies=_cookies(_admin_cookie()))
    assert response.status_code == 400


def test_path_traversal_customer_id_creates_no_stray_directory(client, tmp_path, monkeypatch):
    import facial_people

    monkeypatch.setattr(facial_people, "AAC_FACES_FOLDER", tmp_path / "aac_faces")
    response = client.post(
        "/api/aac/people",
        json={"customer_id": "../../outside", "display_name": "Attacker"},
        cookies=_cookies(_admin_cookie()),
    )
    assert response.status_code == 400
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path.parent / "outside").exists()


def test_ordinary_hex_customer_id_still_works(client):
    """The path-safety check must never reject a real, legitimately-
    generated customer_id -- every real one in this codebase is a
    secrets.token_hex()-style value (see customer_registration.py)."""
    with override_target(sqlite_path="/tmp/test_facial_recognition_adversarial_import.db"):
        pass
    response = client.post(
        "/api/aac/people",
        json={"customer_id": "cust-1", "display_name": "Alice"},
        cookies=_cookies(_admin_cookie()),
    )
    assert response.status_code == 200


# --------------------------------------------------------------- stored-XSS regression guard (static)


_UI_SOURCE = Path(__file__).resolve().parent.parent / "facial_recognition_ui.py"


def _script_blocks() -> list[str]:
    text = _UI_SOURCE.read_text(encoding="utf-8")
    return re.findall(r"<script>(.*?)</script>", text, re.S)


def test_every_page_script_defines_the_escape_helper():
    blocks = _script_blocks()
    assert blocks, "expected at least one <script> block in facial_recognition_ui.py"
    defining_blocks = [block for block in blocks if "function aacEsc(" in block]
    # People, Watchlists, Facial Events, and Match Detail each render
    # server-supplied free text and must each define (or, for pages that
    # render nothing dynamic, simply not need) the escape helper.
    assert len(defining_blocks) >= 4


@pytest.mark.parametrize(
    "raw_pattern",
    [
        r"\$\{p\.display_name\}",
        r"\$\{p\.external_reference\|\|''\}",
        r"\$\{w\.name\}",
        r"\$\{e\.matched_person_name",
        r"\$\{e\.matched_watchlist_name",
        r"\$\{e\.event_timestamp\}",
        r"\$\{e\.camera_id\}",
        r"\$\{e\.match_state\}",
        r"\$\{e\.engine\}",
    ],
)
def test_no_known_field_is_interpolated_into_innerhtml_unescaped(raw_pattern):
    """Each of these exact unescaped interpolations was the concrete
    vulnerable code before this hardening pass. Their presence anywhere
    in the file (escaped or not doesn't matter for THIS check -- it's a
    literal string match) would mean the raw, unescaped form crept back
    in; the escaped form is `${aacEsc(p.display_name)}`, which does not
    match this pattern at all."""
    text = _UI_SOURCE.read_text(encoding="utf-8")
    assert re.search(raw_pattern, text) is None, f"found unescaped interpolation matching {raw_pattern!r}"


def test_event_id_path_param_is_html_escaped_before_use_in_attribute():
    text = _UI_SOURCE.read_text(encoding="utf-8")
    # The attribute is built by concatenation now, not an f-string
    # (2026-09-24 test update -- the escaping itself is unchanged).
    assert 'data-event-id="' + "'''" + " + html.escape(event_id) + " + "'''" in text


def test_reflected_event_id_in_match_detail_page_is_escaped(client, monkeypatch):
    """event_id is a raw URL path segment. A value containing HTML-
    meaningful characters must never appear unescaped in the response
    body's data-event-id attribute.

    Starlette's default path converter for {event_id} never matches a
    literal "/" within one segment (a payload containing "/" -- e.g. a
    real "</script>" closing tag -- 404s at the router level before
    this route's own code ever runs, a structural protection this test
    doesn't need to rely on). The payload below is deliberately
    slash-free so it actually reaches aac_event_detail_page(), to prove
    THIS route's own html.escape() call, not routing, is what makes it
    safe."""
    from urllib.parse import quote

    payload = '"><img src=x onerror=alert(1)>'
    # Entitled to Face Access, so the match detail page (the code under
    # test) renders instead of the subscription upsell -- without this the
    # reflected event id never reaches the page at all (2026-09-24).
    monkeypatch.setattr("analytics_entitlements.get_active_analytics_for_customer", lambda customer_id: ["facial_recognition"])
    response = client.get(f"/aac/events/{quote(payload, safe='')}", cookies=_cookies(_customer_viewer_cookie()))
    assert response.status_code == 200
    assert "<img src=x onerror=alert(1)>" not in response.text
    assert "&lt;img" in response.text


# --------------------------------------------------------------- thumbnail access


def test_thumbnail_requires_authentication(client):
    response = client.get("/api/aac/events/whatever/thumbnail", params={"customer_id": "cust-1"})
    assert response.status_code == 401


def test_thumbnail_for_nonexistent_event_is_404(client):
    response = client.get(
        "/api/aac/events/does-not-exist/thumbnail",
        params={"customer_id": "cust-1"},
        cookies=_cookies(_customer_viewer_cookie()),
    )
    assert response.status_code == 404


def test_thumbnail_never_leaks_across_tenants(client, db_path):
    """Even if an event id from another tenant is guessed/known, the
    thumbnail lookup is scoped by customer_id at the database layer
    (get_event_detail()) -- a cust-2 event_id requested with cust-1's
    customer_id must 404, never serve cust-2's file."""
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-2','cust-2','Site 2',?)", (NOW,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-2','cust-2','site-2','AIC-ADV0001',?)", (NOW,))
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,camera_number) VALUES('cam-2','cust-2','site-2','appl-2','Camera 2','active',?,9)", (NOW,))
            db.execute(
                "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,confidence,object_count,event_timestamp,created_at) "
                "VALUES('devt-2','cust-2','site-2','appl-2','cam-2','devt-2','facial_recognition',0.9,1,?,?)",
                (NOW, NOW),
            )
            db.execute(
                "INSERT INTO facial_events(id,detection_event_id,customer_id,site_id,camera_id,match_state,confidence,engine,face_thumbnail_path,created_at) "
                "VALUES('fevt-2','devt-2','cust-2','site-2','cam-2','unknown',0.1,'haar_intensity','/tmp/should-not-be-served.jpg',?)",
                (NOW,),
            )
    response = client.get(
        "/api/aac/events/fevt-2/thumbnail",
        params={"customer_id": "cust-1"},
        cookies=_cookies(_customer_viewer_cookie(customer_id="cust-1")),
    )
    assert response.status_code == 404


def test_customer_viewer_cannot_request_another_customers_thumbnail_by_switching_customer_id(client):
    response = client.get(
        "/api/aac/events/anything/thumbnail",
        params={"customer_id": "cust-2"},
        cookies=_cookies(_customer_viewer_cookie(customer_id="cust-1")),
    )
    assert response.status_code == 403


# --------------------------------------------------------------- injection-style inputs are inert


def test_search_parameter_with_sql_metacharacters_is_treated_as_literal_text(client, db_path):
    client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "Alice"}, cookies=_cookies(_customer_owner_cookie()))
    response = client.get(
        "/api/aac/people",
        params={"customer_id": "cust-1", "search": "'; DROP TABLE facial_people; --"},
        cookies=_cookies(_customer_viewer_cookie()),
    )
    assert response.status_code == 200
    assert response.json() == {"people": []}
    # The table must still exist and still contain Alice -- a real SQL
    # injection would have dropped it (parameterized queries in
    # facial_people.list_people() make this structurally impossible,
    # this just proves it end to end through the real route).
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM facial_people").fetchone()["n"]
    assert count == 1


def test_display_name_containing_markup_is_stored_verbatim_not_sanitized_or_rejected(client):
    """Storage is a dumb text store by design -- escaping is the
    render-time responsibility (aacEsc() in the client scripts, checked
    statically above), not the enrollment API's. This test pins that
    division of responsibility: the API must not reject or mutate the
    input, and must return exactly what was stored when read back."""
    payload = "<img src=x onerror=alert(1)>"
    created = client.post(
        "/api/aac/people",
        json={"customer_id": "cust-1", "display_name": payload},
        cookies=_cookies(_customer_owner_cookie()),
    )
    assert created.status_code == 200
    person_id = created.json()["person_id"]
    fetched = client.get(f"/api/aac/people/{person_id}", params={"customer_id": "cust-1"}, cookies=_cookies(_customer_viewer_cookie()))
    assert fetched.json()["display_name"] == payload


# --------------------------------------------------------------- cross-tenant watchlist wiring, at the route layer


def test_customer_owner_cannot_add_a_foreign_customers_person_to_their_own_watchlist(client):
    foreign_person = client.post("/api/aac/people", json={"customer_id": "cust-2", "display_name": "Mallory"}, cookies=_cookies(_admin_cookie()))
    foreign_person_id = foreign_person.json()["person_id"]
    own_watchlist = client.post("/api/aac/watchlists", json={"customer_id": "cust-1", "name": "Banned"}, cookies=_cookies(_customer_owner_cookie()))
    watchlist_id = own_watchlist.json()["watchlist_id"]
    response = client.post(
        f"/api/aac/watchlists/{watchlist_id}/members",
        json={"customer_id": "cust-1", "person_id": foreign_person_id},
        cookies=_cookies(_customer_owner_cookie()),
    )
    assert response.status_code == 404
    members = client.get(f"/api/aac/watchlists/{watchlist_id}/members", params={"customer_id": "cust-1"}, cookies=_cookies(_customer_owner_cookie()))
    assert members.json()["members"] == []


# --------------------------------------------------------------- role coverage on every mutating route


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("patch", "/api/aac/people/whatever", {"customer_id": "cust-1", "display_name": "X"}),
        ("post", "/api/aac/watchlists", {"customer_id": "cust-1", "name": "X"}),
        ("put", "/api/aac/settings", {"customer_id": "cust-1", "min_confidence": 0.5}),
    ],
)
def test_customer_viewer_is_rejected_on_every_manage_route(client, method, path, body):
    response = getattr(client, method)(path, json=body, cookies=_cookies(_customer_viewer_cookie()))
    assert response.status_code == 403


# --------------------------------------------------------------- relay only runs behind its own explicit flag, and only mock


def test_main_py_only_passes_a_relay_provider_when_the_access_control_flag_gates_it():
    """Structural guard, not a behavioral one -- updated 2026-09-16.
    Facial Recognition and Face Access are one connected feature: the
    identity match -> authorization decision -> access-control command
    chain (relay_control.py, facial_events.evaluate_access_rules()) is
    real, tested, and now wired into the live save_yolo_events() hook,
    but ONLY behind an explicit opt-in
    (relay_control.FACIAL_ACCESS_CONTROL_ENABLED, default false) --
    never unconditionally. This test fails loudly if a future edit
    either (a) passes relay_provider= unconditionally (bypassing the
    flag), or (b) constructs anything other than
    relay_control.get_provider() at that call site, which is what keeps
    this codebase's "never touch real hardware" guarantee true even
    with the chain enabled -- see test_relay_control.py's own
    test_get_provider_is_always_a_mock_regardless_of_the_flag for the
    companion guarantee that get_provider() itself can never resolve to
    anything but MockRelayProvider."""
    main_source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert main_source.count("facial_events.record_facial_events(") == 1, (
        "expected exactly one record_facial_events(...) call site in main.py"
    )
    start = main_source.index("facial_events.record_facial_events(")
    # A fixed window rather than a balanced-parens regex: the call site's
    # own arguments contain a nested call (relay_control.get_provider())
    # inside a conditional expression, which a simple `[^)]*` regex
    # cannot span correctly.
    call_site = " ".join(main_source[start:start + 400].split())
    assert "relay_provider=relay_control.get_provider() if relay_control.FACIAL_ACCESS_CONTROL_ENABLED else None" in call_site, (
        "main.py's live detection hook must only pass a relay provider when explicitly gated by "
        "relay_control.FACIAL_ACCESS_CONTROL_ENABLED, and only via relay_control.get_provider() -- see this test's own docstring"
    )
