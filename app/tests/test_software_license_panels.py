"""My subscription -> Software License as three panels (VMS 1.2.7, owner request 2026-10-15).

Rendered from Red's data contract (GET /api/customer/software-license, LICENSE_REVIEW.md):
license, registered appliances, approved downloads. A running system never needs a download;
updates install only when the owner chooses; the pre-1.2.6 installer carries a reinstall warning.
"""
from datetime import datetime, timedelta

import software_license_panels as panels
from test_customer_downloads import OWNER, _cookie, _license, _publish, _seed, db_path, license_portal, package, portal, storage  # noqa: F401


def _contract(**overrides):
    base = {
        "account": {"customer_id": "cust-1", "name": "Bea's Bakery"},
        "license": {"kind": "appliance_included", "cameras": 8, "status": "active", "label": panels.LICENSE_LABELS["appliance_included"]},
        "appliances": [], "downloads": [], "notes": {"installer_older_than_installed": False},
    }
    base.update(overrides)
    return base


def _appliance(**overrides):
    item = {"appliance_id": "a1", "cloud_id": "AIC-C814766E", "name": "Store front", "connection": "online",
            "last_check_in": datetime.now().isoformat(), "installed_version": "1.2.6",
            "version_reported_at": datetime.now().isoformat(), "registration": "activated"}
    item.update(overrides)
    return item


def _download(version="1.2.6", platform="linux"):
    return {"platform": platform, "version": version, "commit": "66a0012ccc97", "filename": f"installer-{version}.tar.gz",
            "size_bytes": 899713600, "sha256": "a" * 64, "signed": platform == "windows",
            "min_requirements": "Ubuntu 24.04 (64-bit PC), 4+ CPU cores, 8 GB+ memory, 100 GB free disk",
            "install_flow": platform, "url": f"/api/customer/downloads/{platform}"}


def test_license_panel_shows_type_cameras_status_and_account():
    html = panels.license_panel(_contract())
    assert "My software license" in html and "included with your AnyAiCam appliance" in html
    assert "Up to 8 cameras" in html and ">Active<" in html and "Bea&#x27;s Bakery" in html
    assert "stays the same when you reinstall or update" in html


def test_appliances_panel_shows_connection_reported_version_and_registration():
    html = panels.appliances_panel(_contract(appliances=[
        _appliance(),
        _appliance(cloud_id="AIC-OLD", connection="offline", last_check_in=(datetime.now() - timedelta(hours=5)).isoformat(),
                   installed_version="1.2.4", version_reported_at=(datetime.now() - timedelta(days=3)).isoformat()),
        _appliance(cloud_id="AIC-NEW", connection="never", last_check_in=None, installed_version=None, version_reported_at=None,
                   registration="claimed_pending"),
    ]))
    assert "VMS 1.2.6 · reported just now" in html and "Activated" in html and ">Online<" in html
    assert "Offline · last seen 5 hours ago" in html and "Last reported VMS 1.2.4 (3 days ago)" in html
    assert "Not connected yet" in html and "Not reported" in html and "Waiting to finish activation" in html


def test_unreported_versions_are_never_shown_as_a_version():
    for raw in (None, "", "Unknown", "unknown", "0.1.0"):
        assert panels.reported_version(raw) is None
    assert panels.reported_version("1.2.6") == "1.2.6"


def test_no_appliances_explains_how_to_claim():
    html = panels.appliances_panel(_contract())
    assert 'id="sl-no-appliances"' in html and 'href="/customer/claim-appliance"' in html


def test_downloads_say_a_running_system_needs_nothing_and_updates_are_the_owners_choice():
    html = panels.downloads_panel(_contract(appliances=[_appliance()], downloads=[_download()]))
    assert "don&rsquo;t need to download anything if your AnyAiCam is already installed and running" in html
    assert "installed only when you choose" in html and "automatically" not in html
    assert "AnyAiCam VMS 1.2.6 for Linux" in html and "Download installer" in html and "sl-downgrade-warning" not in html


def test_the_pre_1_2_6_installer_carries_a_reinstall_warning():
    html = panels.downloads_panel(_contract(downloads=[_download("1.2.4")]))
    assert 'id="sl-downgrade-warning"' in html and "do not reinstall it with this installer" in html


def test_a_newer_installed_version_warns_even_on_a_later_installer():
    html = panels.downloads_panel(_contract(downloads=[_download("1.2.6")], notes={"installer_older_than_installed": True}))
    assert "runs a newer version than this installer" in html


def test_only_the_releases_in_the_contract_are_offered():
    assert "for Windows" not in panels.downloads_panel(_contract(downloads=[_download()]))
    both = panels.downloads_panel(_contract(downloads=[_download(), _download("1.2.7", "windows")]))
    assert "AnyAiCam VMS 1.2.7 for Windows" in both  # only because the (approved) record lists it
    empty = panels.downloads_panel(_contract())
    assert 'id="sl-no-installer"' in empty and "Download installer" not in empty


def test_version_comparison():
    assert panels.version_tuple("1.2.4") < panels.version_tuple("1.2.6") < panels.version_tuple("1.2.10")
    assert panels.version_tuple("Unknown") is None


def test_my_subscription_shows_the_three_panels_in_order(license_portal, db_path, storage, package):
    client, _, _ = license_portal
    _seed(db_path)
    _license(db_path, product="camera_slots_local", quantity=8)
    _license(db_path)
    _publish(package)
    html = client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    positions = [html.index(marker) for marker in ('id="sl-license"', 'id="sl-appliances"', 'id="sl-downloads"')]
    assert positions == sorted(positions)
    assert "Software license</h2>" not in html  # the old combined card is gone
    assert 'id="sl-downgrade-warning"' in html  # the published test installer is 1.1.0, older than 1.2.6


def test_capacity_without_a_license_row_never_says_stand_alone(license_portal, db_path, storage, package):
    """Red review 2026-10-15: the capacity-only path was mislabelled "Existing stand-alone VMS license"."""
    client, _, _ = license_portal
    _seed(db_path)
    _license(db_path, product="camera_slots_local", quantity=8)  # no vms_license row
    html = client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    assert "stand-alone" not in html.lower() and "standalone" not in html.lower()


def test_installer_older_than_installed_only_uses_fully_reported_versions():
    def older(installed, served="1.2.4"):
        return panels.installer_older_than_installed([_appliance(cloud_id=f"A{i}", installed_version=v) for i, v in enumerate(installed)],
                                                    [_download(served)])
    assert older(["1.2.6"]) is True          # the owner's real situation: 1.2.6 RC3 against the served 1.2.4
    assert older([None, "Unknown"]) is False  # not reported = unknown, never "older"
    assert older(["1.2"]) is False           # not a full major.minor.patch
    assert older(["1.2.4"]) is False and older(["1.2.6"], served="1.2.6") is False
    assert panels.strict_version("1.2.6-rc3") == (1, 2, 6) and panels.strict_version("1.2") is None


def test_an_own_pc_buyer_sees_the_license_size_and_what_the_plan_covers_now():
    """Activation-first (2026-10-16, Green's review): Basic Local x 1 bought with the 8-camera one-time license must not
    read 'Licensed cameras: Up to 1 camera' next to an 8-camera license."""
    label = "One-time AnyAiCam VMS software license (up to 8 cameras) · your plan covers 1 camera"
    html = panels.license_panel(_contract(license={"kind": "per_camera_plan", "cameras": 1, "usable_cameras": 1,
                                                   "status": "active", "label": label}))
    assert "up to 8 cameras" in html and "your plan covers 1 camera" in html
    assert "Cameras you can run now" in html and "Up to 1 camera<" in html
    assert "Licensed cameras" not in html


def test_a_license_without_a_plan_never_claims_runnable_cameras():
    html = panels.license_panel(_contract(license={"kind": "one_time_vms_license", "cameras": 8, "usable_cameras": 0,
                                                   "status": "active", "label": panels.LICENSE_LABELS["one_time_vms_license"]}))
    assert "None yet: choose a camera plan in My subscription" in html and "Up to 8 cameras" not in html
