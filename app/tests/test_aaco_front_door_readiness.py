"""Front Door questions the AACO physical test will use (2026-09-27).

Free-form readings added or corrected on top of aaco_freeform.py:
- a question about this moment ("who is at the door right now", "is
  someone at the front door") opens live view instead of a 24-hour search;
- "since noon" / "since this morning" is a range until now, not a moment;
- "yesterday evening" (morning, afternoon, night) is that part of
  yesterday, not the whole day;
- deliveries (delivery, courier, mailman, postman) are person visits, and
  "the doorbell" points at the door camera;
- "the driveway right now" no longer picks a camera named "Driveway Right".
Everything else AACO understood is unchanged (test_aaco_freeform.py), and a
door unlock or talk still never comes from a loose reading.
"""
from datetime import datetime

import pytest

from aaco import AacoCommand, Clarification
from aaco_freeform import FreeFormAacoLanguageAdapter
from test_aaco_freeform import CAMERAS, NOW, _client  # NOW = 2026-09-26 11:00

FRONT = "camera-name:front door"
TODAY = datetime(2026, 9, 26)
YESTERDAY = datetime(2026, 9, 25)


def _parse(text, cameras=CAMERAS):
    return FreeFormAacoLanguageAdapter().parse(text, now=NOW, context={"camera_names": cameras})


@pytest.mark.parametrize("text", [
    "who is at the door right now",
    "Who's at the front door right now?",
    "is someone at the front door",
    "is anyone at the door",
    "is there anybody at the front door at the moment",
    "hey aaco who is at the front door currently",
])
def test_a_question_about_this_moment_opens_front_door_live(text):
    assert _parse(text) == AacoCommand("live_view", camera_id=FRONT)


@pytest.mark.parametrize("text", [
    "who was at the front door yesterday",
    "was anyone at the front door right now",      # past tense: a history question
    "did someone come to the front door",
    "has anyone been at my front door in the last hour",
])
def test_history_questions_stay_event_searches(text):
    result = _parse(text)
    assert result.operation == "event_search" and result.camera_id == FRONT and result.event_type == "person"


@pytest.mark.parametrize("text,start,end", [
    ("any people at the front door since 9am", TODAY.replace(hour=9), NOW),
    ("anyone at the front door since noon", None, None),  # noon is after 11:00 -> yesterday's noon
    ("who came to the front door since this morning", TODAY.replace(hour=5), NOW),
])
def test_since_is_a_range_until_now(text, start, end):
    result = _parse(text)
    assert result.operation == "event_search" and result.camera_id == FRONT
    assert result.end == NOW
    if start is not None:
        assert result.start == start
    else:
        assert result.start == YESTERDAY.replace(hour=12)


@pytest.mark.parametrize("text,start,end", [
    ("front door events from yesterday evening", YESTERDAY.replace(hour=17), TODAY),
    ("yesterday morning people at the front door", YESTERDAY.replace(hour=5), YESTERDAY.replace(hour=12)),
    ("anyone at the front door yesterday afternoon", YESTERDAY.replace(hour=12), YESTERDAY.replace(hour=17)),
    ("who was at the front door yesterday", YESTERDAY, TODAY),  # whole day, unchanged
])
def test_parts_of_yesterday(text, start, end):
    result = _parse(text)
    assert (result.start, result.end) == (start, end)


@pytest.mark.parametrize("text,start", [
    ("was there a delivery at the front door today", TODAY),
    ("did the courier come to the front door this morning", TODAY.replace(hour=5)),
    ("did the mailman come by the front door today", TODAY),
    ("who rang the doorbell this morning", TODAY.replace(hour=5)),
])
def test_deliveries_and_the_doorbell_mean_a_person_at_the_front_door(text, start):
    result = _parse(text)
    assert result.operation == "event_search" and result.event_type == "person"
    assert result.camera_id == FRONT and result.start == start


def test_right_now_never_selects_a_camera_named_driveway_right():
    cameras = ["Front Door", "Driveway Left", "Driveway Right"]
    assert _parse("any cars in the driveway right now", cameras).camera_id == "camera-name:driveway"  # boundary asks which
    assert _parse("show me driveway right", cameras).camera_id == "camera-name:driveway right"
    assert _parse("show the driveway right camera right now", cameras) == AacoCommand("live_view", camera_id="camera-name:driveway right")


@pytest.mark.parametrize("text", [
    "can you let the courier in at the front door",
    "let the delivery person in",
    "buzz the visitor in at the front door",
    "open up for the mailman",
    "say hello to whoever is at the front door",
])
def test_visitor_and_delivery_wording_never_targets_a_real_door(text):
    # The exact grammar reads "open <name>" as a door unlock ("open up for the
    # mailman" -> a door named "up for the mailman"). That is safe: the VMS
    # boundary matches a door name EXACTLY (main._ClassicAacoBoundary.
    # _door_matches, never fuzzy), so such a phrase fails closed with an
    # audited 403 and no relay call. What must never happen is a loose
    # visitor/delivery sentence naming one of this customer's real doors.
    result = _parse(text)
    if isinstance(result, AacoCommand) and result.operation in ("unlock_door", "talk"):
        real = {"camera-name:" + name.lower() for name in CAMERAS}
        assert result.camera_id not in real, repr(result)


@pytest.mark.parametrize("typed,spoken", [
    ("Who is at the front door right now?", "who is at the front door right now"),
    ("Was there a delivery at the front door today?", "was there a delivery at the front door today"),
    ("Any people at the front door since 9 AM?", "any people at the front door since nine a.m."),
])
def test_typed_and_spoken_front_door_questions_reach_the_same_action(typed, spoken):
    client_a, vms_a = _client()
    client_b, vms_b = _client()
    assert client_a.post("/api/aaco/command", json={"command": typed}).json() == client_b.post("/api/aaco/command", json={"command": spoken}).json()
    assert vms_a.calls == vms_b.calls and vms_a.calls


def test_another_tenant_cannot_open_this_front_door_live():
    other, vms = _client("tenant-b")
    response = other.post("/api/aaco/command", json={"command": "who is at the front door right now"})
    assert response.status_code in (200, 403)
    assert not any(call[0] == "live" for call in vms.calls)
