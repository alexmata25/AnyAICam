"""Smart Alerts attention inbox (smart_alerts.py): grouping, categories,
pinned intrusion alarms, Voice Call behaviour and per-card actions."""
from datetime import timezone

import smart_alerts as sa


def n(id, event_type="person", camera_id="cam-1", ts="2026-09-28T12:00:00", **extra):
    return dict({"id": id, "event_type": event_type, "camera_id": camera_id, "camera_name": "Front Door",
                 "timestamp": ts, "event_id": f"evt-{id}", "has_event_clip": True}, **extra)


def test_repeated_alerts_from_one_camera_collapse_into_one_group():
    groups = sa.group_alerts([n("a", ts="2026-09-28T12:00:00"), n("b", ts="2026-09-28T12:10:00"), n("c", ts="2026-09-28T12:35:00")])
    assert len(groups) == 1 and groups[0]["count"] == 3 and groups[0]["latest"]["id"] == "c"


def test_a_long_quiet_gap_starts_a_new_group():
    groups = sa.group_alerts([n("a", ts="2026-09-28T09:00:00"), n("b", ts="2026-09-28T12:00:00")])
    assert [g["count"] for g in groups] == [1, 1]


def test_different_cameras_or_event_types_are_never_merged():
    groups = sa.group_alerts([n("a"), n("b", camera_id="cam-2"), n("c", event_type="car")])
    assert len(groups) == 3


def test_intrusion_alarms_and_voice_calls_are_never_grouped_and_active_alarms_are_pinned_first():
    items = [n("p1", ts="2026-09-28T12:05:00"), n("p2", ts="2026-09-28T12:06:00"),
             n("alarm1", "intrusion_alarm", ts="2026-09-28T11:00:00"), n("alarm2", "intrusion_alarm", ts="2026-09-28T11:01:00"),
             n("call1", "aac_voice_call", ts="2026-09-28T12:07:00"), n("call2", "aac_voice_call", ts="2026-09-28T12:08:00")]
    groups = sa.group_alerts(items)
    assert [g["latest"]["id"] for g in groups[:2]] == ["alarm2", "alarm1"]
    assert all(g["pinned"] for g in groups[:2])
    assert sorted(g["count"] for g in groups) == [1, 1, 1, 1, 2]


def test_an_acknowledged_alarm_is_no_longer_pinned():
    (group,) = sa.group_alerts([n("alarm", "intrusion_alarm", acknowledged_at="2026-09-28T12:01:00")])
    assert group["pinned"] is False


def test_categories():
    assert sa.category_for("person", "c") == "people" and sa.category_for("ppe", "c") == "people"
    assert sa.category_for("car", "c") == "vehicles" and sa.category_for("lpr", "c") == "vehicles"
    assert sa.category_for("intrusion_alarm", "c") == "security" and sa.category_for("aac_voice_call", "c") == "security"
    assert sa.category_for("storage_problem", None) == "system" and sa.category_for("anything", None) == "system"
    assert sa.category_for("motion", "c") == "other"


def _render(items, view="active", talk=("cam-1",)):
    groups = sa.group_alerts(items)
    content, _ = sa.render_inbox(groups, view=view, tz=timezone.utc, alert_text=lambda x: (x["event_type"].title(), ""),
                                 clip_href=lambda x: f'/playback?camera={x["camera_id"]}&event={x["event_id"]}',
                                 talk_camera_ids=set(talk), counts={"all": len(items)})
    return content


def test_cards_offer_contextual_actions():
    html = _render([n("a"), n("b", ts="2026-09-28T12:01:00")])
    assert "View clip" in html and ">Live<" in html and ">Talk<" in html
    assert 'data-sa-action="acknowledge" data-ids="b,a"' in html and 'data-sa-action="dismiss"' in html and 'data-sa-action="save"' in html
    assert "×2" in html
    assert ">Talk<" not in _render([n("a")], talk=())


def test_voice_call_keeps_answer_and_view_camera():
    html = _render([n("call", "aac_voice_call", event_id="vc-1")])
    assert 'href="/aac/voice-call/vc-1">Answer<' in html and ">View camera<" in html and "View clip" not in html


def test_intrusion_card_has_live_talk_clip_911_and_acknowledge_but_no_dismiss():
    html = _render([n("alarm", "intrusion_alarm", event_id="det-9")])
    assert 'class="sa-card sa-alarm" role="alert"' in html and "INTRUSION ALARM" in html
    assert 'href="/customer/cameras/cam-1/live?alarm=det-9">Live<' in html and ">Talk<" in html and "View clip" in html
    assert 'href="tel:911"' in html and 'data-sa-action="acknowledge"' in html and 'data-sa-action="dismiss"' not in html


def test_handled_view_has_no_acknowledge_buttons():
    assert 'data-sa-action="acknowledge"' not in _render([n("a", acknowledged_at="x")], view="handled")
