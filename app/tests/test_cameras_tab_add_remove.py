"""Cameras tab: add and remove cameras (2026-10-08, owner-approved).

/customer-live (the customer "Cameras" tab) gives owners a capacity bar --
"N of M cameras in use", Add camera (the existing scan/provision step of
setup), and, when nothing more can be added, the cloud's exact message with
a link to My subscription -- plus a Remove button per camera that asks for
confirmation and calls DELETE /api/customer/cameras/{id}. Everything comes
from GET /api/customer/cameras' camera_capacity (Green's cloud contract,
feature/camera-capacity-add-remove-20261008); a cloud without it shows no
new controls. Viewers see none of it. Removed cameras (status 'removed', no
camera_number) are not shown. There is no JS runner in this repository:
the page and script are checked as served, and the link guard regex is
exercised in Python (same syntax)."""
import re
import sqlite3

import pytest

import live_view_page
from database_backend import override_target
from test_camera_multi_appliance_isolation import (  # noqa: F401  (pytest fixtures)
    _activate, _owner_cookie, _provision_old_cameras, _seed_world, client, db_path,
)

OWNER = {"anyaicam_partner_session": _owner_cookie()}


def _page(client, **params):
    response = client.get("/customer-live", params=params, cookies=OWNER)
    assert response.status_code == 200, response.text
    return response.text


@pytest.fixture()
def world(db_path):
    _seed_world(db_path)
    _activate(db_path, "appl-ryzen")
    return db_path


def test_owners_get_the_capacity_bar_add_and_remove_controls(client, world):
    page = _page(client, appliance_id="appl-ryzen")
    assert '<section class="camera-manage panel" id="camera-manage" hidden' in page   # until the cloud answers
    assert 'id="add-camera-button" href="/customer/setup?step=4&amp;appliance=appl-ryzen" hidden>Add camera</a>' in page
    assert 'id="camera-capacity-link" href="/subscription-portal" hidden>My subscription</a>' in page
    assert ('<button class="camera-tool remove-camera" hidden data-camera-id="cam-ryzen-1" '
            'data-camera-name="Ryzen Camera 1"') in page
    assert 'aria-label="Remove Ryzen Camera 1"' in page
    assert "fetch('/api/customer/cameras'" in page
    assert "{method:'DELETE'}" in page


def test_add_goes_to_the_one_appliance_or_lets_setup_choose(client, world):
    _activate(world, "appl-old")
    _provision_old_cameras(world)
    both = _page(client)
    assert 'id="add-camera-button" href="/customer/setup?step=4" hidden>' in both
    single = _page(client, appliance_id="appl-old")
    assert 'href="/customer/setup?step=4&amp;appliance=appl-old"' in single


def test_viewers_get_no_management_controls(client, world, monkeypatch):
    import partner_portal
    rows = []
    with override_target(sqlite_path=str(world)):
        from partner_db import connection
        with connection() as db:
            rows = live_view_page._customer_live_cameras(db, {"role": "customer_owner", "customer_id": "cust-1"}, "appl-ryzen")
    monkeypatch.setattr(live_view_page, "_customer_live_cameras", lambda db, identity, appliance_id='': rows)
    viewer = {"anyaicam_partner_session": partner_portal._token("viewer@example.test", "customer_viewer", None, "cust-1", None)}
    response = client.get("/customer-live", cookies=viewer)
    assert response.status_code == 200, response.text
    for marker in ('id="camera-manage"', 'class="camera-tool remove-camera"', 'id="add-camera-button"', "{method:'DELETE'}"):
        assert marker not in response.text


def test_a_removed_camera_is_not_shown(client, world):
    with override_target(sqlite_path=str(world)):
        conn = sqlite3.connect(world)
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,resolution,created_at,device_key,camera_number) "
                     "VALUES('cam-ryzen-2','cust-1','site-1','appl-ryzen','Garage','removed','4mp','2026-02-02',NULL,NULL)")
        conn.commit()
    page = _page(client, appliance_id="appl-ryzen")
    assert "Ryzen Camera 1" in page
    assert "Garage" not in page
    assert "cam-ryzen-2" not in page


def test_camera_names_are_escaped_in_the_remove_button(client, world):
    with override_target(sqlite_path=str(world)):
        conn = sqlite3.connect(world)
        conn.execute("UPDATE cameras SET name=? WHERE id='cam-ryzen-1'", ('Gate"><script>alert(1)</script>',))
        conn.commit()
    page = _page(client, appliance_id="appl-ryzen")
    assert "<script>alert(1)</script>" not in page
    assert 'data-camera-name="Gate&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in page


# ------------------------------------------------------------------ the script
SCRIPT = live_view_page._CAMERA_MANAGE_JS


def test_server_text_is_only_ever_set_as_text():
    assert "innerHTML" not in SCRIPT and "insertAdjacentHTML" not in SCRIPT and "outerHTML" not in SCRIPT
    assert "message.textContent=refusal" in SCRIPT
    assert "showToast(String(body.detail||'The camera could not be removed.'))" in SCRIPT


def test_controls_follow_the_cloud_flags():
    assert "const manage=capacity.can_manage===true;" in SCRIPT
    assert "add.hidden=!(manage&&capacity.can_add_camera===true);" in SCRIPT
    assert "document.querySelectorAll('.remove-camera').forEach(button=>{button.hidden=!manage;});" in SCRIPT
    assert "if(body&&body.camera_capacity)render(body.camera_capacity);" in SCRIPT  # older cloud: nothing shown


def test_removal_asks_for_confirmation_and_says_what_is_kept():
    confirm = SCRIPT.index("window.confirm(")
    assert confirm < SCRIPT.index("{method:'DELETE'}")
    assert "if(!ok)return;" in SCRIPT[confirm:]
    for phrase in ("Its recordings, events and clips are kept.", "forgets the camera and its saved password",
                   "If you add this camera again later, it starts as a new camera."):
        assert phrase in SCRIPT
    assert "encodeURIComponent(button.dataset.cameraId)" in SCRIPT


def test_the_subscription_link_must_be_a_site_path():
    pattern = re.search(r"/\^(.*?)\$/\.test\(value\)", SCRIPT).group(1).replace("\\\\", "\\")
    site_path = re.compile("^" + pattern + "$")
    assert site_path.match("/subscription-portal")
    assert site_path.match("/subscription-portal?tab=cameras")
    for bad in ("//evil.example/x", "https://evil.example", "javascript:alert(1)", "subscription-portal", "/x y", '/x"y'):
        assert not site_path.match(bad), bad


# ------------------------------------------------------------------ Add, in setup's discovery step
def test_setup_links_a_capacity_refusal_to_my_subscription(client, world):
    response = client.get("/customer/setup", params={"step": 4, "appliance": "appl-ryzen"}, cookies=OWNER)
    assert response.status_code == 200, response.text
    page = response.text
    assert "function showProvisioningError(message,code){" in page
    assert "['camera_limit_reached','camera_plan_required','vms_license_required'].includes(code)" in page
    assert "link.href='/subscription-portal';link.textContent='My subscription'" in page
    # The provision refusal (403 + code) and the appliance-side refusal (job failure_code) both pass their code.
    assert page.count("Try again or contact support.`,r.code)") == 2
    assert "showProvisioningError(r.message||'The camera could not be added. Try again.',r.failure_code)" in page
    assert "el.textContent=message" in page  # the cloud's text is set as text
