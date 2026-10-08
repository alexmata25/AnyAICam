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

from partner_db import row


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
