"""When a claimed appliance has really enrolled (2026-10-08).

claim/complete creates the cloud's appliance row and issues the credential,
but the appliance can still fail to save its identity locally and roll back
(first_enroll()/coordinated_reenroll() in the agent). So neither "the
appliance row exists" nor "the claim was confirmed" proves the appliance is
working. Ready means THIS appliance has done both:

  * used its own, non-revoked credential -- authenticate_appliance() stamps
    appliance_credentials.last_used_at on every authenticated request (the
    agent's own post-enrollment verification is the first one);
  * sent a heartbeat (appliances.last_check_in, an authenticated request) at
    or after that credential was issued.

Both timestamps are written with datetime.now().isoformat() by the cloud
itself, so they compare as strings.
"""
from __future__ import annotations

from partner_db import row, rows


def enrollment_ready(appliance_id: str) -> bool:
    if not appliance_id:
        return False
    credential = row(
        'SELECT created_at,last_used_at FROM appliance_credentials '
        'WHERE appliance_id=? AND revoked_at IS NULL AND last_used_at IS NOT NULL ORDER BY created_at DESC LIMIT 1',
        (appliance_id,),
    )
    if not credential:
        return False
    appliance = row('SELECT last_check_in FROM appliances WHERE id=?', (appliance_id,))
    return bool(appliance and appliance['last_check_in'] and str(appliance['last_check_in']) >= str(credential['created_at']))


def appliance_activated(appliance: dict | None) -> bool:
    """The appliance is set up for its customer: device-activated with a
    token (appliance_cloud.activate() writes activation_status='activated'),
    or claimed and really enrolled.

    claim/complete (appliance_claims.py) issues the credential but leaves
    activation_status at its 'pending' default, so an owner whose appliance
    came in through a claim code -- with a working credential and live
    heartbeats -- was sent back to /customer/setup forever (2026-10-10).
    A claimed appliance counts once enrollment_ready() proves it is really
    working; a released appliance never does. Read-only: nothing is
    written, so every existing claimed appliance is covered as is, with no
    database backfill (and activation_status keeps driving only what it
    already drove, e.g. build_orders.py's storage-plan start)."""
    if not appliance:
        return False
    status = str(appliance.get('activation_status') or '')
    if status == 'activated':
        return True
    if status == 'released':
        return False
    return enrollment_ready(str(appliance.get('id') or ''))


def customer_has_activated_appliance(customer_id: str) -> bool:
    """appliance_activated() for any of this customer's own appliances."""
    if not customer_id:
        return False
    if row("SELECT id FROM appliances WHERE customer_id=? AND activation_status='activated' LIMIT 1", (customer_id,)):
        return True
    candidates = rows(
        "SELECT a.id,a.activation_status FROM appliances a WHERE a.customer_id=? "
        "AND COALESCE(a.activation_status,'pending')!='released' AND a.last_check_in IS NOT NULL "
        "AND EXISTS(SELECT 1 FROM appliance_credentials c WHERE c.appliance_id=a.id AND c.revoked_at IS NULL)",
        (customer_id,),
    )
    return any(appliance_activated(candidate) for candidate in candidates)
