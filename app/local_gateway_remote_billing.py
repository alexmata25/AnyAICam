"""Appliance-scoped Local Gateway remote subscription integration.

Provider events must reach this module only through the existing verified
webhook. Pure helpers do not establish customer identity or payment on their
own; checkout and reconciliation must use the existing trusted VMS boundaries.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RemoteSubscriptionDecision:
    state: str
    reason: str


def evaluate_current_subscription(
    subscription: dict, *, customer_id: str, appliance_id: str,
    stripe_customer_id: str, price_id: str, payment_verified: bool,
) -> RemoteSubscriptionDecision:
    """Validate a provider-fetched subscription against trusted caller context.

    payment_verified must come from existing payment/reversal checks; a browser
    return or metadata alone never establishes payment. No grace is invented.
    """
    def deny(reason):
        return RemoteSubscriptionDecision("denied", reason)

    if not all(isinstance(v, str) and v.strip() == v and v
               for v in (customer_id, appliance_id, stripe_customer_id, price_id)):
        return deny("missing_trusted_binding")
    if (not isinstance(subscription, dict) or not isinstance(subscription.get("id"), str)
            or not subscription["id"].strip()):
        return deny("invalid_subscription")
    if subscription.get("customer") != stripe_customer_id:
        return deny("customer_mismatch")
    metadata = subscription.get("metadata")
    if not isinstance(metadata, dict):
        return deny("missing_binding")
    if metadata.get("anyaicam_customer_id") != customer_id:
        return deny("customer_mismatch")
    if metadata.get("anyaicam_appliance_id") != appliance_id:
        return deny("appliance_mismatch")
    if metadata.get("anyaicam_product") != "local_gateway_remote":
        return deny("product_mismatch")
    status = subscription.get("status")
    if not isinstance(status, str):
        return deny("subscription_not_active")
    if status in {"canceled", "incomplete_expired", "unpaid"}:
        return RemoteSubscriptionDecision("inactive", "subscription_inactive")
    if status != "active":
        return deny("subscription_not_active")
    items = subscription.get("items")
    data = items.get("data") if isinstance(items, dict) else None
    if not isinstance(data, list):
        return deny("invalid_items")
    matches = []
    for item in data:
        if not isinstance(item, dict):
            return deny("invalid_items")
        price = item.get("price")
        item_price = price.get("id") if isinstance(price, dict) else price
        if item_price == price_id:
            matches.append(item)
    if len(matches) != 1 or len(data) != 1:
        return deny("price_not_unique")
    quantity = matches[0].get("quantity")
    if type(quantity) is not int or quantity != 1:
        return deny("invalid_system_quantity")
    if payment_verified is not True:
        return deny("payment_not_verified")
    return RemoteSubscriptionDecision("active", "verified_active")


def reconcile_bound_subscription(
    db, subscription: dict, *, customer_id: str, appliance_id: str,
    stripe_customer_id: str, price_id: str, payment_verified: bool, verified_at: str,
    observed_binding: dict | None = None,
) -> RemoteSubscriptionDecision:
    """Update an existing exact binding after independent provider verification.

    This helper never creates or replaces a binding. In particular an old
    subscription cannot update a replacement. The caller owns the database
    transaction and must treat a denied result as no authorization, not as
    permission to reuse a stored active value.
    """
    decision = evaluate_current_subscription(
        subscription, customer_id=customer_id, appliance_id=appliance_id,
        stripe_customer_id=stripe_customer_id, price_id=price_id,
        payment_verified=payment_verified,
    )
    if decision.state not in {"active", "inactive"}:
        return decision
    if not isinstance(verified_at, str) or not verified_at.strip():
        return RemoteSubscriptionDecision("denied", "missing_verification_time")
    status = subscription["status"]
    period = subscription.get("current_period_end")
    if period is not None and (type(period) is not int or period < 0):
        return RemoteSubscriptionDecision("denied", "invalid_period")
    cancel = subscription.get("cancel_at_period_end", False)
    if type(cancel) is not bool:
        return RemoteSubscriptionDecision("denied", "invalid_cancellation_flag")
    snapshot_sql = ""
    snapshot_args = ()
    if observed_binding is not None:
        snapshot_args = tuple(observed_binding.get(key) for key in ("status", "stripe_status", "updated_at"))
        if not all(isinstance(value, str) and value for value in snapshot_args):
            return RemoteSubscriptionDecision("denied", "binding_snapshot_invalid")
        snapshot_sql = " AND status=? AND stripe_status=? AND updated_at=?"
    result = db.execute(
        "UPDATE local_gateway_remote_subscriptions SET status=?, stripe_status=?, "
        "current_period_end=?, cancel_at_period_end=?, updated_at=? "
        "WHERE appliance_id=? AND customer_id=? AND stripe_customer_id=? "
        "AND stripe_subscription_id=? AND stripe_price_id=? "
        "AND (? <> 'active' OR (status = 'active' AND EXISTS "
        "(SELECT 1 FROM appliances a WHERE a.id=local_gateway_remote_subscriptions.appliance_id "
        "AND a.customer_id=local_gateway_remote_subscriptions.customer_id "
        "AND a.gateway_edition='local_gateway')))" + snapshot_sql,
        (decision.state, status, period, int(cancel), verified_at, appliance_id,
         customer_id, stripe_customer_id, subscription["id"], price_id, decision.state, *snapshot_args),
    )
    if result.rowcount != 1:
        if observed_binding is not None:
            latest = db.execute(
                "SELECT status,stripe_status,updated_at FROM local_gateway_remote_subscriptions "
                "WHERE appliance_id=? AND customer_id=? AND stripe_subscription_id=?",
                (appliance_id, customer_id, subscription["id"]),
            ).fetchone()
            if latest is not None:
                current = tuple(latest[key] for key in ("status", "stripe_status", "updated_at")) if hasattr(latest, "keys") else tuple(latest)
                if current != snapshot_args:
                    return RemoteSubscriptionDecision("denied", "binding_changed")
        return RemoteSubscriptionDecision("denied", "binding_not_current")
    return decision


def bind_verified_paid_checkout(
    db, session: dict, subscription: dict, *, customer_id: str, appliance_id: str,
    stripe_customer_id: str, price_id: str, payment_verified: bool, verified_at: str,
    ended_subscription_id: str | None = None,
    observed_binding: dict | None = None, protect_observed_binding: bool = False,
) -> RemoteSubscriptionDecision:
    """Bind one paid checkout to its appliance without replacing active service.

    Invoke only inside the verified webhook's database transaction, after
    fetching current provider state, validating the $9.99 monthly price and
    verifying customer ownership of this appliance. The current checkout
    guard must still hold the same session for this appliance. This function
    does not create a checkout, commit, or call a provider.
    """
    decision = evaluate_current_subscription(
        subscription, customer_id=customer_id, appliance_id=appliance_id,
        stripe_customer_id=stripe_customer_id, price_id=price_id,
        payment_verified=payment_verified,
    )
    if decision.state != "active":
        return decision
    if not isinstance(session, dict) or session.get("mode") != "subscription":
        return RemoteSubscriptionDecision("denied", "invalid_checkout")
    if session.get("payment_status") not in {"paid", "no_payment_required"}:
        return RemoteSubscriptionDecision("denied", "checkout_not_paid")
    if session.get("subscription") != subscription["id"] or session.get("customer") != stripe_customer_id:
        return RemoteSubscriptionDecision("denied", "checkout_binding_mismatch")
    if not isinstance(verified_at, str) or not verified_at.strip():
        return RemoteSubscriptionDecision("denied", "missing_verification_time")
    session_id = session.get("id")
    if not isinstance(session_id, str) or not session_id:
        return RemoteSubscriptionDecision("denied", "invalid_checkout")
    pending = db.execute(
        "SELECT 1 FROM checkout_pending WHERE customer_id=? AND purchase=? "
        "AND session_id=? AND price_id=? AND quantity=1 AND status IN ('open','paid')",
        (customer_id, "local_gateway_remote:" + appliance_id, session_id, price_id),
    ).fetchone()
    if not pending:
        return RemoteSubscriptionDecision("denied", "checkout_not_current")
    owned = db.execute(
        "SELECT 1 FROM appliances WHERE id=? AND customer_id=? AND gateway_edition='local_gateway'",
        (appliance_id, customer_id),
    ).fetchone()
    if not owned:
        return RemoteSubscriptionDecision("denied", "appliance_not_owned")
    # A paid session may outlive a revoked edition authorization. Check the
    # current claim and decision under the same transaction as the binding.
    try:
        from gateway_internal_provisioning import edition_authorization_effective
        if not edition_authorization_effective(db, customer_id, appliance_id, "local_gateway"):
            return RemoteSubscriptionDecision("denied", "edition_authorization_invalid")
    except Exception:
        return RemoteSubscriptionDecision("denied", "edition_authorization_invalid")
    matches = [item for item in subscription["items"]["data"]
               if (item.get("price", {}).get("id") if isinstance(item.get("price"), dict)
                   else item.get("price")) == price_id]
    item_id = matches[0].get("id")
    if not isinstance(item_id, str) or not item_id:
        return RemoteSubscriptionDecision("denied", "missing_subscription_item")
    period = subscription.get("current_period_end")
    cancel = subscription.get("cancel_at_period_end", False)
    if (period is not None and (type(period) is not int or period < 0)) or type(cancel) is not bool:
        return RemoteSubscriptionDecision("denied", "invalid_subscription_metadata")
    existing = db.execute(
        "SELECT stripe_subscription_id,status,stripe_status,updated_at FROM local_gateway_remote_subscriptions "
        "WHERE appliance_id=? AND customer_id=?", (appliance_id, customer_id),
    ).fetchone()
    # Attempt-aware payments remain recorded for operator review until the
    # explicit activation/reconciliation path is independently accepted.
    metadata=session.get('metadata') or {}
    if isinstance(metadata,dict) and metadata.get('anyaicam_gateway_attempt'):
        bound_id = (existing['stripe_subscription_id'] if hasattr(existing, 'keys') else existing[0]) if existing else None
        if bound_id != subscription['id']:
            try:
                import gateway_checkout_attempts as attempts
                authorized=attempts.exact_paid_attempt_authorized(db,attempt_id=metadata['anyaicam_gateway_attempt'],
                    customer_id=customer_id,appliance_id=appliance_id,edition='local_gateway',price_id=price_id,
                    quantity=1,session=session,subscription=subscription)
                if not authorized or not attempts.PAID_ACTIVATION_READY:
                    return RemoteSubscriptionDecision("denied", "checkout_attempt_review_required")
            except Exception:
                return RemoteSubscriptionDecision("denied", "checkout_attempt_review_required")
    # Do not create or replace a binding while the authorization-bound
    # handoff is incomplete. A redelivery for the exact existing
    # subscription may still reconcile its current verified state.
    if not CHECKOUT_AUTHORIZATION_ATOMICALLY_ENFORCED:
        bound_id = (existing['stripe_subscription_id'] if hasattr(existing, 'keys') else existing[0]) if existing else None
        if bound_id != subscription['id']:
            return RemoteSubscriptionDecision("denied", "checkout_handoff_unavailable")
    if protect_observed_binding:
        fields = ("stripe_subscription_id", "status", "stripe_status", "updated_at")
        actual = (tuple(existing[key] for key in fields) if hasattr(existing, "keys") else tuple(existing)) if existing else None
        expected = tuple(observed_binding.get(key) for key in fields) if observed_binding else None
        if actual != expected:
            return RemoteSubscriptionDecision("denied", "binding_changed")
    if existing:
        if isinstance(existing, dict):
            bound_id, bound_status = existing["stripe_subscription_id"], existing["status"]
        else:
            bound_id, bound_status = existing[0], existing[1]
        if bound_id == subscription["id"]:
            return reconcile_bound_subscription(
                db, subscription, customer_id=customer_id, appliance_id=appliance_id,
                stripe_customer_id=stripe_customer_id, price_id=price_id,
                payment_verified=payment_verified, verified_at=verified_at,
                observed_binding=observed_binding if protect_observed_binding else None,
            )
        if bound_status != "inactive":
            return RemoteSubscriptionDecision("denied", "previous_subscription_not_inactive")
        if not ended_subscription_id or ended_subscription_id != bound_id:
            return RemoteSubscriptionDecision("denied", "previous_subscription_not_verified_ended")
        snapshot_sql = " AND stripe_status=? AND updated_at=?" if protect_observed_binding else ""
        snapshot_args = (observed_binding["stripe_status"], observed_binding["updated_at"]) if protect_observed_binding else ()
        updated = db.execute(
            "UPDATE local_gateway_remote_subscriptions SET stripe_customer_id=?, "
            "stripe_subscription_id=?,stripe_subscription_item_id=?,stripe_price_id=?,"
            "status='active',stripe_status=?,current_period_end=?,cancel_at_period_end=?,"
            "payment_failed_at=NULL,suspended_reason=NULL,updated_at=? "
            "WHERE appliance_id=? AND customer_id=? AND stripe_subscription_id=? AND status='inactive'" + snapshot_sql,
            (stripe_customer_id, subscription["id"], item_id, price_id, subscription["status"],
             period, int(cancel), verified_at, appliance_id, customer_id, bound_id, *snapshot_args),
        )
        if updated.rowcount != 1:
            return RemoteSubscriptionDecision("denied", "binding_changed")
    else:
        inserted = db.execute(
            "INSERT INTO local_gateway_remote_subscriptions "
            "(appliance_id,customer_id,stripe_customer_id,stripe_subscription_id,"
            "stripe_subscription_item_id,stripe_price_id,status,stripe_status,"
            "current_period_end,cancel_at_period_end,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'active',?,?,?,?,?) ON CONFLICT(appliance_id) DO NOTHING",
            (appliance_id, customer_id, stripe_customer_id, subscription["id"], item_id,
             price_id, subscription["status"], period, int(cancel), verified_at, verified_at),
        )
        if inserted.rowcount != 1:
            return RemoteSubscriptionDecision("denied", "binding_changed")
    return decision


def checkout_fields(*, customer_id: str, appliance_id: str, stripe_customer_id: str,
                    price_id: str, success_url: str, cancel_url: str) -> tuple[str, list[tuple[str, str]]]:
    """Construct isolated checkout fields; caller must authorize and verify price.

    The returned purchase key gives each appliance an independent checkout
    guard. URLs must be server-generated. This helper makes no provider call.
    """
    if not all(isinstance(v, str) and v.strip() == v and v
               for v in (customer_id, appliance_id, stripe_customer_id, price_id, success_url, cancel_url)):
        raise ValueError("missing checkout binding")
    metadata = {"anyaicam_product": "local_gateway_remote",
                "anyaicam_customer_id": customer_id, "anyaicam_appliance_id": appliance_id}
    fields = [
        ("mode", "subscription"), ("customer", stripe_customer_id),
        ("client_reference_id", customer_id), ("line_items[0][price]", price_id),
        ("line_items[0][quantity]", "1"), ("success_url", success_url), ("cancel_url", cancel_url),
    ]
    for key, value in metadata.items():
        fields.extend(((f"metadata[{key}]", value), (f"subscription_data[metadata][{key}]", value)))
    return "local_gateway_remote:" + appliance_id, fields


# A local database check cannot make a remote provider session creation atomic
# with authorization revocation. Keep checkout closed until that handoff has
# a reviewed, end-to-end authorization protocol.
CHECKOUT_AUTHORIZATION_ATOMICALLY_ENFORCED = False


def _eligible_appliance(appliance: dict) -> bool:
    """Reuse the existing token/claim enrollment and live-credential boundary."""
    from appliance_readiness import appliance_activated
    return bool(appliance and appliance.get("state") not in {"revoked", "retired"} and appliance_activated(appliance))


def _edition_verified(customer_id: str, appliance_id: str, edition: str) -> bool:
    """Do not offer service for an unverified or unreadable edition decision."""
    try:
        from partner_db import transaction
        from gateway_internal_provisioning import edition_authorization_effective
        with transaction() as db:
            return bool(edition_authorization_effective(db, customer_id, appliance_id, edition))
    except Exception:
        return False


def customer_appliance_status(customer_id: str, appliance: dict, *, owner: bool, now: int | None = None) -> dict:
    """Render conservative, last-confirmed billing state for one owned appliance."""
    import os
    import time
    from partner_db import row
    import customer_entitlements
    import per_camera_billing
    current_time = int(time.time()) if now is None else now
    if appliance.get("customer_id") != customer_id or appliance.get("gateway_edition") != "local_gateway":
        raise ValueError("Appliance ownership mismatch")
    appliance_id = appliance["id"]
    binding = row("SELECT status,stripe_status,payment_failed_at,current_period_end,cancel_at_period_end "
                  "FROM local_gateway_remote_subscriptions WHERE appliance_id=? AND customer_id=?", (appliance_id, customer_id))
    pending = row("SELECT status,expires_at FROM checkout_pending WHERE customer_id=? AND purchase=? "
                  "AND status IN ('creating','open','paid')", (customer_id, "local_gateway_remote:" + appliance_id))
    status = "not_subscribed"
    if binding:
        stored, stripe = binding.get("status"), binding.get("stripe_status")
        period = binding.get("current_period_end")
        if stripe == "incomplete_expired": status = "expired"
        elif stripe == "canceled": status = "canceled"
        elif stripe in {"past_due", "unpaid"} or binding.get("payment_failed_at"): status = "payment_failed"
        elif stored == "suspended": status = "suspended"
        elif stored == "inactive": status = "expired"
        elif stored == "active" and stripe == "active":
            if type(period) is not int or period <= 0: status = "unknown"
            elif period <= current_time: status = "expired"
            elif binding.get("cancel_at_period_end"): status = "canceling"
            else: status = "active"
        else: status = "unknown"
    pending_expiry = pending.get("expires_at") if pending else None
    pending_current = bool(pending and (pending.get("status") == "paid" or type(pending_expiry) is not int or pending_expiry > current_time))
    if pending_current and (not binding or status in {"canceled", "expired"}):
        status = "pending"
    licences = customer_entitlements.get_entitlements_for_customer(customer_id)
    licensed = (customer_entitlements._purchased_vms_license_capacity(licences) > 0
                and not per_camera_billing.own_pc_licence_reversed(customer_id))
    usable = _eligible_appliance(appliance)
    verified = _edition_verified(customer_id, appliance_id, "local_gateway")
    checkout_enabled = (CHECKOUT_AUTHORIZATION_ATOMICALLY_ENFORCED and
                        os.environ.get("ANYAICAM_LOCAL_GATEWAY_CHECKOUT_ENABLED", "").strip().lower() == "true")
    return {"appliance_id": appliance_id, "name": str(appliance.get("cloud_id") or appliance_id),
            "status": status, "local_license_active": bool(licensed), "appliance_ready": usable, "edition_verified": verified,
            "is_owner": owner, "checkout_enabled": checkout_enabled,
            "can_subscribe": bool(owner and checkout_enabled and usable and verified and licensed and status in {"not_subscribed", "canceled", "expired"}
                                  and (not binding or binding.get("status") == "inactive")),
            "can_manage": bool(owner and binding)}


def register_routes(app):
    """Register the customer-owned appliance checkout using existing guards."""
    import os
    from fastapi import HTTPException, Request
    from partner_portal import partner_identity

    def signed_in(request):
        identity = partner_identity(request)
        if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"} or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer sign-in required.")
        return identity

    @app.post("/api/operations/appliances/{appliance_id}/confirm-local-gateway")
    def confirm_claimed_local_gateway(appliance_id: str, payload: dict, request: Request):
        """Attach the existing server-trusted edition to an already claimed device."""
        from appliance_identity import has_global_administrator_grant
        from partner_db import transaction
        identity = partner_identity(request)
        if not identity or identity.get("role") != "administrator" or not identity.get("email"):
            raise HTTPException(status_code=403, detail="Global administrator permission required.")
        if not isinstance(payload, dict) or set(payload) != {"customer_id"} or not isinstance(payload["customer_id"], str) or not payload["customer_id"].strip() or payload["customer_id"].strip() != payload["customer_id"]:
            raise HTTPException(status_code=400, detail="Expected customer is required.")
        customer_id = payload["customer_id"]
        with transaction() as db:
            if not has_global_administrator_grant(db, email=identity["email"]):
                raise HTTPException(status_code=403, detail="Global administrator permission required.")
            appliance = db.execute("SELECT customer_id,activated_via,gateway_edition FROM appliances WHERE id=? AND customer_id=?",
                                   (appliance_id, customer_id)).fetchone()
            if not appliance:
                raise HTTPException(status_code=404, detail="Claimed appliance not found for this customer.")
            if appliance["activated_via"] != "claim":
                raise HTTPException(status_code=409, detail="This action applies only to claimed appliances.")
            if appliance["gateway_edition"] == "local_gateway":
                return {"status":"already_confirmed"}
            if appliance["gateway_edition"] is not None:
                raise HTTPException(status_code=409, detail="Appliance edition is already assigned.")
            # Claim, ownership and heartbeat do not prove which edition was
            # purchased. No authoritative edition evidence is connected to
            # claimed appliances yet; even direct API callers must wait.
            raise HTTPException(status_code=409, detail="Pending verification: trusted edition evidence is unavailable.")

    @app.get("/api/v2/customer/local-gateway/appliances")
    def local_gateway_appliances(request: Request):
        from partner_db import rows
        identity = signed_in(request)
        customer_id = identity["customer_id"]
        appliances = rows("SELECT * FROM appliances WHERE customer_id=? AND (gateway_edition IN (?,?) OR (activated_via='claim' AND gateway_edition IS NULL)) ORDER BY created_at", (customer_id, "local_gateway", "cloud_gateway"))
        local, cloud, pending = [], [], []
        for appliance in appliances:
            if appliance.get('customer_id') != customer_id:
                continue
            edition = appliance.get('gateway_edition')
            item = {'appliance_id':appliance['id'], 'name':str(appliance.get('cloud_id') or appliance['id'])}
            if edition == 'local_gateway':
                local.append(customer_appliance_status(customer_id, appliance, owner=identity['role'] == 'customer_owner'))
            elif edition == 'cloud_gateway' and _edition_verified(customer_id, appliance['id'], 'cloud_gateway'):
                from cloud_gateway_customer_status import camera_statuses
                item['cameras'] = camera_statuses(customer_id, appliance, edition_verified=True, rows=rows)
                cloud.append(item)
            else:
                pending.append(item)
        return {'appliances':local, 'cloud_appliances':cloud, 'pending_appliances':pending}

    @app.get("/api/v2/customer/local-gateway/appliances/{appliance_id}")
    def local_gateway_appliance(appliance_id: str, request: Request):
        from partner_db import row
        identity = signed_in(request)
        customer_id = identity["customer_id"]
        appliance = row("SELECT * FROM appliances WHERE id=? AND customer_id=? AND gateway_edition=?", (appliance_id, customer_id, "local_gateway"))
        if not appliance or appliance.get("customer_id") != customer_id or appliance.get("gateway_edition") != "local_gateway":
            raise HTTPException(status_code=404, detail="Appliance not found.")
        return customer_appliance_status(customer_id, appliance, owner=identity["role"] == "customer_owner")

    @app.post("/api/v2/customer/local-gateway/checkout")
    def local_gateway_checkout(payload: dict, request: Request):
        if os.environ.get("ANYAICAM_LOCAL_GATEWAY_CHECKOUT_ENABLED", "").strip().lower() != "true":
            raise HTTPException(status_code=503, detail="Local Gateway checkout is not available.")
        identity = partner_identity(request)
        if not identity or identity.get("role") != "customer_owner" or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer owner permission required.")
        customer_id = identity["customer_id"]
        if not isinstance(payload, dict) or set(payload) != {"appliance_id"}:
            raise HTTPException(status_code=400, detail="Choose one appliance.")
        appliance_id = payload["appliance_id"]
        if not isinstance(appliance_id, str) or not appliance_id or len(appliance_id) > 128 or appliance_id.strip() != appliance_id:
            raise HTTPException(status_code=400, detail="Choose one appliance.")
        from partner_db import row
        appliance = row("SELECT * FROM appliances WHERE id=? AND customer_id=?",
                        (appliance_id, customer_id))
        if (not appliance or appliance.get("customer_id") != customer_id
                or appliance.get("gateway_edition") != "local_gateway" or not _eligible_appliance(appliance)):
            raise HTTPException(status_code=403, detail="An active appliance owned by this customer is required.")
        if not _edition_verified(customer_id, appliance_id, "local_gateway"):
            raise HTTPException(status_code=409, detail="Edition pending verification.")
        if not CHECKOUT_AUTHORIZATION_ATOMICALLY_ENFORCED:
            raise HTTPException(status_code=503, detail="Checkout is unavailable while authorization protection is being completed.")
        import gateway_checkout_attempts
        if not gateway_checkout_attempts.ATTEMPTS_SERVICE_READY:
            raise HTTPException(status_code=503, detail="Checkout attempt protection is unavailable.")
        import hmac
        from token_security import unsign
        cookie=request.cookies.get('anyaicam_csrf'); token=request.headers.get('x-csrf-token')
        try:
            csrf_ok=isinstance(cookie,str) and isinstance(token,str) and bool(cookie) and hmac.compare_digest(cookie,token) and unsign(cookie)=='csrf'
        except (TypeError,ValueError):
            csrf_ok=False
        if not csrf_ok:
            raise HTTPException(status_code=403,detail='CSRF validation failed.')
        import checkout_guard
        import customer_entitlements
        import main
        import per_camera_billing
        licences = customer_entitlements.get_entitlements_for_customer(customer_id)
        if (customer_entitlements._purchased_vms_license_capacity(licences) <= 0
                or per_camera_billing.own_pc_licence_reversed(customer_id)):
            raise HTTPException(status_code=403, detail="An active purchased VMS licence is required.")
        existing = row("SELECT status FROM local_gateway_remote_subscriptions WHERE appliance_id=? AND customer_id=?",
                       (appliance_id, customer_id))
        if existing and existing.get("status") != "inactive":
            raise HTTPException(status_code=409, detail="This appliance already has remote service.")
        price_id = os.environ.get("ANYAICAM_STRIPE_PRICE_LOCAL_GATEWAY_REMOTE", "").strip()
        if not price_id or not main.PUBLIC_BASE_URL:
            raise HTTPException(status_code=503, detail="Local Gateway checkout is unavailable.")
        main.require_stripe_price_matches_catalog(price_id, 999, "month")
        stripe_customer_id = checkout_guard.canonical_stripe_customer(customer_id, email=identity.get("email"))
        base = main.PUBLIC_BASE_URL.rstrip("/")
        purchase, fields = checkout_fields(
            customer_id=customer_id, appliance_id=appliance_id,
            stripe_customer_id=stripe_customer_id, price_id=price_id,
            success_url=base + "/subscription-portal?local_gateway=success",
            cancel_url=base + "/subscription-portal?local_gateway=cancelled",
        )
        from gateway_checkout_handoff import create_session
        try:
            session = create_session(identity,customer_id=customer_id,appliance_id=appliance_id,
                edition='local_gateway',stripe_customer_id=stripe_customer_id,price_id=price_id,
                camera_ids=[],purchase=purchase,fields=fields)
        except Exception as error:
            raise HTTPException(status_code=503,detail='Checkout requires reconciliation.') from error
        from urllib.parse import urlsplit
        url = str(session.get("url") or "")
        try:
            parsed = urlsplit(url)
            safe_url = parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
        except ValueError:
            safe_url = False
        if not safe_url:
            raise HTTPException(status_code=502, detail="Checkout could not be started.")
        return {"status": "complete", "checkout_url": url}


def remote_access_for_appliance(customer_id: str, appliance_id: str) -> bool:
    """Return only this owned appliance's current remote-service decision.

    Missing, malformed or unavailable billing state never grants access.
    A different customer-wide plan cannot change this decision.
    """
    if not customer_id or not appliance_id:
        return False
    try:
        from partner_db import row
        appliance = row("SELECT * FROM appliances WHERE id=?", (appliance_id,))
        if (not appliance or appliance.get("customer_id") != customer_id
                or appliance.get("gateway_edition") != "local_gateway" or not _eligible_appliance(appliance)):
            return False
        # An internal-test decision must remain supported by its original,
        # current authorization. Database or verifier errors fail closed.
        from partner_db import transaction
        from gateway_internal_provisioning import edition_authorization_effective
        with transaction() as db:
            if not edition_authorization_effective(db, customer_id, appliance_id, "local_gateway"):
                return False
        binding = row(
            "SELECT customer_id,status,payment_failed_at FROM local_gateway_remote_subscriptions WHERE appliance_id=?",
            (appliance_id,),
        )
        if not binding or binding.get("customer_id") != customer_id or binding.get("status") != "active":
            return False
        failed_at = binding.get("payment_failed_at")
        if failed_at:
            from datetime import datetime
            from billing_status import grace_ends_at
            deadline = grace_ends_at(failed_at)
            if deadline is None:
                return False
            now = datetime.now(deadline.tzinfo) if deadline.tzinfo else datetime.now()
            if now >= deadline:
                return False
        return True
    except Exception:
        return False


def _table_available() -> bool:
    """Inspect schema without hiding connection or permission failures."""
    from database_backend import backend
    from partner_db import row
    if backend() == "sqlite":
        return bool(row("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                        ("local_gateway_remote_subscriptions",)))
    return bool(row("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=current_schema() AND table_name=?",
                    ("local_gateway_remote_subscriptions",)))


def suspend_subscription(subscription_id: str, reason: str) -> int:
    from partner_db import connection
    if not _table_available():
        return 0
    with connection() as db:
        return db.execute(
            "UPDATE local_gateway_remote_subscriptions SET status='suspended',suspended_reason=? "
            "WHERE stripe_subscription_id=? AND status='active'",
            (reason, subscription_id),
        ).rowcount


def restore_subscription(subscription_id: str, reasons: tuple[str, ...], *, clear_failure: bool = False) -> int:
    """Apply Local recovery atomically; a failed restoration rolls back its marker."""
    from partner_db import connection
    if (not reasons and not clear_failure) or not _table_available():
        return 0
    with connection() as db:
        if clear_failure:
            db.execute("UPDATE local_gateway_remote_subscriptions SET payment_failed_at=NULL "
                       "WHERE stripe_subscription_id=?", (subscription_id,))
        if not reasons:
            return 0
        marks = ",".join("?" for _ in reasons)
        return db.execute(
            "UPDATE local_gateway_remote_subscriptions SET status='active',suspended_reason=NULL "
            "WHERE stripe_subscription_id=? AND status='suspended' "
            "AND stripe_status NOT IN ('canceled','incomplete_expired','unpaid') "
            f"AND suspended_reason IN ({marks})",
            (subscription_id, *reasons),
        ).rowcount


def set_payment_failed(subscription_id: str, stamp: str) -> None:
    from partner_db import connection
    if not _table_available():
        return
    with connection() as db:
        db.execute("UPDATE local_gateway_remote_subscriptions SET payment_failed_at=? "
                   "WHERE stripe_subscription_id=? AND payment_failed_at IS NULL "
                   "AND status IN ('active','suspended')", (stamp, subscription_id))


def clear_payment_failed(subscription_id: str) -> None:
    from partner_db import connection
    if not _table_available():
        return
    with connection() as db:
        db.execute("UPDATE local_gateway_remote_subscriptions SET payment_failed_at=NULL "
                   "WHERE stripe_subscription_id=?", (subscription_id,))


def sync_from_verified_webhook(event: dict) -> dict:
    """Process only a signature-verified event dispatched by the VMS webhook.

    Checkout provisioning is anchored to the existing pending-checkout guard;
    later subscription events can update only an existing subscription ID.
    Current provider state, not event-time subscription state, is authoritative.
    """
    from datetime import datetime, timedelta, timezone
    import stripe_state
    from partner_db import connection, row

    event_type = str(event.get("type") or "")
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        return {"status": "ignored"}
    checkout = event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}
    if checkout:
        metadata = obj.get("metadata") or {}
        if not isinstance(metadata, dict) or metadata.get("anyaicam_product") != "local_gateway_remote":
            return {"status": "ignored"}
        if obj.get("mode") != "subscription" or obj.get("payment_status") not in {"paid", "no_payment_required"}:
            return {"status": "pending"}
        customer_id = str(metadata.get("anyaicam_customer_id") or "")
        appliance_id = str(metadata.get("anyaicam_appliance_id") or "")
        session_id = str(obj.get("id") or "")
        pending = row(
            "SELECT price_id FROM checkout_pending WHERE customer_id=? AND purchase=? "
            "AND session_id=? AND quantity=1 AND status IN ('open','paid')",
            (customer_id, "local_gateway_remote:" + appliance_id, session_id),
        )
        sub_id = str(obj.get("subscription") or "")
        stripe_customer_id = str(obj.get("customer") or "")
        if not pending:
            # A distinct redelivery after the checkout guard completed must
            # not attempt a second bind or reopen a terminal entitlement.
            existing = row(
                "SELECT customer_id,stripe_customer_id,stripe_subscription_id "
                "FROM local_gateway_remote_subscriptions WHERE appliance_id=?", (appliance_id,))
            if (existing and existing.get("customer_id") == customer_id
                    and existing.get("stripe_customer_id") == stripe_customer_id
                    and existing.get("stripe_subscription_id") == sub_id and sub_id):
                return {"status": "already_processed"}
            raise stripe_state.RetryableStripeEventError("Local Gateway checkout binding is unavailable")
        price_id = str(pending.get("price_id") or "")
        observed_checkout_binding = row("SELECT * FROM local_gateway_remote_subscriptions WHERE appliance_id=? AND customer_id=?",
                                        (appliance_id, customer_id))
    elif event_type.startswith("customer.subscription."):
        metadata = obj.get("metadata") or {}
        # Existing per-camera products never consult the new table.
        if not isinstance(metadata, dict) or metadata.get("anyaicam_product") != "local_gateway_remote":
            return {"status": "ignored"}
        sub_id = str(obj.get("id") or "")
        binding = row("SELECT * FROM local_gateway_remote_subscriptions WHERE stripe_subscription_id=?", (sub_id,))
        if not binding:
            return {"status": "ignored"}
        customer_id = str(binding["customer_id"])
        appliance_id = str(binding["appliance_id"])
        stripe_customer_id = str(binding["stripe_customer_id"])
        price_id = str(binding["stripe_price_id"])
    else:
        return {"status": "ignored"}
    if not sub_id or not stripe_customer_id or not price_id:
        raise stripe_state.RetryableStripeEventError("Local Gateway subscription binding is incomplete")
    if stripe_state.bound_elsewhere(stripe_customer_id, customer_id):
        raise stripe_state.RetryableStripeEventError("Local Gateway customer binding mismatch")
    current = stripe_state.current_subscription(sub_id)
    if not current:
        raise stripe_state.RetryableStripeEventError("Local Gateway subscription state unavailable")
    # Verify payment and commit binding/status in one transaction. The
    # reversal helper may acquire several ordered payment locks internally;
    # do not acquire an invoice-only lock before that helper.
    # Database exceptions propagate to the verified webhook's retry machinery.
    from partner_db import transaction
    invoice_id = stripe_state._id(current.get("latest_invoice")) if current.get("status") == "active" else None
    if current.get("status") == "active" and not invoice_id:
        raise stripe_state.RetryableStripeEventError("Local Gateway payment not identified")
    stamp = datetime.now(timezone.utc).isoformat()
    # A successful update must change its comparison token, even if two
    # updates happen in the same clock tick.
    old_binding = observed_checkout_binding if checkout else binding
    if old_binding and stamp == old_binding.get("updated_at"):
        stamp = (datetime.fromisoformat(stamp) + timedelta(microseconds=1)).isoformat()
    with transaction() as db:
        verified_payment = False
        reversal = None
        if invoice_id:
            invoice = stripe_state.stripe_get("/v1/invoices/" + invoice_id, "the subscription invoice")
            if invoice.get("id") != invoice_id or invoice.get("status") != "paid":
                raise stripe_state.RetryableStripeEventError("Local Gateway payment not confirmed")
            reversal = stripe_state.subscription_payment_reversal({**current, "latest_invoice": invoice})
            if not reversal:
                verified_payment = _verified_unreversed_invoice(invoice, current)
        if not checkout and reversal:
            snapshot = tuple(binding.get(key) for key in ("status", "stripe_status", "updated_at"))
            if not all(isinstance(value, str) and value for value in snapshot):
                raise stripe_state.RetryableStripeEventError("Local Gateway binding snapshot is invalid")
            result = db.execute(
                "UPDATE local_gateway_remote_subscriptions SET status='suspended',suspended_reason=?,updated_at=? "
                "WHERE stripe_subscription_id=? AND status='active' AND status=? AND stripe_status=? AND updated_at=?",
                (str(reversal), stamp, sub_id, *snapshot),
            )
            if result.rowcount == 1:
                return {"status": "suspended", "reason": str(reversal)}
            latest = db.execute(
                "SELECT status,stripe_status,updated_at FROM local_gateway_remote_subscriptions WHERE stripe_subscription_id=?",
                (sub_id,),
            ).fetchone()
            if latest is None:
                return {"status": "ignored"}
            current_binding = tuple(latest[key] for key in ("status", "stripe_status", "updated_at")) if hasattr(latest, "keys") else tuple(latest)
            if current_binding != snapshot:
                raise stripe_state.RetryableStripeEventError("Local Gateway binding changed during verification")
            return {"status": "ignored"}
        ended_subscription_id = None
        if checkout:
            previous = row("SELECT stripe_subscription_id,status FROM local_gateway_remote_subscriptions "
                           "WHERE appliance_id=? AND customer_id=?", (appliance_id, customer_id))
            if previous and previous.get("stripe_subscription_id") != sub_id:
                previous_id = str(previous.get("stripe_subscription_id") or "")
                prior = stripe_state.current_subscription(previous_id)
                if (not prior or prior.get("id") != previous_id or
                        prior.get("customer") != stripe_customer_id or
                        prior.get("status") not in stripe_state.ENDED_STATUSES):
                    raise stripe_state.RetryableStripeEventError("Previous Local Gateway subscription has not ended")
                ended_subscription_id = previous_id
        if checkout:
            decision = bind_verified_paid_checkout(
                db, obj, current, customer_id=customer_id, appliance_id=appliance_id,
                stripe_customer_id=stripe_customer_id, price_id=price_id,
                payment_verified=verified_payment, verified_at=stamp,
                ended_subscription_id=ended_subscription_id,
                observed_binding=observed_checkout_binding, protect_observed_binding=True,
            )
        else:
            decision = reconcile_bound_subscription(
                db, current, customer_id=customer_id, appliance_id=appliance_id,
                stripe_customer_id=stripe_customer_id, price_id=price_id,
                payment_verified=verified_payment, verified_at=stamp, observed_binding=binding,
            )
            if decision.reason == "binding_changed":
                raise stripe_state.RetryableStripeEventError("Local Gateway binding changed during verification")
            if (decision.state == "denied" and decision.reason != "binding_not_current"
                    and current.get("status") != "past_due"):
                db.execute(
                    "UPDATE local_gateway_remote_subscriptions SET status='suspended',"
                    "suspended_reason='verification_failed',updated_at=? "
                    "WHERE appliance_id=? AND customer_id=? AND stripe_subscription_id=?",
                    (stamp, appliance_id, customer_id, sub_id),
                )
        if checkout and decision.state != "active":
            raise stripe_state.RetryableStripeEventError("Local Gateway checkout was not provisioned")
    return {"status": decision.state, "reason": decision.reason}


def _verified_unreversed_invoice(invoice: dict, subscription: dict) -> bool:
    """Require current provider payment evidence as well as local reversal state."""
    from urllib.parse import quote
    import stripe_state
    import sales_commissions
    if invoice.get("status") != "paid":
        return False
    if stripe_state.subscription_payment_reversal({**subscription, "latest_invoice": invoice}):
        return False
    amount_paid = invoice.get("amount_paid")
    # Missing, malformed or negative amounts are not evidence of payment.
    # A verified paid zero-value invoice remains compatible with existing billing.
    if type(amount_paid) is not int or amount_paid < 0:
        return False
    if amount_paid == 0:
        return True
    intent = sales_commissions._invoice_payment_intent(invoice)
    charge = None
    if intent:
        details = stripe_state.stripe_get(
            "/v1/payment_intents/" + quote(str(intent), safe="") + "?expand[]=latest_charge",
            "the subscription payment")
        charge = details.get("latest_charge")
    if not isinstance(charge, dict):
        charge_id = stripe_state._id(charge or invoice.get("charge"))
        if not charge_id:
            return False
        charge = stripe_state.stripe_get("/v1/charges/" + quote(charge_id, safe=""),
                                         "the subscription charge")
    # Missing dispute/refund facts are not proof of an unreversed payment.
    if (not isinstance(charge, dict) or type(charge.get("disputed")) is not bool
            or type(charge.get("refunded")) is not bool
            or type(charge.get("amount")) is not int or charge["amount"] <= 0
            or type(charge.get("amount_refunded")) is not int
            or not 0 <= charge["amount_refunded"] <= charge["amount"]):
        return False
    if charge["disputed"] or stripe_state._full_refund(charge):
        return False
    return True


def issue_remote_lease(appliance: dict, request_nonce: str, *, now=None) -> dict | None:
    """Sign a fresh nonce-bound remote lease only after current payment checks.

    A failed provider, binding, deadline or signing check grants nothing. This
    function does not alter billing or local VMS capacity.
    """
    from datetime import datetime, timedelta, timezone
    from urllib.parse import quote
    try:
        import appliance_identity
        import stripe_state
        from partner_db import connection, row
        customer_id = appliance.get("customer_id")
        appliance_id = appliance.get("id")
        cloud_id = appliance.get("cloud_id")
        if not all(isinstance(value, str) and value.strip() for value in
                   (customer_id, appliance_id, cloud_id, request_nonce)):
            return None
        if len(request_nonce) < 16 or not remote_access_for_appliance(customer_id, appliance_id):
            return None
        binding = row("SELECT * FROM local_gateway_remote_subscriptions "
                      "WHERE appliance_id=? AND customer_id=?",
                      (appliance_id, customer_id))
        if not binding or binding.get("status") != "active":
            return None
        # Existing failure timestamps can be naive local times. Do not invent
        # a timezone or lease across an unverifiable grace deadline.
        if binding.get("payment_failed_at"):
            return None
        sub = stripe_state.current_subscription(binding.get("stripe_subscription_id"))
        if not sub or stripe_state.bound_elsewhere(binding.get("stripe_customer_id"), customer_id):
            return None
        invoice_id = stripe_state._id(sub.get("latest_invoice"))
        if not invoice_id:
            return None
        invoice = stripe_state.stripe_get("/v1/invoices/" + quote(invoice_id, safe=""), "the subscription invoice")
        if invoice.get("id") != invoice_id or invoice.get("status") != "paid":
            return None
        verified = _verified_unreversed_invoice(invoice, sub)
        decision = evaluate_current_subscription(
            sub, customer_id=customer_id, appliance_id=appliance_id,
            stripe_customer_id=binding.get("stripe_customer_id"),
            price_id=binding.get("stripe_price_id"), payment_verified=verified)
        if decision.state != "active" or sub.get("id") != binding.get("stripe_subscription_id"):
            return None
        issued = now or datetime.now(timezone.utc)
        if issued.tzinfo is None:
            return None
        issued = issued.astimezone(timezone.utc)
        deadline = issued + timedelta(hours=24)
        cancel = sub.get("cancel_at_period_end", False)
        if type(cancel) is not bool:
            return None
        # Even when automatic renewal is expected, the next period is not yet
        # verified as paid. Never sign beyond the currently verified period.
        period = sub.get("current_period_end")
        if type(period) is not int or period <= 0:
            return None
        deadline = min(deadline, datetime.fromtimestamp(period, timezone.utc))
        cancel_at = sub.get("cancel_at")
        if cancel_at is not None:
            if type(cancel_at) is not int or cancel_at <= 0:
                return None
            deadline = min(deadline, datetime.fromtimestamp(cancel_at, timezone.utc))
        if deadline <= issued:
            return None
        latest_binding = row("SELECT stripe_subscription_id,stripe_customer_id,stripe_price_id,status,payment_failed_at "
                             "FROM local_gateway_remote_subscriptions WHERE appliance_id=? AND customer_id=?",
                             (appliance_id, customer_id))
        if (not latest_binding or latest_binding.get("status") != "active"
                or latest_binding.get("payment_failed_at")
                or latest_binding.get("stripe_subscription_id") != sub.get("id")
                or latest_binding.get("stripe_customer_id") != binding.get("stripe_customer_id")
                or latest_binding.get("stripe_price_id") != binding.get("stripe_price_id")
                or not remote_access_for_appliance(customer_id, appliance_id)):
            return None
        body = {"type": "anyaicam.local_gateway_remote_access", "version": 1,
                "appliance_id": appliance_id, "cloud_id": cloud_id, "customer_id": customer_id,
                "request_nonce": request_nonce, "issued_at": issued.isoformat(),
                "expires_at": deadline.isoformat()}
        with connection() as db:
            key = appliance_identity.ensure_signing_key(db)
        signature = appliance_identity.sign_body(body, key_id=key["key_id"], private_key_b64=key["private_key_b64"])
        return {**body, "signature": signature}
    except Exception:
        return None
