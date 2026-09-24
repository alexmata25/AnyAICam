"""Shared appliance configuration cache (2026-09-24).

edge_camera_sync publishes each well-formed GET /api/appliance/
configuration response; the other edge workers reuse it instead of each
fetching (and authenticating) independently -- measured on staging at
~15,600 configuration requests/day from one appliance, most of them from
event_media_uploader refreshing on every uploaded event and the storage
manager fetching its policy on every scan. No network, cloud, or device
is involved: every fetch below is a counting stub.
"""
import pytest

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_appliance_config_cache_import.db"):
    import analytics_sync
    import appliance_config_cache as cache
    import edge_camera_sync
    import local_storage_manager
    import recording_uploader
    import talk_down_discovery
    import webrtc_publisher
    from partner_db import initialize_database

CAMERA = {"id": "cam-1", "name": "Front Door", "camera_number": 1, "status": "configured", "site_id": "site-1", "recording_mode": "event"}
CONFIG = {
    "cameras": [CAMERA],
    "storage_policy": {"reserved_free_percent": 12, "warning_free_percent": 20, "local_retention_days": 14},
    "configuration_version": "configured",
}
READERS = [
    (analytics_sync, "_refresh_camera_map"),
    (recording_uploader, "_refresh_camera_map"),
    (local_storage_manager, "_refresh_camera_map"),
    (local_storage_manager, "_local_storage_policy"),
    (talk_down_discovery, "_refresh_camera_map"),
    (webrtc_publisher, "_refresh_camera_map"),
]


def _counting_fetch(monkeypatch, module, response):
    calls = []
    monkeypatch.setattr(module, "_control_plane_get", lambda *args, **kwargs: calls.append(args) or response)
    return calls


# ------------------------------------------------------------- the cache itself


def test_nothing_is_served_before_anything_is_published():
    assert cache.fresh() is None


def test_a_published_configuration_is_served_as_a_private_copy():
    cache.publish(CONFIG)
    first = cache.fresh()
    first["cameras"].append({"id": "mutated"})
    assert cache.fresh() == CONFIG


def test_a_stale_configuration_is_not_served(monkeypatch):
    cache.publish(CONFIG, now=1000.0)
    assert cache.fresh(now=1000.0 + cache.MAX_AGE_SECONDS - 1) == CONFIG
    assert cache.fresh(now=1000.0 + cache.MAX_AGE_SECONDS + 1) is None


def test_non_dict_responses_are_never_published():
    cache.publish(None)
    cache.publish(["not", "a", "config"])
    assert cache.fresh() is None


def test_get_or_fetch_falls_back_to_the_callers_own_fetch_without_publishing_it():
    assert cache.get_or_fetch(lambda: {"from": "fetch"}) == {"from": "fetch"}
    assert cache.fresh() is None  # a reader's own fetch never becomes the shared copy


# ------------------------------------------------------------- edge sync publishes


@pytest.fixture()
def edge(tmp_path, monkeypatch):
    path = tmp_path / "edge.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY=")
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "CLOUD_URL", "https://portal.example")
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: {
        "appliance_id": "appl-1", "cloud_id": "AIC-1", "credential": "c", "customer_id": "cust-1", "site_id": "site-1",
    })
    return path


def test_edge_sync_publishes_a_well_formed_configuration(edge, monkeypatch):
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda path, appliance_id, credential: CONFIG)
    with override_target(sqlite_path=str(edge)):
        assert edge_camera_sync.sync_provisioned_cameras()["status"] == "ok"
    assert cache.fresh()["cameras"][0]["id"] == "cam-1"


@pytest.mark.parametrize("response", [None, {"cameras": "not-a-list"}])
def test_edge_sync_never_publishes_an_unreachable_or_malformed_response(edge, monkeypatch, response):
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda path, appliance_id, credential: response)
    with override_target(sqlite_path=str(edge)):
        edge_camera_sync.sync_provisioned_cameras()
    assert cache.fresh() is None


def test_edge_sync_always_fetches_fresh_itself(edge, monkeypatch):
    cache.publish({"cameras": []})
    calls = []
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda path, appliance_id, credential: calls.append(path) or CONFIG)
    with override_target(sqlite_path=str(edge)):
        edge_camera_sync.sync_provisioned_cameras()
    assert calls == ["/api/appliance/configuration"]


# ------------------------------------------------------------- readers reuse it


@pytest.mark.parametrize(("module", "function"), READERS)
def test_a_reader_uses_the_published_configuration_instead_of_fetching(monkeypatch, module, function):
    calls = _counting_fetch(monkeypatch, module, {"cameras": []})
    cache.publish(CONFIG)
    getattr(module, function)()
    assert calls == []


@pytest.mark.parametrize(("module", "function"), READERS)
def test_a_reader_fetches_for_itself_when_nothing_recent_was_published(monkeypatch, module, function):
    calls = _counting_fetch(monkeypatch, module, CONFIG)
    getattr(module, function)()
    assert len(calls) == 1


def test_readers_build_the_same_state_from_the_cache_as_from_a_fetch(monkeypatch):
    _counting_fetch(monkeypatch, recording_uploader, {"cameras": []})
    monkeypatch.setattr(recording_uploader, "_camera_map", {})
    cache.publish(CONFIG)
    recording_uploader._refresh_camera_map()
    identity = recording_uploader._camera_identity(1)
    assert identity["camera_id"] == "cam-1" and identity["cloud_recording_mode"] == "event"

    _counting_fetch(monkeypatch, local_storage_manager, {"cameras": []})
    assert local_storage_manager._local_storage_policy()["reserved_free_percent"] == 12


def test_per_event_media_refreshes_no_longer_fetch_while_edge_sync_is_healthy(monkeypatch):
    """event_media_uploader calls recording_uploader._refresh_camera_map()
    for every event it uploads -- the single largest source of repeated
    configuration requests before this change."""
    calls = _counting_fetch(monkeypatch, recording_uploader, CONFIG)
    cache.publish(CONFIG)
    for _event in range(100):
        recording_uploader._refresh_camera_map()
    assert calls == []


def test_offline_a_reader_keeps_its_previous_state_when_its_own_fetch_fails(monkeypatch):
    """Unchanged offline behavior: nothing recent published (edge sync
    can't reach the cloud either) and the reader's own fetch fails -> the
    reader's existing camera map is left exactly as it was."""
    monkeypatch.setattr(webrtc_publisher, "_camera_map", {"cam-1": 1})
    _counting_fetch(monkeypatch, webrtc_publisher, None)
    cache.publish(CONFIG, now=0.0)  # long stale
    webrtc_publisher._refresh_camera_map()
    assert webrtc_publisher._camera_map == {"cam-1": 1}


def test_ten_minutes_of_every_worker_cadence_costs_only_edge_syncs_own_fetches(edge, monkeypatch):
    """Rough request budget: edge sync every 60s publishes; every reader
    (including 4 event-media refreshes a minute) runs at its real cadence
    in between. Only edge sync's own fetches reach the cloud."""
    edge_calls = []
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda path, appliance_id, credential: edge_calls.append(path) or CONFIG)
    reader_calls = {id(module): _counting_fetch(monkeypatch, module, CONFIG) for module, _ in READERS}
    with override_target(sqlite_path=str(edge)):
        for minute in range(10):
            edge_camera_sync.sync_provisioned_cameras()
            webrtc_publisher._refresh_camera_map()
            local_storage_manager._local_storage_policy()
            for _event in range(4):
                recording_uploader._refresh_camera_map()
            if minute % 5 == 0:
                analytics_sync._refresh_camera_map()
                local_storage_manager._refresh_camera_map()
                talk_down_discovery._refresh_camera_map()
    assert len(edge_calls) == 10
    assert sum(len(calls) for calls in reader_calls.values()) == 0
