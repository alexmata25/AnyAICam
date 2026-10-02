"""Shared talk-down service contract (2026-09-23).

talk_sessions.py's start/stop REST routes (POST /api/customer/cameras/
{camera_id}/talk/start, POST /api/customer/talk/sessions/{session_id}/
stop) are already the one, shared talk-down entry point: Live's own
wireTalkMic() calls them directly today, and they are the exact
interface any other feature needing talk-down -- AACO's natural-
language layer, AAC Voice Call's active-call screen -- should call too,
rather than each building its own session/authorization logic.

This file does NOT add a new code path. It proves the EXISTING contract
already satisfies what a caller other than the browser needs, from that
caller's own point of view:

  - AACO invocation path: an authenticated customer session (the same
    partner_identity() cookie AACO's own command handler already
    receives -- see app/aaco.py's _require_customer()) can start/stop a
    talk session through this exact REST contract with no special
    casing, exactly as a natural-language "talk to whoever's at the
    front door" intent would need to.
  - AAC Voice Call invocation path: same contract, same authorization
    model -- proven here because app/aac_voice_call.py itself lives on
    a separate, unmerged branch (feature/aac-voice-call-phase1-20260923)
    this worktree does not have; the CONTRACT it needs is what's tested
    here, not that branch's own code.
  - Repeated start/stop and disconnect-adjacent session-state semantics
    (a session left "requested" and then explicitly stopped without
    ever having a WebSocket attach -- the REST-layer equivalent of a
    caller that opens a session, then disconnects/changes its mind
    before the audio socket ever connects).
  - Camera-count-agnostic: the same assertions repeated across differently-
    sized camera fleets (3 and 11), proving nothing here assumes a
    specific fleet size.

Reuses test_talk_down_foundation.py's own seeding helpers and fixtures
verbatim (same file, same DB schema, same authorization model) rather
than re-deriving them, since this file is testing the same contract
from a different caller's perspective, not a different system.
"""

import pytest

from test_talk_down_foundation import (  # noqa: F401  (customer_client re-exported as a fixture)
    customer_client,
    db_path,
    _owner_cookie,
    _seed_camera,
    _seed_tenant,
)
from database_backend import override_target
from partner_db import connection


def _seed_fleet(db_path, customer_id, camera_count, supported_camera_number=1):
    """Seeds camera_count cameras for one customer; only
    supported_camera_number is talk_down_supported=1 -- the rest are
    unverified (NULL), matching a realistic fleet where capability
    discovery hasn't run against every camera yet."""
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db, customer_id=customer_id)
            camera_ids = []
            for n in range(1, camera_count + 1):
                camera_id = f"cam-{customer_id}-{n}"
                _seed_camera(
                    db, camera_id, customer_id=customer_id, camera_number=n,
                    name=f"Camera {n}",
                    talk_down_supported=(1 if n == supported_camera_number else None),
                )
                camera_ids.append(camera_id)
            db.commit()
    return camera_ids


# ---------------------------------------------------- AACO invocation path


@pytest.mark.parametrize("camera_count", [3, 11])
def test_aaco_style_caller_can_start_and_stop_a_talk_session(customer_client, db_path, camera_count):
    """Exercises the exact two calls AACO's own natural-language talk
    intent would need to make, once wired: nothing here is AACO-
    specific code -- it's the same partner_identity() cookie and the
    same two REST routes Live already uses, proving no new backend
    surface is needed for AACO to reuse this service."""
    camera_ids = _seed_fleet(db_path, "cust-aaco", camera_count, supported_camera_number=1)
    cookies = {"anyaicam_partner_session": _owner_cookie("cust-aaco")}

    start = customer_client.post(f"/api/customer/cameras/{camera_ids[0]}/talk/start", cookies=cookies)
    assert start.status_code == 200
    session_id = start.json()["session_id"]
    assert start.json()["status"] == "requested"

    stop = customer_client.post(f"/api/customer/talk/sessions/{session_id}/stop", cookies=cookies)
    assert stop.status_code == 200
    assert stop.json()["status"] == "stopped"


def test_aaco_style_caller_is_rejected_for_a_camera_outside_its_own_fleet_even_if_it_knows_the_id():
    """A natural-language layer that resolved the WRONG camera id (e.g.
    a bug in its own entity-resolution logic, or an attempt to probe
    another tenant's camera by guessing an id) must be rejected by this
    contract exactly like a browser would be -- there is no AACO-
    specific bypass of camera ownership."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db_path_local = Path(tmp) / "test_aaco_cross_tenant.db"
        with override_target(sqlite_path=str(db_path_local)):
            from partner_db import initialize_database
            initialize_database()
            with connection() as db:
                _seed_tenant(db, customer_id="cust-a", site_id="site-a", appliance_id="appl-a", cloud_id="AIC-TESTAAAA")
                _seed_tenant(db, customer_id="cust-b", site_id="site-b", appliance_id="appl-b", cloud_id="AIC-TESTBBBB")
                _seed_camera(db, "cam-b-1", customer_id="cust-b", site_id="site-b", appliance_id="appl-b", camera_number=1, talk_down_supported=1)
                db.commit()
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            import talk_sessions

            app = FastAPI()
            talk_sessions.register_talk_session_routes(app)
            with TestClient(app, follow_redirects=False) as client:
                cookies = {"anyaicam_partner_session": _owner_cookie("cust-a")}
                response = client.post("/api/customer/cameras/cam-b-1/talk/start", cookies=cookies)
    assert response.status_code == 404


# ----------------------------------------------- AAC Voice Call invocation path


@pytest.mark.parametrize("camera_count", [3, 11])
def test_voice_call_style_caller_can_start_a_session_for_the_entrance_camera(customer_client, db_path, camera_count):
    """AAC Voice Call's own active-call screen (a separate, unmerged
    branch) needs to open a talk session for the specific camera a
    visitor rang at, using the homeowner's own authenticated session --
    the exact same call an AACO-driven or browser-driven talk press
    already makes. Proven at fleet sizes that do not match the 5-camera
    Ryzen pilot, so nothing here can be accidentally tied to it."""
    camera_ids = _seed_fleet(db_path, "cust-voicecall", camera_count, supported_camera_number=2)
    entrance_camera_id = camera_ids[1]  # the one seeded talk_down_supported=1
    cookies = {"anyaicam_partner_session": _owner_cookie("cust-voicecall")}

    start = customer_client.post(f"/api/customer/cameras/{entrance_camera_id}/talk/start", cookies=cookies)
    assert start.status_code == 200


def test_voice_call_style_caller_gets_an_honest_409_for_an_entrance_camera_without_confirmed_support(customer_client, db_path):
    """If a customer configures a camera as their AAC Voice Call
    entrance camera before capability discovery has ever run against
    it (talk_down_supported is NULL, not yet 0 or 1), the shared
    service must refuse with the same honest "not verified" signal
    Live's own mic button already renders as disabled for -- never a
    silent success that then has no real audio path."""
    camera_ids = _seed_fleet(db_path, "cust-unverified", 4, supported_camera_number=None)
    cookies = {"anyaicam_partner_session": _owner_cookie("cust-unverified")}
    response = customer_client.post(f"/api/customer/cameras/{camera_ids[0]}/talk/start", cookies=cookies)
    assert response.status_code == 409
    assert "not verified" in response.json()["detail"].lower()


# ------------------------------------------------- repeated start/stop, disconnect-adjacent


def test_repeated_start_stop_on_the_same_camera_produces_independent_sessions(customer_client, db_path):
    camera_ids = _seed_fleet(db_path, "cust-repeat", 3, supported_camera_number=1)
    cookies = {"anyaicam_partner_session": _owner_cookie("cust-repeat")}
    camera_id = camera_ids[0]

    session_ids = set()
    for _ in range(3):
        start = customer_client.post(f"/api/customer/cameras/{camera_id}/talk/start", cookies=cookies)
        assert start.status_code == 200
        session_id = start.json()["session_id"]
        assert session_id not in session_ids
        session_ids.add(session_id)
        stop = customer_client.post(f"/api/customer/talk/sessions/{session_id}/stop", cookies=cookies)
        assert stop.status_code == 200
        assert stop.json()["status"] == "stopped"


def test_stopping_a_session_that_never_had_its_audio_socket_attach_still_succeeds(customer_client, db_path):
    """The REST-layer equivalent of "caller opened a session, then
    disconnected/changed its mind before the audio WebSocket ever
    connected" -- e.g. AACO asked a clarifying question and the
    customer declined, or the browser tab closed before the mic
    permission prompt resolved. Must cleanly transition to 'stopped',
    the same durable outcome talk_audio_relay.py's own _end_relay()
    forces when a real socket WAS attached and then dropped."""
    camera_ids = _seed_fleet(db_path, "cust-nosocket", 5, supported_camera_number=3)
    cookies = {"anyaicam_partner_session": _owner_cookie("cust-nosocket")}
    start = customer_client.post(f"/api/customer/cameras/{camera_ids[2]}/talk/start", cookies=cookies)
    session_id = start.json()["session_id"]

    stop = customer_client.post(f"/api/customer/talk/sessions/{session_id}/stop", cookies=cookies)
    assert stop.status_code == 200
    assert stop.json()["status"] == "stopped"

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT state FROM customer_talk_sessions WHERE id=?", (session_id,)).fetchone()
    assert row["state"] == "stopped"
