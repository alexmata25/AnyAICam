"""The appliance configuration carries the usable camera capacity (2026-10-03).

Appliances from 1.2.0 on run no camera until the cloud's configuration sync
says how many they may run (vms_capacity.py, fail closed). The cloud half
of 065fdd1 is what sends it: usable capacity = min(camera plan, VMS
license), and an appliance purchase includes a license matching the plan.
Without it a 1.2.0 appliance records nothing (found on Ryzen after the
1.2.0 repair against a cloud without this field)."""
import sqlite3

import pytest

from database_backend import override_target
from test_camera_slot_enforcement import (  # noqa: F401 -- fixtures and helpers
    _appliance_headers,
    _seed_and_activate_appliance,
    _seed_tenant,
    client,
    db_path,
)


def _entitle(db_path, customer_id="cust-1", *, plan=0, license=0, appliance_order=False):
    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        if plan:
            ce.upsert_entitlement(customer_id=customer_id, product="camera_slots_local", camera_slot_quantity=plan, status="active")
        if license:
            ce.upsert_entitlement(customer_id=customer_id, product=ce.VMS_LICENSE_PRODUCT, camera_slot_quantity=license, status="active")
        if appliance_order:
            conn = sqlite3.connect(db_path)
            conn.execute("INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,amount_cents,status,created_at,updated_at) "
                         "VALUES('ord-1',?,?,?,?,?,?,?,?)",
                         (customer_id, "AIC-APPLIANCE-8", "AnyAiCam appliance (8 cameras)", "price_test", 49900, "paid", "2026-01-01", "2026-01-01"))
            conn.commit()
            conn.close()


def _configuration(client, db_path):
    token = _seed_and_activate_appliance(db_path)
    activated = client.post("/api/appliance/activate", json={"cloud_id": "AIC-SLOT1", "activation_token": token})
    assert activated.status_code == 200, activated.text
    response = client.get("/api/appliance/configuration", headers=_appliance_headers("appl-1", activated.json()["credential"]))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("plan,license,order,expected", [
    (8, 0, False, 0),      # camera plan but no software license
    (0, 8, False, 0),      # license but no camera plan
    (8, 4, False, 4),      # DIY: the smaller of the two
    (4, 8, False, 4),
    (8, 0, True, 8),       # appliance purchase includes a license matching the plan
    (0, 0, False, 0),
])
def test_usable_capacity_is_the_smaller_of_plan_and_license(client, db_path, plan, license, order, expected):
    _seed_tenant(db_path)
    _entitle(db_path, plan=plan, license=license, appliance_order=order)
    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        assert ce.usable_camera_capacity("cust-1") == expected
        breakdown = ce.capacity_breakdown("cust-1")
        assert breakdown["camera_slot_quantity"] == expected and breakdown["plan_camera_slots"] == plan


def test_the_appliance_configuration_carries_the_usable_capacity(client, db_path):
    _seed_tenant(db_path)
    _entitle(db_path, plan=8, license=4)
    config = _configuration(client, db_path)
    assert config["usable_camera_capacity"]["camera_slot_quantity"] == 4
    assert config["usable_camera_capacity"]["plan_camera_slots"] == 8


def test_an_appliance_customer_gets_their_full_plan(client, db_path):
    _seed_tenant(db_path)
    _entitle(db_path, plan=8, appliance_order=True)
    assert _configuration(client, db_path)["usable_camera_capacity"]["camera_slot_quantity"] == 8

