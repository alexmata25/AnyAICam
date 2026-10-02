"""Customer event presentation (2026-09-25): stored event types get
customer names ('PPE', 'Entry', 'License plate' -- not 'Ppe' or 'People
Counting In'), confidence is shown only when it is one (motion rows store
a raw motion score, PPE a placeholder 0.0), and Investigate shows
viewer-local times, escapes stored values and no longer offers colour /
plate inputs it never sent to the search."""
import inspect

import main
from customer_analytics_panel import event_type_label


def test_event_type_labels():
    assert event_type_label("ppe") == "PPE"
    assert event_type_label("people_counting_in") == "Entry"
    assert event_type_label("people_counting_out") == "Exit"
    assert event_type_label("plate") == "License plate"
    assert event_type_label("smart_motion") == "Smart Motion"  # the product name customers buy
    assert event_type_label("aac_voice_call") == "Voice call"
    assert event_type_label("person") == "Person"
    assert event_type_label("some_new_type") == "Some New Type"
    assert event_type_label(None) == "Event"


def test_events_table_uses_labels_and_real_confidence():
    source = inspect.getsource(main._render_customer_events)
    assert 'type_label = _customer_event_type_label(event.get("event_type"))' in source
    assert "_event_confidence_percent(_customer_real_confidence(" in source
    assert main._event_confidence_percent(main._customer_real_confidence("motion", 17.1)) == "—"
    assert main._event_confidence_percent(main._customer_real_confidence("ppe", 0.0)) == "—"
    assert main._event_confidence_percent(main._customer_real_confidence("person", 0.539)) == "53.9%"


def test_investigate_client_shape_is_customer_ready(monkeypatch):
    monkeypatch.setattr(main, "load_json_file", lambda path, default: {})
    base = {"id": "e1", "camera_id": "cam-1", "camera_name": "Front", "timestamp": "2026-09-25T18:27:27",
            "event_type": "ppe", "confidence": 0.0, "has_event_clip": False}
    shaped = main._customer_investigate_event_for_client(base)
    assert shaped["type_label"] == "PPE" and shaped["confidence"] is None
    assert shaped["timestamp_ms"] == 1790360847000  # naive UTC -> epoch ms; the browser localizes it
    motion = main._customer_investigate_event_for_client({**base, "event_type": "motion", "confidence": 25.4})
    assert motion["confidence"] is None and motion["type_label"] == "Motion"
    person = main._customer_investigate_event_for_client({**base, "event_type": "person", "confidence": 0.54})
    assert person["confidence"] == 0.54


def test_investigate_page_escapes_localizes_and_drops_dead_filters():
    source = inspect.getsource(main._render_customer_investigate)
    assert "investigation-color" not in source and "investigation-plate" not in source
    assert "replace('T',' ')" not in source  # no raw UTC ISO strings
    assert "${{esc(label)}}" in source and "${{esc(event.camera)}}" in source
    assert "new Date(event.timestamp_ms)" in source
    # The legacy (non-customer) investigation page is untouched.
    assert "investigation-color" in inspect.getsource(main.investigation_page)


def test_alert_cards_replace_generated_raw_titles_but_keep_custom_text():
    assert main._customer_alert_text({"event_type": "ppe", "title": "Ppe", "message": "Ppe detected"}) == ("PPE", "PPE detected")
    assert main._customer_alert_text({"event_type": "people_counting_in", "title": "People Counting In",
                                      "message": "People Counting In detected"}) == ("Entry", "Person entered")
    assert main._customer_alert_text({"event_type": "aac_voice_call", "title": "Aac Voice Call",
                                      "message": "Someone is at Front Door."}) == ("Voice call", "Someone is at Front Door.")
    assert main._customer_alert_text({"event_type": "person", "title": "Intruder rule", "message": "Custom"}) == ("Intruder rule", "Custom")


def test_new_notifications_use_customer_names():
    import notification_engine
    source = inspect.getsource(notification_engine)
    assert "title=event_type_label(event_type)" in source and "event_type_message(event_type)" in source
