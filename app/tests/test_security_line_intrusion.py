"""Security-grade line crossing -> INTRUSION ALARM (security_rules).

Line: vertical at x=0.5 from (0.5,0) to (0.5,1) on a 1000x1000 frame.
With the engine's orientation, direction "inbound" protects the LEFT side
(x < 0.5); "outbound" protects the right side. A person is a 60x200 box
whose feet are at y=800."""
import pytest

import security_rules as sr

W = H = 1000
RULE = {"id": "r1", "rule_type": "security_line", "name": "Back fence", "direction": "inbound",
        "geometry": [{"x": 0.5, "y": 0.0}, {"x": 0.5, "y": 1.0}]}


@pytest.fixture(autouse=True)
def _reset():
    sr.reset_state()
    yield
    sr.reset_state()


def person(x_left, track="t1", cls="person", conf=0.9, height=200):
    return {"track_id": track, "class_name": cls, "confidence": conf, "x": x_left, "y": 800 - height, "width": 60, "height": height}


def run(xs, rule=RULE, **kw):
    """Feed one detection per reading; return the readings (1-based) that alarmed."""
    fired = []
    for i, x in enumerate(xs, 1):
        dets = [person(x, **kw)] if x is not None else []
        if sr.evaluate(1, rule, dets, W, H, now=float(i * 2)):
            fired.append(i)
    return fired


def test_a_confirmed_crossing_into_the_protected_side_alarms_once():
    # right side (outside) ... crosses ... fully left (inside) for two readings
    assert run([700, 650, 520, 400, 380, 360]) == [5]


def test_standing_near_or_on_the_line_never_alarms():
    assert run([560, 530, 505, 480, 505, 530, 560]) == []  # footprint straddles the line at most


def test_a_partial_crossing_that_returns_is_not_an_intrusion():
    assert run([700, 600, 460, 700, 700]) == []  # one reading inside, then back out: not confirmed


def test_walking_out_of_the_protected_side_never_alarms():
    assert run([300, 350, 420, 600, 700]) == []


def test_someone_first_seen_already_inside_does_not_alarm():
    assert run([300, 300, 300, 300]) == []


def test_direction_is_respected():
    outbound = dict(RULE, direction="outbound")  # protects the right side
    assert run([700, 650, 520, 400, 380], rule=outbound) == []
    sr.reset_state()
    assert run([300, 350, 520, 700, 720], rule=outbound) == [4]  # 520..580 is already fully across


@pytest.mark.parametrize("cls", ["car", "truck", "dog", "cat", "bird"])
def test_animals_and_vehicles_never_raise_an_intrusion_alarm(cls):
    assert run([700, 650, 520, 400, 380, 360], cls=cls) == []


def test_low_confidence_and_tiny_detections_are_ignored():
    assert run([700, 650, 520, 400, 380, 360], conf=0.3) == []
    sr.reset_state()
    assert run([700, 650, 520, 400, 380, 360], height=30) == []


def test_pacing_back_and_forth_is_rate_limited_by_the_cooldown():
    rule = dict(RULE, alarm_cooldown_seconds=60)
    # in (alarm at reading 5), out, in again quickly: the second entry is within the cooldown
    assert run([700, 650, 520, 400, 380, 700, 700, 400, 380, 360], rule=rule) == [5]


def test_a_later_separate_entry_alarms_again_after_the_cooldown():
    rule = dict(RULE, alarm_cooldown_seconds=5)
    assert run([700, 650, 520, 400, 380, 700, 700, 400, 380, 360], rule=rule) == [5, 9]


def test_majority_mode_uses_the_feet_centre_point():
    rule = dict(RULE, crossing_mode="majority")
    # footprint straddling but centre clearly inside (x 450..510 -> centre 480, margin 0.02 -> needs < 480)
    assert run([700, 650, 440, 440, 440], rule=rule) == [4]


def test_invalid_rules_are_ignored():
    assert sr.evaluate(1, dict(RULE, geometry=[{"x": 0.5, "y": 0}]), [person(400)], W, H, 1.0) == []
    assert sr.evaluate(1, dict(RULE, direction="both"), [person(400)], W, H, 1.0) == []
    assert sr.evaluate(1, dict(RULE, rule_type="line_crossing"), [person(400)], W, H, 1.0) == []


def test_two_people_are_tracked_independently():
    fired = []
    for i, (xa, xb) in enumerate([(700, 300), (650, 300), (520, 300), (400, 300), (380, 300)], 1):
        alarms = sr.evaluate(1, dict(RULE, alarm_cooldown_seconds=0), [person(xa, track="A"), person(xb, track="B")], W, H, float(i * 2))
        fired += [(i, a["track_id"]) for a in alarms]
    assert fired == [(5, "A")]  # B was inside all along and never alarms
