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


# ---------------------------------------------------------------- arming lifecycle (2026-10-01)

def _feed(xs, start=0, rule=RULE, track="t1"):
    fired = []
    for i, x in enumerate(xs, start + 1):
        if sr.evaluate(1, rule, [person(x, track=track)] if x is not None else [], W, H, now=float(i * 2)):
            fired.append(i)
    return fired


def test_a_crossing_made_while_disarmed_cannot_alarm_after_rearming():
    """Armed, the person is seen outside; the site is Disarmed and they walk
    in; it is re-armed while they are still inside. Without the reset the
    old 'was outside' history plus two inside readings raised an alarm."""
    sr.note_arm_state(1, "away")
    assert _feed([700, 650]) == []                # armed: seen outside
    sr.note_arm_state(1, None)                    # disarmed: not evaluated while they walk in
    sr.note_arm_state(1, "away")                  # re-armed with the person still inside
    assert _feed([400, 380, 360, 350], start=10) == []
    # only a NEW armed outside-to-inside crossing alarms
    assert _feed([700, 650, 520, 400, 380], start=20) == [25]


def test_switching_between_stay_and_away_also_starts_over():
    sr.note_arm_state(1, "stay")
    _feed([700, 650])
    assert sr.note_arm_state(1, "away") is True
    assert _feed([400, 380, 360], start=10) == []
    assert sr.note_arm_state(1, "away") is False  # unchanged mode keeps the state


def test_a_lost_and_reacquired_track_needs_a_fresh_crossing():
    """After track loss (or an appliance restart) the person comes back under
    a new track with no 'outside' history: standing inside never alarms."""
    sr.note_arm_state(1, "away")
    assert _feed([700, 650, 520]) == []           # was crossing as t1 ...
    assert _feed([400, 380, 360], start=10, track="t2") == []  # ... re-acquired inside as t2


def test_a_restart_starts_with_no_crossing_history():
    sr.note_arm_state(1, "away")
    _feed([700, 650])
    sr.reset_state()                              # process restart
    sr.note_arm_state(1, "away")
    assert _feed([400, 380, 360], start=10) == []


# ---------------------------------------------------------------- one alarm per physical intrusion

FENCE_A = dict(RULE, id="fence-a", name="Fence A", alarm_cooldown_seconds=0)
FENCE_B = dict(RULE, id="fence-b", name="Fence B", geometry=[{"x": 0.55, "y": 0.0}, {"x": 0.55, "y": 1.0}], alarm_cooldown_seconds=0)


def _walk_both(xs, track="t1", start=0):
    alarms = []
    for i, x in enumerate(xs, start + 1):
        for rule in (FENCE_A, FENCE_B):
            alarms += sr.evaluate(1, rule, [person(x, track=track)], W, H, now=float(i * 2))
    return alarms


def test_overlapping_security_lines_raise_one_alarm_for_one_intrusion():
    alarms = _walk_both([800, 750, 650, 520, 400, 380, 360])
    assert len(alarms) == 1
    # every line the person crossed is kept on the alarm, for audit
    assert set(alarms[0]["matched_rule_ids"]) == {"fence-a", "fence-b"}
    assert set(alarms[0]["matched_rule_names"]) == {"Fence A", "Fence B"}


def test_two_different_people_are_two_intrusions():
    a = _walk_both([800, 750, 650, 520, 400, 380, 360], track="p1")
    b = _walk_both([800, 750, 650, 520, 400, 380, 360], track="p2", start=20)
    assert len(a) == 1 and len(b) == 1


def test_leaving_and_coming_back_is_a_new_intrusion():
    first = _walk_both([800, 750, 650, 520, 400, 380, 360])
    out_again = _walk_both([800, 800], start=10)
    second = _walk_both([650, 520, 400, 380, 360], start=20)
    assert len(first) == 1 and out_again == [] and len(second) == 1


def test_the_dedup_window_is_bounded(monkeypatch):
    monkeypatch.setattr(sr, "ALARM_DEDUP_SECONDS", 5.0)
    alarms = []
    for i, x in enumerate([800, 750, 520, 400, 380], 1):
        alarms += sr.evaluate(1, FENCE_A, [person(x)], W, H, now=float(i * 2))
    # much later the same track confirms the second line: past the window, it alarms
    for i, x in enumerate([800, 750, 650, 400, 380], 50):
        alarms += sr.evaluate(1, dict(FENCE_B, geometry=[{"x": 0.7, "y": 0.0}, {"x": 0.7, "y": 1.0}]), [person(x)], W, H, now=float(i * 2))
    assert len(alarms) == 2
