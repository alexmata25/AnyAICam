"""Integration coverage for main._ClassicAacoBoundary -- the concrete
adapter that binds AACO's provider-independent command schema
(app/aaco.py, tests/test_aaco.py) to Classic VMS's real, tenant-scoped
functions. test_aaco_web.py deliberately never exercises this class
(it injects a fully independent ControlledVms fake instead), so this
file exists to catch exactly the class of bug that gap allows through:
after rebasing feature/aaco-command-engine onto the current
reconciliation baseline, _ClassicAacoBoundary.camera_status() called a
module-level `customer_camera_status(camera_id, request)` that no
longer exists under that name/signature anywhere reachable, and
register_aaco_routes()'s identity_provider referenced a bare
`partner_identity` name never imported at module scope in main.py --
both would have raised NameError on the very first real AACO request
despite all 32 pre-existing AACO tests passing. Both are fixed in this
same rebase; this file proves the fix against real database rows
instead of a fake boundary, using the same fixture shape
test_playback_camera_default_selection_order.py already established
for this exact class of "real function, real DB, monkeypatched
identity" test.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import aaco
import main
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_aaco_classic_boundary_integration.db"


def _seed_base_tenant(conn, customer_id="cust-1", partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, "Test Co", f"{customer_id}@example.com", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer_id}", customer_id, "Main", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
        (f"appl-{customer_id}", customer_id, f"site-{customer_id}", f"AIC-{customer_id}", "2026-01-01"),
    )
    conn.commit()


def _seed_camera(conn, camera_id, name, camera_number, customer_id="cust-1"):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, f"site-{customer_id}", f"appl-{customer_id}", camera_number, name, "2026-01-01"),
    )
    conn.commit()


def _request(camera=None):
    from types import SimpleNamespace
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: camera if key == "camera" else default))


def _owner_identity(customer_id="cust-1"):
    return {"role": "customer_owner", "customer_id": customer_id, "email": "owner@example.test"}


@pytest.fixture()
def owner_seeded(db_path, monkeypatch):
    """One customer, two real cameras (1 online, 2 offline), one
    placeholder (camera_number NULL) that must never surface anywhere."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_camera(conn, "cam-1", "Front Entrance", 1)
        _seed_camera(conn, "cam-2", "Back Lot", 2)
        _seed_camera(conn, "cam-placeholder", "Camera 3", None)
        conn.execute(
            "INSERT INTO appliance_camera_status(appliance_id,camera_id,name,online,recording,analytics,updated_at) "
            "VALUES('appl-cust-1','cam-1','Front Entrance',1,1,1,'2026-09-16T00:00:00')"
        )
        conn.execute(
            "INSERT INTO appliance_camera_status(appliance_id,camera_id,name,online,recording,analytics,updated_at) "
            "VALUES('appl-cust-1','cam-2','Back Lot',0,0,0,'2026-09-16T00:00:00')"
        )
        conn.commit()
        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        yield conn


def test_camera_status_reflects_real_online_offline_state_and_excludes_placeholders(owner_seeded):
    boundary = main._ClassicAacoBoundary(_request())
    result = boundary.camera_status(_owner_identity())
    assert result["kind"] == "status"
    by_label = {row["label"]: row["state"] for row in result["cameras"]}
    assert by_label == {"Front Entrance": "online", "Back Lot": "offline"}, (
        f"expected exactly the 2 real cameras with their real online/offline state, no placeholder; got {result['cameras']!r}"
    )


def test_authorized_camera_resolves_by_number_and_rejects_unknown_camera(owner_seeded):
    boundary = main._ClassicAacoBoundary(_request())
    identity = _owner_identity()
    assert boundary.authorized_camera(identity, "camera-1") is not None
    assert boundary.authorized_camera(identity, "camera-99") is None


def test_authorized_camera_resolves_by_display_name(owner_seeded):
    boundary = main._ClassicAacoBoundary(_request())
    identity = _owner_identity()
    resolved = boundary.authorized_camera(identity, "camera-name:front entrance")
    assert resolved is not None and resolved["id"] == "cam-1"


def test_live_view_returns_an_authorized_deep_link_for_a_real_camera(owner_seeded):
    boundary = main._ClassicAacoBoundary(_request())
    result = boundary.live_view(_owner_identity(), "camera-1")
    assert result["kind"] == "live"
    assert "/customer/cameras/" in result["href"] and result["href"].endswith("/live")
    assert result["context"] == {"camera_id": "camera-1"}


def test_live_view_raises_permission_error_for_a_camera_outside_the_tenant(owner_seeded):
    boundary = main._ClassicAacoBoundary(_request())
    with pytest.raises(PermissionError):
        boundary.live_view(_owner_identity(), "camera-99")


def test_playback_reports_no_media_honestly_when_no_recording_exists(owner_seeded):
    boundary = main._ClassicAacoBoundary(_request())
    now = datetime.now(timezone.utc)
    result = boundary.playback(_owner_identity(), "camera-1", now - timedelta(minutes=5), now)
    assert result["kind"] == "playback"
    assert "currently unavailable" in result["message"]
    assert result["context"]["camera_id"] == "camera-1"


def test_playback_finds_existing_recording_metadata_near_the_requested_time(owner_seeded):
    conn = owner_seeded
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
        "VALUES('rec-1','cust-1','site-cust-1','appl-cust-1','cam-1','k.mp4','2026-09-16T10:00:00','2026-09-16T10:05:00','available','2026-09-16T10:05:00')"
    )
    conn.commit()
    boundary = main._ClassicAacoBoundary(_request())
    result = boundary.playback(_owner_identity(), "camera-1", datetime(2026, 9, 16, 10, 2), datetime(2026, 9, 16, 10, 2))
    assert "1 existing recording" in result["message"]


def test_search_events_scopes_to_the_tenant_and_the_requested_window(owner_seeded):
    conn = owner_seeded
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
        "VALUES('evt-1','cust-1','site-cust-1','appl-cust-1','cam-1','local-1','person','2026-09-16T10:00:00','2026-09-16T10:00:00')"
    )
    conn.commit()
    boundary = main._ClassicAacoBoundary(_request())
    result = boundary.search_events(
        _owner_identity(), event_type="person", camera_id=None,
        start=datetime(2026, 9, 16, 9, 0), end=datetime(2026, 9, 16, 11, 0),
    )
    assert result["kind"] == "events"
    assert len(result["events"]) == 1
    assert "Front Entrance" in result["events"][0]["label"]


def test_search_events_matches_the_same_raw_type_buckets_the_investigate_page_uses(owner_seeded):
    """2026-09-19: AACO's event_type filter must find the same rows a
    customer clicking the Investigate page's own "Motion"/"License
    Plate" filter chip already finds -- including raw stored values
    like 'smart_motion' and 'plate' that are not literally the
    canonical category name. Proves _aaco_event_category() is actually
    wired into search_events(), not just unit-tested in isolation."""
    conn = owner_seeded
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
        "VALUES('evt-motion','cust-1','site-cust-1','appl-cust-1','cam-1','local-1','smart_motion','2026-09-16T10:00:00','2026-09-16T10:00:00')"
    )
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
        "VALUES('evt-plate','cust-1','site-cust-1','appl-cust-1','cam-1','local-2','plate','2026-09-16T10:01:00','2026-09-16T10:01:00')"
    )
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
        "VALUES('evt-intrusion','cust-1','site-cust-1','appl-cust-1','cam-1','local-3','intrusion','2026-09-16T10:02:00','2026-09-16T10:02:00')"
    )
    conn.commit()
    boundary = main._ClassicAacoBoundary(_request())
    window = {"start": datetime(2026, 9, 16, 9, 0), "end": datetime(2026, 9, 16, 11, 0)}
    motion = boundary.search_events(_owner_identity(), event_type="motion", camera_id=None, **window)
    lpr = boundary.search_events(_owner_identity(), event_type="lpr", camera_id=None, **window)
    intrusion = boundary.search_events(_owner_identity(), event_type="intrusion", camera_id=None, **window)
    assert len(motion["events"]) == 1
    assert len(lpr["events"]) == 1
    assert len(intrusion["events"]) == 1
    # Asking for "person" must not accidentally also match motion/lpr/intrusion rows.
    person = boundary.search_events(_owner_identity(), event_type="person", camera_id=None, **window)
    assert person["events"] == []


def test_search_events_finds_nothing_outside_the_requested_window(owner_seeded):
    conn = owner_seeded
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
        "VALUES('evt-1','cust-1','site-cust-1','appl-cust-1','cam-1','local-1','person','2026-09-16T02:00:00','2026-09-16T02:00:00')"
    )
    conn.commit()
    boundary = main._ClassicAacoBoundary(_request())
    result = boundary.search_events(
        _owner_identity(), event_type="person", camera_id=None,
        start=datetime(2026, 9, 16, 9, 0), end=datetime(2026, 9, 16, 11, 0),
    )
    assert result["events"] == []


def test_a_second_tenants_camera_and_events_are_never_visible(db_path, monkeypatch):
    """Cross-tenant isolation at the real _ClassicAacoBoundary layer,
    not just inside aaco.py's own generic authorization gate."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn, customer_id="cust-1", partner_id="partner-1")
        _seed_base_tenant(conn, customer_id="cust-2", partner_id="partner-1")
        _seed_camera(conn, "cam-1", "Tenant A Camera", 1, customer_id="cust-1")
        _seed_camera(conn, "cam-2", "Tenant B Camera", 1, customer_id="cust-2")
        conn.execute(
            "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
            "VALUES('evt-b','cust-2','site-cust-2','appl-cust-2','cam-2','local-1','person','2026-09-16T10:00:00','2026-09-16T10:00:00')"
        )
        conn.commit()
        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))

        boundary = main._ClassicAacoBoundary(_request())
        identity = _owner_identity("cust-1")
        assert boundary.authorized_camera(identity, "camera-1") is not None
        # A customer_owner has live access to their own full fleet without
        # a separate per-camera grant (that restriction is customer_viewer-
        # only) -- this proves tenant A's own camera resolves correctly,
        # the real cross-tenant proof is the events assertion below.
        assert boundary.live_view(identity, "camera-1")["kind"] == "live"
        result = boundary.search_events(
            identity, event_type=None, camera_id=None,
            start=datetime(2026, 9, 16, 9, 0), end=datetime(2026, 9, 16, 11, 0),
        )
        assert result["events"] == [], "tenant A must never see tenant B's detection_events row"


# ---------------------------------------------------------------------------
# 2026-09-23: natural-language camera resolution. A customer's phrase for
# "which camera" is often not that camera's exact display name -- these
# prove _ClassicAacoBoundary's fuzzy fallback (main.py's
# _aaco_fuzzy_camera_matches()/_resolve_camera()/_camera_ambiguity()) reaches
# the one real, seeded camera ("Front Entrance") a phrase like "the
# entrance"/"my front camera"/"outside the front" plausibly means, end to
# end through the real aaco.parse()->aaco.execute() pipeline against a real
# database -- not just the exact-name unit coverage above.
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("text", [
    "Show me the front entrance.",
    "Let me see the entrance.",
    "What's happening at the entrance?",
    "What does the front entrance look like right now?",
    "Pull up my front camera.",
    "I want to see out front.",
    "Open the camera by the entrance.",
])
def test_many_natural_phrasings_resolve_to_the_one_real_seeded_camera(owner_seeded, text):
    command = aaco.DeterministicLanguageAdapter().parse(text, now=NOW)
    assert isinstance(command, aaco.AacoCommand), f"{text!r} did not parse to a command: {command!r}"
    boundary = main._ClassicAacoBoundary(_request())
    result = aaco.execute(command, identity=_owner_identity(), vms=boundary)
    assert not isinstance(result, aaco.Clarification), f"{text!r} unexpectedly asked for clarification: {result!r}"
    assert result["kind"] == "live"
    # The real proof: whichever raw phrase parse() extracted, execute()
    # must have resolved it, through the fuzzy fallback, to cam-1
    # specifically ("Front Entrance", the one real seeded camera) --
    # confirmed via the href, which always carries the resolved camera's
    # own database id, never the raw pre-resolution token text.
    assert "cam-1" in result["href"], f"{text!r} resolved to the wrong camera: {result!r}"


def test_ambiguous_natural_phrase_asks_which_camera_never_guesses(db_path, monkeypatch):
    """Two real cameras both plausibly match "front" -- must ask which
    one, never silently pick the first/either."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_camera(conn, "cam-1", "Front Door", 1)
        _seed_camera(conn, "cam-2", "Front Gate", 2)
        conn.commit()
        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())

        boundary = main._ClassicAacoBoundary(_request())
        command = aaco.DeterministicLanguageAdapter().parse("Show me the front.", now=NOW)
        result = aaco.execute(command, identity=_owner_identity(), vms=boundary)
        assert isinstance(result, aaco.Clarification)
        assert "Front Door" in result.message and "Front Gate" in result.message


def test_unambiguous_camera_among_several_still_resolves(db_path, monkeypatch):
    """A phrase that's ambiguous in the two-camera test above must still
    resolve cleanly once it's specific enough to pick exactly one --
    proves the fuzzy matcher scores by best overlap, not "any overlap
    at all count as ambiguous."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_camera(conn, "cam-1", "Front Door", 1)
        _seed_camera(conn, "cam-2", "Front Gate", 2)
        conn.commit()
        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())

        boundary = main._ClassicAacoBoundary(_request())
        command = aaco.DeterministicLanguageAdapter().parse("Show me who's at the door.", now=NOW)
        result = aaco.execute(command, identity=_owner_identity(), vms=boundary)
        assert not isinstance(result, aaco.Clarification)
        assert "cam-1" in result["href"]


def test_natural_language_camera_reference_can_never_resolve_to_another_tenants_camera(db_path, monkeypatch):
    """Tenant isolation, specifically for the new fuzzy path: tenant A
    asking for "the front" must only ever be scored against tenant A's
    own cameras, even though tenant B also owns a real camera whose name
    would otherwise fuzzy-match the same phrase."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn, customer_id="cust-1", partner_id="partner-1")
        _seed_base_tenant(conn, customer_id="cust-2", partner_id="partner-1")
        _seed_camera(conn, "cam-a", "Front Entrance", 1, customer_id="cust-1")
        _seed_camera(conn, "cam-b", "Front Gate", 1, customer_id="cust-2")
        conn.commit()
        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))

        boundary = main._ClassicAacoBoundary(_request())
        command = aaco.DeterministicLanguageAdapter().parse("Let me see out front.", now=NOW)
        result = aaco.execute(command, identity=_owner_identity("cust-1"), vms=boundary)
        assert not isinstance(result, aaco.Clarification)
        assert result["kind"] == "live"
        assert "cam-a" in result["href"]
        assert "cam-b" not in result["href"]


def test_talk_intent_is_recognized_and_resolved_but_honestly_not_connected(owner_seeded):
    """The prepared-but-not-wired talk hook: parse() must correctly
    recognize the intent and resolve the real, authorized camera through
    the exact same path every other operation uses -- proving genuine
    understanding, not a stub that only accepts a fixed phrase -- while
    execute() must never fake a working call."""
    command = aaco.DeterministicLanguageAdapter().parse("Talk to the front entrance.", now=NOW)
    assert command == aaco.AacoCommand("talk", camera_id="camera-name:front entrance")
    boundary = main._ClassicAacoBoundary(_request())
    result = aaco.execute(command, identity=_owner_identity(), vms=boundary)
    assert not isinstance(result, aaco.Clarification)
    assert result["kind"] == "status"
    assert "isn't connected yet" in result["message"]
    assert "Front Entrance" in result["message"]


def test_talk_still_enforces_the_same_authorization_as_every_other_operation(owner_seeded):
    boundary = main._ClassicAacoBoundary(_request())
    with pytest.raises(PermissionError):
        aaco.execute(aaco.AacoCommand("talk", camera_id="camera-99"), identity=_owner_identity(), vms=boundary)
