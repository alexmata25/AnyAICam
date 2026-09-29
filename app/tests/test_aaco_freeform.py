"""AACO free-form natural-language input (2026-09-26, aaco_freeform.py).

Differently worded requests -- typed text and voice-transcript text (what
the browser's speech recognition puts in the same box: lowercase, no
punctuation, numbers as words) -- must reach the same existing AACO action;
the exact commands AACO already understood must parse exactly as before;
missing details get a short question; unknown requests get an explanation;
a door unlock or talk command can never come from a loose reading; and the
VMS boundary still authorizes every camera (tenant-scoped)."""
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import aaco_freeform
from aaco import AacoCommand, Clarification, DeterministicLanguageAdapter
from aaco_freeform import HELP_MESSAGE, FreeFormAacoLanguageAdapter
from aaco_web import register_aaco_routes

NOW = datetime(2026, 9, 26, 11, 0)
MIDNIGHT = datetime(2026, 9, 26)
CAMERAS = ["Living Room", "Front Door", "Driveway Left", "Driveway Right", "Bedroom"]


def _parse(text, context=None):
    return FreeFormAacoLanguageAdapter().parse(text, now=NOW, context={**(context or {}), "camera_names": CAMERAS})


def _assert_same(phrases, expected):
    for text in phrases:
        result = _parse(text)
        assert result == expected, f"{text!r} -> {result!r}"

# ------------------------------------------------------------ each intent, many wordings (typed + voice transcript)


def test_live_view_in_many_wordings():
    _assert_same([
        "Show me the front door",                               # typed
        "Can I see the Front Door camera?",
        "What's going on at the front door right now?",
        "front door live please",
        "Let me look at the front door.",
        "hey aaco show the front door",                         # voice transcript
        "aaco what is happening at the front door",
        "pull up front door",
    ], AacoCommand("live_view", camera_id="camera-name:front door"))


def test_people_today_at_a_camera_in_many_wordings():
    _assert_same([
        "Did anyone come to the front door today?",
        "Any people at the Front Door today?",
        "Who was at the front door today?",
        "Show me people at the front door so far today.",
        "were there any visitors at the front door today",      # voice
        "hey aaco did somebody come by the front door today",
    ], AacoCommand("event_search", camera_id="camera-name:front door", event_type="person", start=MIDNIGHT, end=NOW))


def test_motion_in_the_last_half_hour_in_many_wordings():
    _assert_same([
        "Any motion in the last half hour?",
        "Was there movement in the past 30 minutes?",
        "show motion events from the last thirty minutes",      # voice (number word)
        "aaco was there any motion in the last 30 minutes",
    ], AacoCommand("event_search", event_type="motion", start=NOW - timedelta(minutes=30), end=NOW))


def test_latest_event_in_many_wordings():
    latest = AacoCommand("event_search", start=NOW - timedelta(days=7), end=NOW, limit=1)
    _assert_same([
        "Show me the latest event",
        "What was the most recent alert?",
        "what's the last thing that happened",                  # voice
        "hey aaco show the newest event",
    ], latest)
    _assert_same([
        "Latest person at the front door",
        "who was the last visitor at the front door",
    ], AacoCommand("event_search", camera_id="camera-name:front door", event_type="person",
                   start=NOW - timedelta(days=7), end=NOW, limit=1))


def test_last_event_without_a_selected_event_means_the_latest_one():
    """Previously 'show the last event' only worked with an event already
    selected; now it finds the latest one instead of refusing."""
    assert _parse("show the last event").limit == 1
    # With an event selected it still means the previous event, exactly as before.
    context = {"camera_id": "camera-4", "event_at": datetime(2026, 9, 26, 10)}
    assert _parse("show the last event", context) == AacoCommand("event_navigation", camera_id="camera-4", end=datetime(2026, 9, 26, 10))


def test_playback_by_time_in_many_wordings():
    _assert_same([
        "Play back the living room at 9:15 AM",
        "What happened in the Living Room at 9:15 this morning?",
        "Show me the living room at 9:15am",
        "go back to 9:15 on the living room camera",
        "living room footage from 9:15 a.m. today",
        "what happened in the living room at nine fifteen a.m.",  # voice
        "play back the living room at 9 15 am",
    ], AacoCommand("playback", camera_id="camera-name:living room", start=datetime(2026, 9, 26, 9, 15),
                   end=datetime(2026, 9, 26, 9, 20)))


def test_relative_and_yesterday_times():
    assert _parse("replay the bedroom from an hour ago") == AacoCommand(
        "playback", camera_id="camera-name:bedroom", start=NOW - timedelta(hours=1), end=NOW - timedelta(minutes=55))
    assert _parse("show the bedroom yesterday at 3:15 pm").start == datetime(2026, 9, 25, 15, 15)
    assert _parse("bedroom at 10 pm").start == datetime(2026, 9, 25, 22, 0)  # later than now today -> last night
    assert _parse("any cars on the driveway left last night") == AacoCommand(
        "event_search", camera_id="camera-name:driveway left", event_type="vehicle",
        start=MIDNIGHT - timedelta(hours=6), end=MIDNIGHT + timedelta(hours=6))


def test_camera_status_in_many_wordings():
    _assert_same([
        "Is everything online?",
        "Are any cameras offline?",
        "which of my cameras are not working",
        "camera status",
        "hey aaco is any camera down",
    ], AacoCommand("camera_status"))


def test_investigate_style_searches_return_event_results():
    """Event results link to Investigate (the boundary builds those links);
    free-form searches reach the same event_search."""
    result = _parse("Find vehicles on camera 3 this afternoon")
    assert result.operation == "event_search" and result.camera_id == "camera-3" and result.event_type == "vehicle"
    assert _parse("license plates today").event_type == "lpr"
    assert _parse("how many people were counted today").event_type == "people_counting"
    assert _parse("people counting today").event_type == "people_counting"

# ------------------------------------------------------------ the commands AACO already understood are unchanged


@pytest.mark.parametrize("text,context", [
    ("Show Camera 4", None),
    ("show camera 2 yesterday at 3:15 PM", None),
    ("show camera 2 from 15:15 yesterday", None),
    ("person events in the last 2 hours", None),
    ("were there any vehicle events in the past 3 hours", None),
    ("Which cameras are offline?", None),
    ("Show the front entrance", None),
    ("AACO, could you please pull up the back lot?", None),
    ("return to live", {"camera_id": "camera-4"}),
    ("previous event", {"camera_id": "camera-4", "event_at": datetime(2026, 9, 26, 10)}),
    ("go back 10 minutes", {"camera_id": "camera-4", "playback_at": datetime(2026, 9, 26, 10)}),
    ("unlock the front door", None),
    ("let me in", None),
    ("talk to the front door", None),
    ("open the camera by the front door", None),
    ("previous event", None),
    ("show camera", None),
])
def test_existing_commands_parse_exactly_as_before(text, context):
    expected = DeterministicLanguageAdapter().parse(text, now=NOW, context=context)
    assert _parse(text, context) == expected

# ------------------------------------------------------------ safety: physical actions only from the exact grammar


@pytest.mark.parametrize("text", [
    "could you maybe unlock the front door and show me people",
    "open the front door for the delivery guy please",
    "hey aaco say something to whoever is at the front door",
    "please talk down to the driveway left",
])
def test_door_and_talk_requests_never_come_from_a_loose_reading(text):
    result = _parse(text)
    grammar = DeterministicLanguageAdapter().parse(text, now=NOW, context=None)
    if isinstance(grammar, AacoCommand):
        assert result == grammar  # the exact grammar's own command, nothing looser
    else:
        assert isinstance(result, Clarification)
        assert not isinstance(result, AacoCommand)

# ------------------------------------------------------------ ambiguity asks, unknown explains


def test_ambiguous_or_incomplete_requests_ask_a_short_question():
    assert _parse("play back 3:15 pm") == Clarification("Which camera should I play back?")
    assert _parse("replay the living room").message.startswith("What time should playback start?")
    assert _parse("people or cars today").message.startswith("Which should I look for: people or vehicles?")
    assert _parse("show the living room at 25:99") == Clarification("Use a valid time, for example 3:15 PM.")
    assert _parse("show me a live camera") == Clarification("Which camera would you like to see?")
    # Two cameras match "driveway": the token keeps the shared word so the boundary asks which one.
    assert _parse("any cars on the driveway today").camera_id == "camera-name:driveway"


@pytest.mark.parametrize("text", ["What's the weather tomorrow?", "tell me a joke", "order more printer paper"])
def test_unknown_requests_explain_what_aaco_can_do(text):
    assert _parse(text) == Clarification(HELP_MESSAGE)


def test_show_followed_by_an_event_request_is_no_longer_a_camera_lookup():
    """The grammar's catch-all turned this into a camera named 'person
    events this morning'."""
    result = _parse("show person events this morning")
    assert result == AacoCommand("event_search", event_type="person", start=MIDNIGHT + timedelta(hours=5), end=NOW)


def test_command_words_never_pick_a_camera():
    """Found in the browser run: "play back three fifteen pm" chose a camera
    named "Back Lot" because of the word "back" in "play back"."""
    names = ["Front Entrance", "Back Lot", "Back Yard Gate"]
    parse = lambda text: FreeFormAacoLanguageAdapter().parse(text, now=NOW, context={"camera_names": names})
    assert parse("play back three fifteen pm") == Clarification("Which camera should I play back?")
    assert parse("go back to 3:15 pm") == Clarification("Which camera should I play back?")
    assert parse("play back the back lot at 3:15 pm").camera_id == "camera-name:back lot"
    assert parse("go back to 3:15 pm on the back yard gate").camera_id == "camera-name:back yard gate"
    assert parse("did anyone come back to the yard today").camera_id == "camera-name:back yard gate"  # "yard" is the camera word


def test_without_a_camera_list_the_place_phrase_is_still_found():
    parser = aaco_freeform.FreeFormIntentParser()
    result = parser.parse("any people on the driveway today", now=NOW)
    assert result.camera_id == "camera-name:driveway" and result.event_type == "person"

# ------------------------------------------------------------ the web route: typed and voice take the same path


class RecordingVms:
    def __init__(self, tenant="tenant-a"):
        self.calls, self.tenant = [], tenant

    def camera_names(self, identity):
        return ["Front Door", "Living Room"] if identity["customer_id"] == "tenant-a" else ["Warehouse"]

    def authorized_camera(self, identity, camera_id):
        self.calls.append(("authorized_camera", camera_id))
        own = {"tenant-a": {"camera-name:front door", "camera-name:living room"}, "tenant-b": {"camera-name:warehouse"}}
        return {"id": camera_id} if camera_id in own[identity["customer_id"]] else None

    def search_events(self, identity, **kwargs):
        self.calls.append(("events", kwargs))
        return {"kind": "events", "message": "ok", "events": []}

    def playback(self, identity, camera_id, start, end):
        self.calls.append(("playback", camera_id, start))
        return {"kind": "playback", "message": "ok", "href": "/playback"}

    def live_view(self, identity, camera_id):
        self.calls.append(("live", camera_id))
        return {"kind": "live", "message": "ok", "href": "/live"}

    def camera_status(self, identity):
        return {"kind": "status", "message": "ok", "cameras": []}


def _client(customer="tenant-a"):
    app, vms = FastAPI(), RecordingVms()
    register_aaco_routes(app, lambda title, *args: "", identity_provider=lambda _r: {"role": "customer_owner", "customer_id": customer},
                         vms_factory=lambda _r: vms, now=lambda: NOW, language_adapter_factory=FreeFormAacoLanguageAdapter)
    return TestClient(app), vms


@pytest.mark.parametrize("typed,spoken", [
    ("Did anyone come to the front door today?", "did anyone come to the front door today"),
    ("What happened in the Living Room at 9:15 AM?", "what happened in the living room at nine fifteen a.m."),
    ("Show me the front door.", "hey aaco show me the front door"),
])
def test_typed_and_voice_transcript_reach_the_same_action(typed, spoken):
    client_a, vms_a = _client()
    client_b, vms_b = _client()
    assert client_a.post("/api/aaco/command", json={"command": typed}).json() == client_b.post("/api/aaco/command", json={"command": spoken}).json()
    assert vms_a.calls == vms_b.calls and vms_a.calls


def test_route_uses_server_camera_names_and_enforces_the_tenant():
    client, vms = _client("tenant-a")
    assert client.post("/api/aaco/command", json={"command": "any people at the front door today"}).status_code == 200
    events = next(call for call in vms.calls if call[0] == "events")[1]
    assert events["camera_id"] == "camera-name:front door" and events["event_type"] == "person"
    # Another tenant naming this tenant's camera: it isn't in their list, so it
    # is never matched (no hint it exists) and nothing is played...
    other, other_vms = _client("tenant-b")
    response = other.post("/api/aaco/command", json={"command": "play back the front door at 9:15 am"})
    assert response.json() == {"kind": "clarification", "message": "Which camera should I play back?"}
    assert not any(call[0] == "playback" for call in other_vms.calls)
    # ...and a camera token they don't own is refused by the boundary.
    response = other.post("/api/aaco/command", json={"command": "show camera 4"})
    assert response.status_code == 403
    # A client can't inject camera names through the request context.
    response = other.post("/api/aaco/command", json={"command": "play back the front door at 9:15 am",
                                                     "context": {"camera_names": ["Front Door"]}})
    assert response.status_code in (200, 400, 403) and not any(call[0] == "playback" for call in other_vms.calls)

# ------------------------------------------------------------ the real boundary: camera filter and "latest"


def _seed(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p1','P','2026-01-01')")
    for customer in ("cust-1", "cust-2"):
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)", (customer, "p1", customer, f"{customer}@e.test", "active", "x"))
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer}", customer, "Main", "x"))
        conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (f"appl-{customer}", customer, f"site-{customer}", f"AIC-{customer}", "x"))
    for camera, name, number, customer in (("cam-1", "Front Entrance", 1, "cust-1"), ("cam-2", "Back Lot", 2, "cust-1"), ("cam-9", "Warehouse", 1, "cust-2")):
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES(?,?,?,?,?,?,?)",
                     (camera, customer, f"site-{customer}", f"appl-{customer}", number, name, "x"))
    for event, customer, camera, kind, ts in (("e1", "cust-1", "cam-1", "person", "2026-09-26T09:00:00"), ("e2", "cust-1", "cam-2", "person", "2026-09-26T10:00:00"),
                                             ("e3", "cust-1", "cam-1", "person", "2026-09-26T10:30:00"), ("e4", "cust-2", "cam-9", "person", "2026-09-26T10:45:00")):
        conn.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (event, customer, f"site-{customer}", f"appl-{customer}", camera, event, kind, ts, ts))
    conn.commit()
    conn.close()


def test_real_boundary_filters_by_the_named_camera_and_returns_the_latest(tmp_path, monkeypatch):
    import main
    import partner_portal
    from database_backend import override_target
    from partner_db import initialize_database
    identity = {"role": "customer_owner", "customer_id": "cust-1", "email": "o@e.test"}
    with override_target(sqlite_path=tmp_path / "freeform.db"):
        initialize_database()
        _seed(tmp_path / "freeform.db")
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: identity)
        boundary = main._ClassicAacoBoundary(SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default)))
        assert set(boundary.camera_names(identity)) == {"Front Entrance", "Back Lot"}  # never another tenant's
        from aaco import execute
        adapter = FreeFormAacoLanguageAdapter()
        context = {"camera_names": boundary.camera_names(identity)}
        today = adapter.parse("any people at the front entrance today", now=NOW, context=context)
        result = execute(today, identity=identity, vms=boundary)
        assert [e["timestamp"] for e in result["events"]] == ["2026-09-26T10:30:00", "2026-09-26T09:00:00"]  # cam-1 only
        latest = adapter.parse("who was the last person at the front entrance", now=NOW, context=context)
        result = execute(latest, identity=identity, vms=boundary)
        assert result["message"] == "Latest event." and [e["timestamp"] for e in result["events"]] == ["2026-09-26T10:30:00"]
        everywhere = execute(adapter.parse("show me the latest event", now=NOW, context=context), identity=identity, vms=boundary)
        assert [e["timestamp"] for e in everywhere["events"]] == ["2026-09-26T10:30:00"]  # not cust-2's 10:45
        with pytest.raises(PermissionError):
            execute(AacoCommand("event_search", camera_id="camera-name:warehouse", start=MIDNIGHT, end=NOW), identity=identity, vms=boundary)


# ------------------------------------------------------------ the pages' scripts parse


def test_workspace_and_widget_scripts_are_valid_javascript(tmp_path):
    """The /aaco workspace's inline script never closed its wrapping function
    (since 2026-09-19), so the page's command box did nothing; found while
    validating free-form input in a real browser."""
    import re
    import shutil
    import subprocess
    import aaco_web
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    for name, html in (("workspace", aaco_web._workspace()), ("widget", aaco_web.render_aaco_floating_widget())):
        scripts = re.findall(r"<script(?: [^>]*)?>(.*?)</script>", html, re.S)
        assert scripts, name
        for index, script in enumerate(scripts):
            path = tmp_path / f"{name}-{index}.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            assert result.returncode == 0, f"{name} script {index}: {result.stderr[-300:]}"
