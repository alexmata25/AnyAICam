"""Line crossing / intrusion events carry which rule fired and the crossing
direction to the cloud (2026-09-25), so the customer Analytics workspaces
can show them -- same loose-key forwarding as PPE and facial recognition."""
import analytics_sync


def test_rule_events_forward_rule_name_and_direction():
    for kind in ("line_crossing", "intrusion"):
        payload = analytics_sync._build_payload({"id": "e1", "event_type": kind, "timestamp": "2026-09-25T10:00:00",
                                                 "rule_name": "Line Crossing (Front gate)", "rule_id": "r1", "direction": "in"})
        assert payload["detections"] == [{"rule_name": "Line Crossing (Front gate)", "rule_id": "r1", "direction": "in"}]


def test_explicit_detections_and_other_types_are_untouched():
    explicit = [{"label": "person"}]
    assert analytics_sync._build_payload({"id": "e2", "event_type": "line_crossing", "detections": explicit})["detections"] == explicit
    assert analytics_sync._build_payload({"id": "e3", "event_type": "person", "rule_name": "x"})["detections"] is None
