"""Customer download of the AnyAiCam VMS installer (2026-10-01).

A customer owner whose account holds a VMS software license (bought for a
customer-owned PC, or included with an AnyAiCam appliance) downloads the
exact published installer from My subscription. Everyone else is refused,
and the file is never reachable through the public /storage/ route.
"""
import hashlib
import json
from pathlib import Path

import pytest

from test_pricing_ff_commission import (  # noqa: F401 -- fixtures
    OWNER,
    _cookie,
    _paid_appliance_order,
    _seed,
    db_path,
    license_portal,
    portal,
)

COMMIT = "5948d74c6cbf3f509a2cd8ca4a5cda7f00f09be0"
PACKAGE = f"anyaicam-appliance-installer-1.1.0-vms-{COMMIT[:12]}.tar.gz"


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    import customer_downloads
    from object_storage import LocalStorage
    store = LocalStorage(tmp_path / "objects")
    monkeypatch.setattr(customer_downloads, "get_storage", lambda: store)
    return store


@pytest.fixture()
def package(tmp_path):
    path = tmp_path / PACKAGE
    path.write_bytes(b"installer-bytes" * 1000)
    return path


def _publish(package):
    import customer_downloads
    return customer_downloads.publish_vms_installer(package, commit=COMMIT, version="1.1.0")


def _license(db_path, product="vms_license", quantity=8, customer_id="cust-1"):
    import customer_entitlements as ce
    ce.upsert_entitlement(customer_id=customer_id, product=product, camera_slot_quantity=quantity)


# ------------------------------------------------------------------ publishing

def test_publish_records_size_sha256_and_commit(storage, package):
    record = _publish(package)
    assert record["sha256"] == hashlib.sha256(package.read_bytes()).hexdigest()
    assert record["size_bytes"] == package.stat().st_size and record["commit"] == COMMIT
    import customer_downloads
    assert customer_downloads.latest_vms_installer() == record


def test_publish_refuses_a_package_from_a_different_commit(storage, tmp_path):
    import customer_downloads
    other = tmp_path / "anyaicam-appliance-installer-1.1.0-vms-2e1086246293.tar.gz"
    other.write_bytes(b"x")
    with pytest.raises(ValueError):
        customer_downloads.publish_vms_installer(other, commit=COMMIT, version="1.1.0")
    bad_name = tmp_path / "setup.exe"
    bad_name.write_bytes(b"x")
    with pytest.raises(ValueError):
        customer_downloads.publish_vms_installer(bad_name, commit=COMMIT, version="1.1.0")


def test_nothing_published_is_not_an_error(storage):
    import customer_downloads
    assert customer_downloads.latest_vms_installer() is None


# ------------------------------------------------------------------ who may download

def test_licensed_owner_downloads_the_exact_bytes(license_portal, db_path, storage, package):
    client, _, _ = license_portal
    _seed(db_path)
    _license(db_path)
    _publish(package)
    response = client.get("/api/customer/downloads/vms-installer", cookies=_cookie(*OWNER))
    assert response.status_code == 200 and response.content == package.read_bytes()
    assert response.headers["x-content-sha256"] == hashlib.sha256(package.read_bytes()).hexdigest()
    assert PACKAGE in response.headers["content-disposition"]


def test_appliance_customer_is_licensed_through_the_appliance(license_portal, db_path, storage, package):
    client, _, _ = license_portal
    _seed(db_path)
    _paid_appliance_order(db_path)
    _license(db_path, product="camera_slots_local", quantity=8)
    _publish(package)
    assert client.get("/api/customer/downloads/vms-installer", cookies=_cookie(*OWNER)).status_code == 200


def test_owner_without_a_license_is_refused(license_portal, db_path, storage, package):
    client, _, _ = license_portal
    _seed(db_path)
    _license(db_path, product="camera_slots_local", quantity=8)  # a plan alone is not a license
    _publish(package)
    response = client.get("/api/customer/downloads/vms-installer", cookies=_cookie(*OWNER))
    assert response.status_code == 403 and "license is required" in response.json()["detail"]


def test_viewer_partner_and_signed_out_are_refused(license_portal, db_path, storage, package):
    client, _, _ = license_portal
    _seed(db_path)
    _license(db_path)
    _publish(package)
    viewer = client.get("/api/customer/downloads/vms-installer", cookies=_cookie("viewer@example.test", "customer_viewer", "cust-1"))
    assert viewer.status_code == 403 and "account owner" in viewer.json()["detail"]
    assert client.get("/api/customer/downloads/vms-installer", cookies=_cookie("sales@example.test", "salesperson")).status_code == 403
    assert client.get("/api/customer/downloads/vms-installer").status_code in (401, 403)  # signed out


def test_another_customers_license_never_counts(license_portal, db_path, storage, package):
    client, _, _ = license_portal
    _seed(db_path)
    _seed(db_path, customer_id="cust-2", email="other@example.test")
    _license(db_path, customer_id="cust-2")
    _publish(package)
    assert client.get("/api/customer/downloads/vms-installer", cookies=_cookie(*OWNER)).status_code == 403


def test_licensed_owner_before_any_release_gets_a_plain_404(license_portal, db_path, storage):
    client, _, _ = license_portal
    _seed(db_path)
    _license(db_path)
    response = client.get("/api/customer/downloads/vms-installer", cookies=_cookie(*OWNER))
    assert response.status_code == 404 and "not available yet" in response.json()["detail"]


def test_release_details_are_only_shown_to_eligible_owners(license_portal, db_path, storage, package):
    client, _, _ = license_portal
    _seed(db_path)
    _publish(package)
    unlicensed = client.get("/api/customer/downloads", cookies=_cookie(*OWNER)).json()
    assert unlicensed["eligible"] is False and "vms_installer" not in unlicensed
    _license(db_path)
    licensed = client.get("/api/customer/downloads", cookies=_cookie(*OWNER)).json()
    assert licensed["eligible"] is True and licensed["vms_installer"]["commit"] == COMMIT
    assert "key" not in licensed["vms_installer"]  # no storage paths leak


def test_the_public_storage_route_never_serves_downloads(license_portal, db_path, storage, package):
    client, _, _ = license_portal
    record = _publish(package)
    import cloud_features
    response = client.get(f"/storage/downloads/{record['key']}", cookies=_cookie(*OWNER))
    assert response.status_code == 404
    assert "downloads" not in cloud_features.PUBLIC_LOCAL_STORAGE_CATEGORIES


# ------------------------------------------------------------------ My subscription

def test_my_subscription_shows_the_download_to_a_licensed_owner(license_portal, db_path, storage, package):
    client, _, _ = license_portal
    _seed(db_path)
    _license(db_path, product="camera_slots_local", quantity=8)
    _license(db_path)
    record = _publish(package)
    html = client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    assert 'id="vms-installer-download"' in html and 'href="/api/customer/downloads/vms-installer"' in html
    assert record["sha256"] in html and "Ubuntu 24.04" in html and "sudo ./install.sh" in html


def test_my_subscription_hides_the_download_without_a_license(license_portal, db_path, storage, package):
    client, _, _ = license_portal
    _seed(db_path)
    _license(db_path, product="camera_slots_local", quantity=8)
    _publish(package)
    html = client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    assert 'id="vms-installer-download"' not in html and "/api/customer/downloads/vms-installer" not in html


def test_my_subscription_says_when_the_installer_is_not_released_yet(license_portal, db_path, storage):
    client, _, _ = license_portal
    _seed(db_path)
    _license(db_path, product="camera_slots_local", quantity=8)
    _license(db_path)
    html = client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    assert "will appear here when it is released" in html


# ------------------------------------------------------------------ install + activate steps

def test_steps_use_the_customer_claim_flow_with_this_portal_and_the_plan_mode(license_portal, db_path, storage, package, monkeypatch):
    """The customer path is `anyaicam-setup --claim` with this portal's
    address: plain `anyaicam-setup` is the administrator Cloud ID + token
    flow, and its Portal URL default is the appliance's own local VMS."""
    import main
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://portal.anyaicam.com")
    client, _, _ = license_portal
    _seed(db_path)
    _license(db_path, product="camera_slots_local", quantity=8)
    _license(db_path)
    _publish(package)
    html = client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    assert 'id="vms-installer-steps"' in html
    assert "sudo ./install.sh --product-mode=local" in html and "sudo ./validate.sh" in html
    assert "anyaicam-setup --claim --portal-url=https://portal.anyaicam.com" in html
    assert 'href="/customer/claim-appliance"' in html
    # The archive has no top-level folder: unpack it into one, by its real name.
    # Verify the published SHA-256 first, then unpack into a new folder for this build.
    record_sha = __import__("hashlib").sha256(package.read_bytes()).hexdigest()
    assert f'echo "{record_sha}  {PACKAGE}" | sha256sum -c' in html
    assert html.index("sha256sum -c") < html.index("tar -xzf")
    assert f"mkdir anyaicam-installer-{COMMIT[:12]} &amp;&amp; tar -xzf {PACKAGE} -C anyaicam-installer-{COMMIT[:12]}" in html
    assert "mkdir -p" not in html
    assert "anyaicam-setup</code>" not in html  # the administrator flow is never offered


def test_steps_follow_the_plan_and_never_offer_a_local_address():
    import main
    hybrid = main._installer_steps_html("hybrid")
    assert "--product-mode=hybrid" in hybrid
    none = main._installer_steps_html(None)
    assert "--product-mode" not in none and "Choose <strong>local</strong> or <strong>hybrid</strong>" in none
    for local in ("", "http://localhost:8000", "http://127.0.0.1:8000"):
        original = main.PUBLIC_BASE_URL
        try:
            main.PUBLIC_BASE_URL = local
            steps = main._installer_steps_html("local")
        finally:
            main.PUBLIC_BASE_URL = original
        assert "--portal-url" not in steps and "enter the address of this portal" in steps
        assert "anyaicam-setup --claim" in steps


def test_steps_escape_the_portal_address():
    import main
    original = main.PUBLIC_BASE_URL
    try:
        main.PUBLIC_BASE_URL = 'https://p.example/"><script>x</script>'
        steps = main._installer_steps_html("local")
    finally:
        main.PUBLIC_BASE_URL = original
    assert "<script>" not in steps
