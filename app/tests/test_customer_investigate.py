"""Investigate for cloud customer accounts: the /investigate route existed
but (1) was never in the customer portal's nav-key allowlist, so no link
to it ever rendered for a customer_owner/customer_viewer session, and
(2) even navigated to directly, used the legacy edge/VMS-admin auth and
local-JSON data path -- current_user()/has_permission() never resolve a
real cloud customer session (a different cookie/store entirely), so a
customer would have hit "permission denied" or seen only mock data.

This covers the fix: nav visibility, real tenant-scoped data reuse
(_customer_detection_events(), the same source the Events page already
uses -- not mock data), and a correct /playback?camera=<id>&t=<timestamp>
deep link per result so older results resolve through Playback's own
near= lookup even outside its initial bounded page.
"""

from types import SimpleNamespace

import main


def test_investigate_key_is_in_customer_portal_nav():
    keys = main.navigation_keys_for_role("customer_owner")
    assert "investigate" in keys


def test_investigate_key_is_in_customer_viewer_nav_too():
    keys = main.navigation_keys_for_role("customer_viewer")
    assert "investigate" in keys


def test_alerts_and_events_keys_are_unaffected():
    keys = main.navigation_keys_for_role("customer_owner")
    assert {"events", "alerts", "playback"}.issubset(keys)


def _fake_request():
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda k, d=None: d))


def test_customer_identity_gets_real_data_not_mock(monkeypatch):
    monkeypatch.setattr(
        main, "_customer_playback_cameras",
        lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}],
    )
    real_event = {
        "id": "evt-real-1", "camera": 1, "camera_id": "cam-1", "camera_name": "Front Door",
        "site": "Main", "rule_name": "Person detection", "event_type": "person",
        "timestamp": "2026-08-20T10:00:00", "confidence": 0.91, "thumbnail": None,
        "linked_recording": None, "plate_number": None, "vehicle_color": None, "mock": False,
    }
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [real_event])
    html = main.investigation_page(_fake_request())
    assert "evt-real-1" in html
    assert '"mock"' not in html  # never claims mock data for a real customer


def test_result_deep_links_to_playback_with_camera_and_timestamp(monkeypatch):
    monkeypatch.setattr(
        main, "_customer_playback_cameras",
        lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}],
    )
    real_event = {
        "id": "evt-real-2", "camera": 1, "camera_id": "cam-1", "camera_name": "Front Door",
        "site": "Main", "rule_name": "Vehicle detection", "event_type": "vehicle",
        "timestamp": "2026-08-01T09:30:00", "confidence": 0.8, "thumbnail": None,
        "linked_recording": None, "plate_number": None, "vehicle_color": "red", "mock": False,
    }
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [real_event])
    html = main.investigation_page(_fake_request())
    expected_epoch_ms = main._naive_utc_timestamp_to_epoch_ms("2026-08-01T09:30:00")
    assert f"/playback?camera=cam-1&t={expected_epoch_ms}&autoplay=event" in html


def test_older_result_still_carries_its_own_deep_link(monkeypatch):
    # "Older" here just means: nothing in this page bounds how far back a
    # result can come from -- the deep link is built from the event's own
    # timestamp regardless of age, and Playback's own near= lookup (not
    # this page) is what makes an old timestamp resolve to a recording
    # outside Playback's initial bounded load.
    monkeypatch.setattr(
        main, "_customer_playback_cameras",
        lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}],
    )
    old_event = {
        "id": "evt-old", "camera": 1, "camera_id": "cam-1", "camera_name": "Front Door",
        "site": "Main", "rule_name": "Motion detection", "event_type": "motion",
        "timestamp": "2025-01-15T04:00:00", "confidence": 0.5, "thumbnail": None,
        "linked_recording": None, "plate_number": None, "vehicle_color": None, "mock": False,
    }
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [old_event])
    html = main.investigation_page(_fake_request())
    expected_epoch_ms = main._naive_utc_timestamp_to_epoch_ms("2025-01-15T04:00:00")
    assert f"camera=cam-1&t={expected_epoch_ms}&autoplay=event" in html


def test_non_customer_identity_falls_through_to_legacy_path(monkeypatch):
    monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: None)
    calls = []
    monkeypatch.setattr(main, "current_user", lambda request: {"role": "viewer", "enabled": False, "id": "anonymous"})
    monkeypatch.setattr(main, "has_permission", lambda user, perm: False)
    html = main.investigation_page(_fake_request())
    assert "Access control" in html
    assert "view_analytics" in html
