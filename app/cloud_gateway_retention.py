"""Cloud Gateway event-clip retention (owner approval 2026-11-05).

Policy:
- 14 days is the default for every eligible Cloud Gateway customer;
- 7 and 30 days are the only other options;
- a customer owner (their own account) or a live global administrator
  may change it, validated and audited here on the server.

Cloud Gateway media is authorized by the $9.99-per-camera paid binding
(appliance_cloud._require_gateway_media_authorization), not by a Full VMS
plan. Its retention therefore lives in its own table and never reads or
writes the Full VMS sources (customer_cloud_policy override, v2
cloud_event_retention_days, legacy plans). A Full VMS Hybrid customer's
retention promise cannot be shortened by a Gateway choice, nor the
reverse.

Guarantees:
- effective_retention_days() never returns None for a Cloud Gateway
  customer, so media accepted from a Gateway camera always expires
  (stored choice, else the 14-day default), even after the binding ends.
- The upload guard calls ensure_default() inside its own write
  transaction. The first authorized upload records the 14-day default
  (source 'default', audited), so the customer "receives" it
  automatically with no backfill.
- Only cloud S3 event media is affected. Local recordings on the
  appliance are never read, listed or deleted by this module or by the
  sweep; the edge keeps its own local retention independently.
- Changes are versioned (optimistic concurrency), and lowering the
  retention needs confirm_reduction=True, because the next sweep tick
  deletes clips older than the new period.

Nothing here contacts AWS or Stripe."""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import HTTPException, Request

DEFAULT_RETENTION_DAYS = 14
RETENTION_OPTIONS = (7, 14, 30)
TABLE = 'cloud_gateway_retention_settings'
SOURCES = frozenset({'default', 'customer_owner', 'administrator'})


class RetentionRefused(Exception):
    """A change request the server will not apply; status is the HTTP code."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_retention_days(value) -> int:
    """Strict: an int (not bool, float or string) in RETENTION_OPTIONS."""
    if isinstance(value, bool) or not isinstance(value, int) or value not in RETENTION_OPTIONS:
        raise RetentionRefused(422, 'Retention must be 7, 14 or 30 days.')
    return value


def customer_has_cloud_gateway(db, customer_id: str) -> bool:
    """Any Cloud Gateway camera binding, current or ended, for this customer."""
    return db.execute('SELECT 1 FROM cloud_gateway_camera_entitlements WHERE customer_id=? LIMIT 1',
                      (customer_id,)).fetchone() is not None


def cloud_gateway_camera_ids(db) -> set:
    """Every camera that has ever held a Cloud Gateway binding. Its event
    media follows Cloud Gateway retention in the sweep."""
    return {row['camera_id'] for row in db.execute('SELECT DISTINCT camera_id FROM cloud_gateway_camera_entitlements').fetchall()}


def _row(db, customer_id: str):
    # A missing table raises here: the guard turns that into 503 and the
    # sweep skips the row (never deletes early).
    row = db.execute(f'SELECT customer_id,retention_days,source,version,updated_at,updated_by FROM {TABLE} WHERE customer_id=?',
                     (customer_id,)).fetchone()
    return dict(row) if row else None


def effective_retention_days(db, customer_id: str) -> int:
    """The stored choice, else the 14-day default. Never None. A corrupt
    stored value raises; the sweep then skips (never deletes early) and
    the upload guard refuses."""
    row = _row(db, customer_id)
    if row is None:
        return DEFAULT_RETENTION_DAYS
    days = row['retention_days']
    if isinstance(days, bool) or not isinstance(days, int) or days not in RETENTION_OPTIONS:
        raise RuntimeError(f'Invalid Cloud Gateway retention on file for customer {customer_id}.')
    return days


def _audit(db, *, actor_email, actor_role, action, customer_id, details, now):
    db.execute('INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)',
               (actor_email, actor_role, action, 'customer', customer_id, json.dumps(details, sort_keys=True), now))


def ensure_default(db, customer_id: str, *, now: str | None = None) -> int:
    """Inside the caller's write transaction: record the 14-day default if
    the customer has no setting yet, then return the effective retention."""
    now = now or datetime.now().isoformat()
    if _row(db, customer_id) is None:
        db.execute(f'INSERT INTO {TABLE}(customer_id,retention_days,source,version,updated_at,updated_by) VALUES(?,?,?,?,?,?) '
                   'ON CONFLICT(customer_id) DO NOTHING',
                   (customer_id, DEFAULT_RETENTION_DAYS, 'default', 1, now, 'system:cloud_gateway_default'))
        _audit(db, actor_email='system:cloud_gateway_default', actor_role='system', action='cloud_gateway.retention.default_applied',
               customer_id=customer_id, details={'retention_days': DEFAULT_RETENTION_DAYS}, now=now)
    return effective_retention_days(db, customer_id)


def describe(db, customer_id: str) -> dict:
    row = _row(db, customer_id)
    return {
        'customer_id': customer_id,
        'retention_days': effective_retention_days(db, customer_id),
        'source': row['source'] if row else 'default',
        'version': row['version'] if row else 0,
        'options': list(RETENTION_OPTIONS),
        'default_retention_days': DEFAULT_RETENTION_DAYS,
        'applies_to': 'cloud_event_clips_only',
    }


def authorize_actor(db, identity: dict | None, requested_customer_id) -> tuple[str, str, str]:
    """(customer_id, actor_email, actor_role) or RetentionRefused.
    A customer owner may act only on their own account; the requested id,
    if any, must match it. A global administrator must name a customer.
    Partners, technicians and salespeople are refused."""
    if not identity:
        raise RetentionRefused(401, 'Sign in required.')
    role = identity.get('role')
    email = str(identity.get('email') or '').strip().lower()
    if role == 'customer_owner':
        own = identity.get('customer_id')
        if not own or (requested_customer_id not in (None, '') and requested_customer_id != own):
            raise RetentionRefused(403, 'Customer owner permission required for this account.')
        return own, email, role
    if role == 'administrator':
        from appliance_identity import has_global_administrator_grant
        if not email or not has_global_administrator_grant(db, email=email):
            raise RetentionRefused(403, 'A global administrator is required.')
        if not isinstance(requested_customer_id, str) or not requested_customer_id:
            raise RetentionRefused(422, 'customer_id is required.')
        return requested_customer_id, email, role
    raise RetentionRefused(403, 'Customer owner permission required.')


def set_retention(db, customer_id: str, retention_days, *, actor_email: str, actor_role: str,
                  expected_version, confirm_reduction: bool = False, now: str | None = None) -> dict:
    """Inside the caller's write transaction. Validates, compares the
    version, requires confirmation to lower, writes and audits."""
    days = parse_retention_days(retention_days)
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
        raise RetentionRefused(422, 'expected_version is required.')
    if actor_role not in ('customer_owner', 'administrator'):
        raise RetentionRefused(403, 'Customer owner permission required.')
    if db.execute('SELECT 1 FROM customers WHERE id=?', (customer_id,)).fetchone() is None:
        raise RetentionRefused(404, 'Customer not found.')
    if not customer_has_cloud_gateway(db, customer_id):
        raise RetentionRefused(409, 'This account has no Cloud Gateway cameras.')
    now = now or datetime.now().isoformat()
    row = _row(db, customer_id)
    current_version = row['version'] if row else 0
    if expected_version != current_version:
        raise RetentionRefused(409, 'Retention changed since it was loaded; reload and try again.')
    previous = effective_retention_days(db, customer_id)
    if days < previous and confirm_reduction is not True:
        raise RetentionRefused(409, f'Lowering retention from {previous} to {days} days deletes older cloud clips; confirm to continue.')
    source = 'administrator' if actor_role == 'administrator' else 'customer_owner'
    if row is None:
        db.execute(f'INSERT INTO {TABLE}(customer_id,retention_days,source,version,updated_at,updated_by) VALUES(?,?,?,?,?,?)',
                   (customer_id, days, source, 1, now, actor_email))
    else:
        updated = db.execute(f'UPDATE {TABLE} SET retention_days=?,source=?,version=version+1,updated_at=?,updated_by=? '
                             'WHERE customer_id=? AND version=?', (days, source, now, actor_email, customer_id, current_version))
        if getattr(updated, 'rowcount', 1) != 1:
            raise RetentionRefused(409, 'Retention changed since it was loaded; reload and try again.')
    _audit(db, actor_email=actor_email, actor_role=actor_role, action='cloud_gateway.retention.changed', customer_id=customer_id,
           details={'from_days': previous, 'to_days': days, 'version': current_version + 1}, now=now)
    return describe(db, customer_id)


def register_cloud_gateway_retention_routes(app) -> None:
    from partner_db import connection, transaction
    from partner_portal import partner_identity

    def _http(error: RetentionRefused):
        return HTTPException(status_code=error.status, detail=error.message)

    @app.get('/api/cloud-gateway/retention')
    def cloud_gateway_retention_get(request: Request, customer_id: str | None = None):
        try:
            with connection() as db:
                target, _, _ = authorize_actor(db, partner_identity(request), customer_id)
                if not customer_has_cloud_gateway(db, target):
                    raise RetentionRefused(404, 'This account has no Cloud Gateway cameras.')
                return describe(db, target)
        except RetentionRefused as error:
            raise _http(error) from None

    @app.put('/api/cloud-gateway/retention')
    async def cloud_gateway_retention_put(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=422, detail='A JSON body is required.') from None
        if not isinstance(body, dict) or set(body) - {'customer_id', 'retention_days', 'expected_version', 'confirm_reduction'}:
            raise HTTPException(status_code=422, detail='Unexpected fields.')
        try:
            with transaction() as db:
                target, email, role = authorize_actor(db, partner_identity(request), body.get('customer_id'))
                return set_retention(db, target, body.get('retention_days'), actor_email=email, actor_role=role,
                                     expected_version=body.get('expected_version'),
                                     confirm_reduction=body.get('confirm_reduction') is True)
        except RetentionRefused as error:
            raise _http(error) from None
