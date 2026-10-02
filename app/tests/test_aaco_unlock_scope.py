"""AACO door unlock honours AACO's camera scope for every token form
(2026-10-02, Codex launch blocker).

RestrictedBoundary.unlock_door() checked AACO's "only these cameras" scope
for camera-name: tokens only; a numeric camera-N token went straight to the
VMS door resolver, so an owner who may unlock both doors could unlock a
door camera the account had excluded from AACO. The scope is now enforced
on the door's resolved camera id, before authorization or the relay.
"""
from datetime import datetime

import pytest

import aaco
import aaco_settings
import main
from database_backend import override_target
from partner_db import connection
from test_aaco_unlock_door import _audit_rows, _isolated_relay, _owner_identity, _request, _seed, db_path  # noqa: F401


@pytest.fixture()
def scoped(db_path):
    """customer-a: Front Door (camera 1, AACO allowed) and Back Gate
    (camera 3, a configured door the owner may unlock, excluded from AACO)."""
    _seed(db_path)
    with override_target(sqlite_path=db_path), connection() as db:
        db.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,door_access_enabled,door_relay_channel,"
                   "door_relay_pulse_ms,created_at) VALUES('camera-back-gate','customer-a','site-a','Back Gate','configured',3,1,2,2500,?)",
                   (datetime.now().isoformat(),))
    settings = dict(aaco_settings.DEFAULTS, camera_scope="selected", camera_ids=["camera-front-door"], allow_door_actions=True)
    return settings


def _unlock(db_path, settings, token):
    with override_target(sqlite_path=db_path):
        vms = aaco_settings.restrict(main._ClassicAacoBoundary(_request()), settings, "customer-a")
        try:
            return aaco.execute(aaco.AacoCommand("unlock_door", camera_id=token), identity=_owner_identity(), vms=vms)
        except (PermissionError, ValueError) as error:  # refused before or at the boundary
            return error


EXCLUDED_TOKENS = [
    "camera-3", "camera-03", "camera-+3", "camera- 3", "camera-3 ", "camera-0003",
    "camera-name:back gate", "camera-name:BACK   GATE", "camera-name: back gate ",
    "camera-back-gate", "camera-id:camera-back-gate", "camera-name:camera-back-gate",
    "camera-3.0", "camera-", "camera-name:", "", "camera-3;camera-1", "camera-name:back gate\x00",
]


@pytest.mark.parametrize("token", EXCLUDED_TOKENS)
def test_no_token_form_unlocks_a_camera_excluded_from_aaco(db_path, scoped, _isolated_relay, token):
    result = _unlock(db_path, scoped, token)
    assert not (isinstance(result, dict) and result.get("kind") == "door_unlock"), (token, result)
    assert _isolated_relay.calls == [], token  # never reached the relay
    assert not [r for r in _audit_rows(db_path, "camera-back-gate") if r["success"]]


def test_a_numeric_token_for_the_excluded_door_is_refused_and_audited(db_path, scoped, _isolated_relay):
    result = _unlock(db_path, scoped, "camera-3")
    assert isinstance(result, aaco.Clarification) and result.message == aaco_settings.CAMERA_NOT_ALLOWED_MESSAGE
    audit = _audit_rows(db_path, "camera-back-gate")
    assert audit and audit[-1]["authorization_result"] == "denied" and audit[-1]["error"] == "aaco_camera_scope"
    assert audit[-1]["relay_result"] == "skipped" and audit[-1]["success"] == 0


@pytest.mark.parametrize("token", ["camera-1", "camera-01", "camera-name:front door"])
def test_the_permitted_door_still_unlocks_by_number_or_name(db_path, scoped, _isolated_relay, token):
    result = _unlock(db_path, scoped, token)
    assert isinstance(result, dict) and result["kind"] == "door_unlock" and result["message"] == "Front Door unlocked."
    assert len(_isolated_relay.calls) == 1 and _isolated_relay.calls[0].channel == 1


def test_all_cameras_scope_is_unchanged(db_path, scoped, _isolated_relay):
    everything = dict(scoped, camera_scope="all", camera_ids=[])
    result = _unlock(db_path, everything, "camera-3")
    assert isinstance(result, dict) and result["message"] == "Back Gate unlocked."
