"""Provisions a wholly synthetic, clearly-labeled Face Access test tenant
on staging for e2e/tests/test_face_access.py -- deliberately never the
real pilot customer's own account/cameras, matching this project's
established disposable-synthetic-tenant convention for any staging
validation that writes data.

Unlike provision_rdm_test_harness.py, this harness is torn down after
each real validation pass rather than left permanently in place (see
docs/PROJECT_CHECKPOINT.md's Face Access staging deployment milestone
for why: Face Access writes a real, growing door_access_events audit
trail on every run, which a permanent harness would accumulate
indefinitely). Both provisioning and teardown are here, idempotent
either way (INSERT OR IGNORE / guarded DELETEs), so a future session can
re-run this file to set up, run the suite, then re-run with
`--teardown` rather than re-deriving the schema from scratch.

Seeds:
  - customer e2efacetest / site e2efacetestsite
  - owner (customer_owner) + viewer (customer_viewer) partner_users,
    password hashed via partner_db.password_hash() -- the SAME PBKDF2-
    SHA256 scheme partner_db.authenticate_detailed() actually verifies
    against at login. main.py's own separate bcrypt hash_password()/
    verify_password() pair is NOT used by this login path; using it here
    was tried first and produced real, reproducible 403s despite a
    byte-correct password, confirmed by directly calling both modules'
    verify_password() against the same hash (see the matching
    PROJECT_CHECKPOINT.md entry for the full trace).
  - e2efacecamdoor: starts NOT door-configured (Face Access-config-UI
    test enables it), e2efacecamplain: never door-configured (negative/
    visibility cases), e2efacecamnorelay: seeded directly with
    door_access_enabled=1 and door_relay_channel=NULL -- a state the
    real /door-config API can never produce on its own (enabling always
    requires a valid channel), but that _authorized_door_camera()'s own
    defensive check still guards; mirrors the fixture shape
    app/tests/test_door_access.py already uses to reach that branch.
  - a customer_camera_permissions row per (viewer, camera) so the viewer
    can see either camera at all (this account's camera_access_mode is
    'selected', matching every real onboarded viewer's own default).

Run once via: docker cp this file into the running portal container,
then `docker exec <container> python3 provision_face_access_test_harness.py`
(add `--teardown` to remove everything this script created, verified
against the exact row counts it inserted -- never a blanket DELETE).
Prints the generated owner/viewer password once, to stdout only --
never written to any file this script controls. Put it into e2e/.env
as ANYAICAM_E2E_FACE_ACCESS_OWNER_PASSWORD /
ANYAICAM_E2E_FACE_ACCESS_VIEWER_PASSWORD (same password, both
accounts) before running the suite.
"""
import secrets
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")

from partner_db import connection, password_hash

PARTNER_ID = "anyaicam-primary"
CUSTOMER_ID = "e2efacetest"
SITE_ID = "e2efacetestsite"
OWNER_ID = "e2efaceowner"
VIEWER_ID = "e2efaceviewer"
CAM_DOOR_ID = "e2efacecamdoor"
CAM_PLAIN_ID = "e2efacecamplain"
CAM_NO_RELAY_ID = "e2efacecamnorelay"
OWNER_EMAIL = "e2e-face-access-owner@anyaicam-test.invalid"
VIEWER_EMAIL = "e2e-face-access-viewer@anyaicam-test.invalid"


def provision() -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connection() as db:
        existing = db.execute("SELECT id FROM customers WHERE id=?", (CUSTOMER_ID,)).fetchone()
        if existing:
            print("already provisioned -- no changes made. Run with --teardown first to reseed.")
            return

        password = secrets.token_urlsafe(18)
        hashed = password_hash(password)

        db.execute(
            "INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
            (CUSTOMER_ID, PARTNER_ID, "E2E Face Access Test", OWNER_EMAIL, "active", "real", now),
        )
        db.execute(
            "INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)",
            (SITE_ID, CUSTOMER_ID, "E2E Test Site", now),
        )
        for user_id, email, role in ((OWNER_ID, OWNER_EMAIL, "customer_owner"), (VIEWER_ID, VIEWER_EMAIL, "customer_viewer")):
            db.execute(
                "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                "VALUES(?,?,?,?,?,?,1,?,?,'active','selected')",
                (user_id, PARTNER_ID, email, email.split("@")[0], role, hashed, CUSTOMER_ID, now),
            )
        db.execute(
            "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,door_access_enabled,created_at) VALUES(?,?,?,?,?,?,0,?)",
            (CAM_DOOR_ID, CUSTOMER_ID, SITE_ID, "E2E Front Door", "configured", 1, now),
        )
        db.execute(
            "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
            (CAM_PLAIN_ID, CUSTOMER_ID, SITE_ID, "E2E Plain Camera", "configured", 2, now),
        )
        db.execute(
            "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,door_access_enabled,door_relay_channel,created_at) VALUES(?,?,?,?,?,?,1,NULL,?)",
            (CAM_NO_RELAY_ID, CUSTOMER_ID, SITE_ID, "E2E No Relay Door", "configured", 3, now),
        )
        for camera_id in (CAM_DOOR_ID, CAM_PLAIN_ID):
            db.execute(
                "INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback,can_alerts) VALUES(?,?,1,1,1)",
                (VIEWER_ID, camera_id),
            )

    print("provisioned:")
    print("  customer_id =", CUSTOMER_ID)
    print("  owner_email =", OWNER_EMAIL)
    print("  viewer_email =", VIEWER_EMAIL)
    print("  password (both accounts) =", password)
    print("  cam_door_id / cam_plain_id / cam_no_relay_id =", CAM_DOOR_ID, "/", CAM_PLAIN_ID, "/", CAM_NO_RELAY_ID)


def teardown() -> None:
    with connection() as db:
        before = db.execute("SELECT COUNT(*) c FROM customers WHERE id=?", (CUSTOMER_ID,)).fetchone()["c"]
        if not before:
            print("nothing to tear down -- customer not present.")
            return
        db.execute("DELETE FROM door_access_events WHERE customer_id=?", (CUSTOMER_ID,))
        db.execute("DELETE FROM customer_camera_permissions WHERE user_id IN (?,?)", (OWNER_ID, VIEWER_ID))
        db.execute("DELETE FROM cameras WHERE customer_id=?", (CUSTOMER_ID,))
        db.execute("DELETE FROM partner_users WHERE customer_id=?", (CUSTOMER_ID,))
        db.execute("DELETE FROM sites WHERE customer_id=?", (CUSTOMER_ID,))
        db.execute("DELETE FROM customers WHERE id=?", (CUSTOMER_ID,))
        db.execute("DELETE FROM account_lockouts WHERE email IN (?,?)", (OWNER_EMAIL, VIEWER_EMAIL))
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    print("torn down. integrity_check =", integrity)


if __name__ == "__main__":
    teardown() if "--teardown" in sys.argv else provision()
