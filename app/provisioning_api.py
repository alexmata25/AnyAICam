"""HTTP surface for AnyAiCam customer provisioning Phase 1, layered on
top of customer_entitlements.py and the pre-existing appliance/
provisioning infrastructure (provisioning_service.py, appliance_cloud.py).

Endpoints
---------
GET  /api/customer/entitlements   Authoritative entitlement list + total
                                   camera-slot capacity for the caller's
                                   own customer (browser session, role
                                   customer_owner).
GET  /api/customer/installations  Every appliance/installation bound to
                                   the caller's own customer, alongside
                                   that same authoritative capacity --
                                   so a customer (or the setup wizard)
                                   can see "N of M slots claimed" without
                                   the browser having to combine two
                                   separate calls.
POST /api/provisioning/release    Revokes ONE installation's binding to
                                   the caller's own customer (lost/stolen
                                   device, replacing a PC, reinstalling
                                   Windows). Local-DB side only in Phase
                                   1: it marks the appliance
                                   activation_status='released' so a
                                   fresh activation token can claim it
                                   again later. Telling AWS itself to
                                   revoke the installation's credential
                                   is future work -- AwsProvisioningBackend
                                   is intentionally unimplemented (see
                                   provisioning_service.py); this phase
                                   does not touch production AWS.
POST /api/provisioning/refresh    Appliance-facing (NOT browser-facing):
                                   an already-activated installation
                                   calls this periodically to learn its
                                   current camera-slot entitlement.
                                   Authenticated exactly like every other
                                   appliance-facing endpoint --
                                   appliance_cloud.authenticate_appliance()
                                   (signed request: X-Appliance-Id,
                                   X-Request-Timestamp, X-Request-Nonce,
                                   Bearer credential; replay-protected via
                                   appliance_request_nonces) -- reused
                                   rather than inventing a second
                                   appliance-identity/auth mechanism.

Deliberately NOT added this phase
----------------------------------
POST /api/provisioning/claim -- the spec names this endpoint, but
POST /api/customer/appliances/link (partner_workspace.py) already does
exactly this: given cloud_id + activation_token, it calls
provisioning_service.get_provisioning_backend().verify_link() and binds
the appliance to the caller's own customer_id, tenant-scoped. That
function lives as a closure inside register_partner_workspace_routes()
(shares its `shell` factory and helper closures), so a second top-level
implementation here would either duplicate its logic or require
refactoring a working, already-tested route for a naming preference
alone. /api/customer/appliances/link IS the claim endpoint; see the
Phase 1 report for this explicit decision.
"""
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request

from partner_db import audit, connection, row, rows
from partner_portal import partner_identity

from customer_entitlements import get_entitlements_for_customer, total_camera_slots


def _customer_owner(request: Request) -> dict:
    identity = partner_identity(request)
    if not identity or identity.get("role") != "customer_owner":
        raise HTTPException(status_code=403, detail="Customer owner permission required.")
    return identity


def register_provisioning_api_routes(app: FastAPI) -> None:
    @app.get("/api/customer/entitlements")
    def customer_entitlements_endpoint(request: Request) -> dict:
        identity = _customer_owner(request)
        customer_id = identity["customer_id"]
        entitlements = get_entitlements_for_customer(customer_id)
        return {
            "customer_id": customer_id,
            "entitlements": entitlements,
            "total_camera_slots": total_camera_slots(customer_id),
        }

    @app.get("/api/customer/installations")
    def customer_installations_endpoint(request: Request) -> dict:
        identity = _customer_owner(request)
        customer_id = identity["customer_id"]
        installations = rows(
            "SELECT id,cloud_id,appliance_type,serial_number,software_version,last_check_in,online_status,"
            "activation_status,created_at FROM appliances WHERE customer_id=? ORDER BY created_at",
            (customer_id,),
        )
        return {
            "customer_id": customer_id,
            "installations": installations,
            "camera_slots_purchased": total_camera_slots(customer_id),
            "installations_claimed": sum(1 for item in installations if item["activation_status"] in ("linked", "activated")),
        }

    @app.post("/api/provisioning/release")
    def provisioning_release(request: Request, payload: dict) -> dict:
        identity = _customer_owner(request)
        appliance_id = str((payload or {}).get("appliance_id") or "").strip()
        if not appliance_id:
            raise HTTPException(status_code=400, detail="appliance_id is required.")
        appliance = row("SELECT * FROM appliances WHERE id=? AND customer_id=?", (appliance_id, identity["customer_id"]))
        if not appliance:
            raise HTTPException(status_code=404, detail="Installation not found on this account.")
        now = datetime.now().isoformat()
        with connection() as db:
            db.execute(
                "UPDATE appliances SET activation_status='released',online_status='offline' WHERE id=?",
                (appliance_id,),
            )
            # Revoke every live credential too -- a released installation
            # must not keep calling /api/provisioning/refresh or any other
            # appliance-authenticated endpoint as if it were still claimed.
            db.execute(
                "UPDATE appliance_credentials SET revoked_at=? WHERE appliance_id=? AND revoked_at IS NULL",
                (now, appliance_id),
            )
        audit(identity, "installation.released", "appliance", appliance_id)
        return {
            "message": "Installation released. It can be re-claimed later with a new activation token.",
            "appliance_id": appliance_id,
        }

    @app.post("/api/provisioning/refresh")
    def provisioning_refresh(request: Request) -> dict:
        from appliance_cloud import authenticate_appliance  # deferred: avoid a module-load-order cycle

        appliance = authenticate_appliance(request)
        customer_id = appliance["customer_id"]
        entitlements = get_entitlements_for_customer(customer_id)
        return {
            "cloud_id": appliance["cloud_id"],
            "activation_status": appliance.get("activation_status"),
            "camera_slot_quantity": total_camera_slots(customer_id),
            "entitlements": [
                {
                    "product": item["product"],
                    "camera_slot_quantity": item["camera_slot_quantity"],
                    "status": item["status"],
                    "expires_at": item["expires_at"],
                }
                for item in entitlements
            ],
        }
