"""Edge-side half of cloud -> appliance rule delivery (2026-09-21):
edge_camera_sync.py's own analytics-rules reconciliation, which mirrors
GET /api/appliance/configuration's new `analytics_rules` field into the
LOCAL customer_analytics_rules table -- see that module's own
docstring for why this is a full replace (unlike camera sync's
deliberate "never delete" policy) and for the cloud-side half
(test_appliance_analytics_rules_delivery.py).

Reuses test_edge_camera_sync.py's own established harness/fixtures
exactly (same IDENTITY, same _seeded_db autouse fixture, same
_mock_identity helper) -- only _mock_cloud_config is redefined locally
to also carry an analytics_rules list, since the shared helper's
signature is deliberately left unchanged for its own existing tests.
"""
import sqlite3

import pytest

import edge_camera_sync
from database_backend import override_target
from partner_db import connection, initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_edge_camera_sync_analytics_rules.db"


@pytest.fixture(autouse=True)
def _seeded_db(db_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY=")
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "CLOUD_URL", "https://portal.example")
    with override_target(sqlite_path=str(db_path)):
        yield


IDENTITY = {
    "appliance_id": "appl-ryzen", "cloud_id": "AIC-RYZEN0001",
    "credential": "ryzen-credential", "customer_id": "cust-1", "site_id": "site-1",
}

CAMERA = {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured"}

RULE = {
    "id": "rule-1", "customer_id": "cust-1", "site_id": "site-1", "camera_id": "cam-1",
    "rule_type": "line_crossing", "name": "Driveway line", "direction": "both",
    "geometry": [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.9}], "updated_at": "2026-09-21T00:00:00",
}


def _mock_identity(monkeypatch, identity=IDENTITY):
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: identity)


def _mock_cloud_config(monkeypatch, cameras, rules=None):
    response = {"cameras": cameras, "configuration_version": "x"}
    if rules is not None:
        response["analytics_rules"] = rules
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda path, appliance_id, credential: response)


def _local_rules(db_path):
    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        return {r["id"]: dict(r) for r in con.execute("SELECT * FROM customer_analytics_rules")}


def test_a_rule_reported_by_the_cloud_is_upserted_into_the_local_table(db_path, monkeypatch):
    _mock_identity(monkeypatch)
    _mock_cloud_config(monkeypatch, [CAMERA], [RULE])

    result = edge_camera_sync.sync_provisioned_cameras()

    assert result["rules_synced"] == 1
    local = _local_rules(db_path)
    assert set(local) == {"rule-1"}
    assert local["rule-1"]["camera_id"] == "cam-1"
    assert local["rule-1"]["rule_type"] == "line_crossing"
    assert local["rule-1"]["direction"] == "both"
    assert local["rule-1"]["enabled"] == 1
    import json
    assert json.loads(local["rule-1"]["geometry_json"]) == RULE["geometry"]


def test_updating_a_rules_geometry_in_the_cloud_updates_the_local_copy_not_a_duplicate(db_path, monkeypatch):
    _mock_identity(monkeypatch)
    _mock_cloud_config(monkeypatch, [CAMERA], [RULE])
    edge_camera_sync.sync_provisioned_cameras()

    moved_rule = dict(RULE, geometry=[{"x": 0.2, "y": 0.2}, {"x": 0.8, "y": 0.8}], name="Renamed")
    _mock_cloud_config(monkeypatch, [CAMERA], [moved_rule])
    edge_camera_sync.sync_provisioned_cameras()

    local = _local_rules(db_path)
    assert len(local) == 1
    assert local["rule-1"]["name"] == "Renamed"
    import json
    assert json.loads(local["rule-1"]["geometry_json"]) == moved_rule["geometry"]


def test_a_rule_no_longer_reported_by_the_cloud_is_deleted_locally(db_path, monkeypatch):
    """The core behavioral difference from camera sync's own "never
    delete" policy -- see edge_camera_sync.py's own docstring for why a
    disabled/deleted rule must actually stop being enforced, not linger
    forever locally."""
    _mock_identity(monkeypatch)
    _mock_cloud_config(monkeypatch, [CAMERA], [RULE])
    edge_camera_sync.sync_provisioned_cameras()
    assert set(_local_rules(db_path)) == {"rule-1"}

    _mock_cloud_config(monkeypatch, [CAMERA], [])  # customer deleted (or disabled) the rule
    result = edge_camera_sync.sync_provisioned_cameras()

    assert result["rules_synced"] == 0
    assert _local_rules(db_path) == {}


def test_a_missing_analytics_rules_field_leaves_local_rules_untouched(db_path, monkeypatch):
    """Fail-safe posture, matching the camera list's own malformed_
    response bail-out: a transient/older-deployment response that
    simply omits this field must never be read as "delete everything"."""
    _mock_identity(monkeypatch)
    _mock_cloud_config(monkeypatch, [CAMERA], [RULE])
    edge_camera_sync.sync_provisioned_cameras()
    assert set(_local_rules(db_path)) == {"rule-1"}

    _mock_cloud_config(monkeypatch, [CAMERA], rules=None)  # field omitted entirely this cycle
    result = edge_camera_sync.sync_provisioned_cameras()

    assert result["rules_synced"] is None
    assert set(_local_rules(db_path)) == {"rule-1"}


def test_two_rules_on_two_cameras_for_the_same_appliance_are_both_synced(db_path, monkeypatch):
    camera_2 = {"id": "cam-2", "name": "Camera 2", "camera_number": 2, "status": "configured"}
    rule_2 = dict(RULE, id="rule-2", camera_id="cam-2", rule_type="intrusion", direction=None,
                  geometry=[{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.9, "y": 0.9}])
    _mock_identity(monkeypatch)
    _mock_cloud_config(monkeypatch, [CAMERA, camera_2], [RULE, rule_2])

    result = edge_camera_sync.sync_provisioned_cameras()

    assert result["rules_synced"] == 2
    local = _local_rules(db_path)
    assert set(local) == {"rule-1", "rule-2"}
    assert local["rule-2"]["rule_type"] == "intrusion"
    assert local["rule-2"]["camera_id"] == "cam-2"


def test_rules_belong_only_to_this_appliances_own_identity_never_a_second_ones(db_path, monkeypatch):
    """This appliance's local reconciliation is scoped to its OWN
    appliance_id -- a rule for some other appliance's camera_id could
    never legitimately arrive here (appliance_configuration() already
    scopes it server-side, see test_appliance_analytics_rules_
    delivery.py), but this proves the local write itself is stamped
    with THIS appliance's identity, not trusted from the payload."""
    _mock_identity(monkeypatch)
    _mock_cloud_config(monkeypatch, [CAMERA], [RULE])
    edge_camera_sync.sync_provisioned_cameras()

    local = _local_rules(db_path)
    assert local["rule-1"]["appliance_id"] == IDENTITY["appliance_id"]


def test_not_activated_appliance_never_touches_local_rules(db_path, monkeypatch):
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: None)
    result = edge_camera_sync.sync_provisioned_cameras()
    assert result == {"status": "not_activated"}
    assert _local_rules(db_path) == {}
