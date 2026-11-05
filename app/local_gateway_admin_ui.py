"""Read-only edition-verification queue for the existing partner workspace."""
from html import escape


def claim_purchase_context(appliance, query):
    """Show related records without treating either as proof of an edition."""
    appliance_id = str(appliance.get('id') or '')
    customer_id = str(appliance.get('customer_id') or '')
    claims = query("SELECT id,customer_id,status,revoked_at FROM appliance_claims WHERE appliance_id=?",
                   (appliance_id,))
    completed = [c for c in claims if c.get('status') == 'completed' and not c.get('revoked_at')]
    if len(completed) == 1 and completed[0].get('customer_id') == customer_id:
        claim = 'Completed claim ' + str(completed[0].get('id') or '(unknown)')
    elif completed:
        claim = 'Ambiguous or mismatched claim records; review required'
    else:
        claim = 'No current completed claim record'
    licences = query("SELECT id,status,camera_slot_quantity FROM customer_entitlements "
                     "WHERE customer_id=? AND product=?", (customer_id, 'vms_license'))
    if len(licences) == 1:
        license_row = licences[0]
        licence = 'VMS license record {}: {} (not edition evidence)'.format(
            license_row.get('id') or '(unknown)', license_row.get('status') or 'unknown')
    elif licences:
        licence = 'Ambiguous VMS license records (not edition evidence)'
    else:
        licence = 'No VMS license record (not edition evidence)'
    return claim, licence


def verification_queue(appliances, customers, *, is_global_admin, query=None):
    # The caller must use the same live global grant as the operations API.
    if not is_global_admin:
        return ""
    from appliance_readiness import _live, enrollment_ready
    by_id = {str(c.get('id')): c for c in customers}
    cards = []
    for appliance in appliances:
        if appliance.get('activated_via') != 'claim' or appliance.get('gateway_edition') is not None:
            continue
        appliance_id = str(appliance.get('id') or '')
        customer_id = str(appliance.get('customer_id') or '')
        customer = by_id.get(customer_id) or {}
        live = _live(appliance_id) and appliance.get('state') != 'retired'
        ready = live and enrollment_ready(appliance_id)
        claim, licence = claim_purchase_context(appliance, query) if query else ('Claim details unavailable', 'VMS license details unavailable')
        fields = [
            ('Customer', customer.get('name') or 'Unknown customer'),
            ('Customer ID', customer_id),
            ('Customer email', customer.get('email') or 'Not available'),
            ('Appliance', appliance.get('cloud_id') or appliance_id),
            ('Appliance ID', appliance_id),
            ('Claim record', claim),
            ('Software license', licence),
            ('Credential', 'Live' if live else 'Unavailable or revoked'),
            ('Credential and heartbeat', 'Ready' if ready else 'Not ready'),
            ('Edition evidence', 'Pending verification: no trusted edition evidence is linked'),
        ]
        detail = ''.join('<div><strong>{}</strong>: {}</div>'.format(escape(k), escape(str(v))) for k, v in fields)
        cards.append('<article class="customer-row"><div>' + detail + '</div></article>')
    body = ''.join(cards) or '<div class="empty">No claimed appliances await edition verification.</div>'
    return ('<section class="panel" aria-label="Local Gateway edition verification">'
            '<h2>Local Gateway edition verification</h2>'
            '<p>Pending verification. Claim and heartbeat records do not establish the purchased edition. '
            'Confirmation is unavailable until independently verified edition evidence is connected and reviewed.</p>'
            '<div class="customer-list">' + body + '</div></section>')
