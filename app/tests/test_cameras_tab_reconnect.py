"""Cameras tab: Reconnect button and dialog (2026-10-15), as served.
Owners on the portal get a Reconnect button per camera (revealed with Remove
once the cloud confirms can_manage) and one dialog; viewers and the
appliance's own pages get neither."""
import pytest

from test_camera_multi_appliance_isolation import (  # noqa: F401  (pytest fixtures)
    _activate, _owner_cookie, _seed_world, client, db_path,
)

OWNER = {"anyaicam_partner_session": _owner_cookie()}


@pytest.fixture()
def world(db_path):
    _seed_world(db_path)
    _activate(db_path, "appl-ryzen")
    return db_path


def _page(client, cookies=OWNER, **params):
    response = client.get("/customer-live", params=params, cookies=cookies)
    assert response.status_code == 200, response.text
    return response.text


def test_owners_get_a_reconnect_button_per_camera_and_the_dialog(client, world, monkeypatch):
    monkeypatch.delenv("ANYAICAM_RUNTIME_ROLE", raising=False)
    page = _page(client, appliance_id="appl-ryzen")
    assert ('<button class="camera-tool reconnect-camera" hidden data-camera-id="cam-ryzen-1" '
            'data-camera-name="Ryzen Camera 1"') in page
    assert 'aria-label="Reconnect Ryzen Camera 1"' in page
    assert page.index("reconnect-camera") < page.index('class="camera-tool remove-camera"')
    for element in ('id="reconnect-dialog"', 'id="reconnect-username"', 'id="reconnect-password" name="password" type="password"',
                    'id="reconnect-use-saved"', 'id="reconnect-submit"', 'Verify and reconnect'):
        assert element in page, element


def test_the_appliance_page_has_no_reconnect(client, world, monkeypatch):
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    page = _page(client, appliance_id="appl-ryzen")
    assert "reconnect-camera" not in page and 'id="reconnect-dialog"' not in page
